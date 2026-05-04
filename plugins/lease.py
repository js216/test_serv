# SPDX-License-Identifier: MIT
# lease.py --- Cross-session device holds
# Copyright (c) 2026 Jakob Kastelic

"""Lease plugin: hold one or more devices across multiple plan submissions.

Use case: an interactive R&D debug window where the operator wants to
keep the device opened by plan N reachable for plan N+1 (FT4222 SPI
where each open() resets the chip; multi-agent benches where another
agent might otherwise grab the device between two of your plans).

Lifecycle:
    plan 1:  lease:claim devices="dsp.A,fpga.hx1k" duration_s=600
                --> emits the token to the lease.token stream so the
                    agent can read it from the artefact.
    plan 2:  lease:resume token="abc..."
             dsp.A:read_serial   # works; the lease blocks other agents
             ...
    plan N:  lease:resume token="abc..."
             lease:release token="abc..."

If a plan ends without releasing, the lease lives until expires_at and
the operator can keep submitting `resume` plans; the registry evicts
expired leases on the next refresh tick. State is in-memory; a poller
restart loses every live lease.
"""

import config
from plugin import DevicePlugin, Op


def _lease_token_from(args):
    v = args.get("token")
    return v.raw if (v is not None and hasattr(v, "raw")) else None


def _split_devices(arg):
    raw = arg.raw if hasattr(arg, "raw") else str(arg)
    return [s.strip() for s in raw.split(",") if s.strip()]


def _op_claim(session, h, args):
    devices = _split_devices(args["devices"])
    duration_s = args["duration_s"]
    token = session.registry.lease_claim(devices, duration_s)
    session.lease_token = token
    session.stream("lease.token").append(
        (token + "\n").encode())
    session.log_event(
        "LEASE", "lease:claim",
        f"token={token} devices={sorted(devices)} "
        f"duration_s={duration_s}")


def _op_resume(session, h, args):
    # _prescan_lease_resume in session.py validates the token at
    # session start (before lock acquisition). At op time we just
    # log -- the work is already done.
    token = _lease_token_from(args)
    session.log_event("LEASE", "lease:resume", f"token={token}")


def _op_release(session, h, args):
    token = _lease_token_from(args)
    if token is None:
        # Default to the session's own lease.
        token = getattr(session, "lease_token", None)
    if token is None:
        session.log_event("LEASE", "lease:release", "no-op (no token)")
        return
    dropped = session.registry.lease_release(token)
    session.log_event(
        "LEASE", "lease:release",
        f"token={token} devices={sorted(dropped)}")


def _op_list(session, h, args):
    import json as _json
    leases = session.registry.lease_list()
    session.stream("lease.list").append(
        (_json.dumps(leases, indent=2) + "\n").encode())
    session.log_event("LEASE", "lease:list", f"n={len(leases)}")


class LeasePlugin(DevicePlugin):
    name = "lease"
    doc = (
        "Cross-session device hold. Claim a set of devices for N "
        "seconds; subsequent plans `resume` the lease via its token "
        "to keep other agents from grabbing the device between "
        "submissions. State is in-memory; poller restart drops all "
        "leases.")

    ops = {
        "claim": Op(
            args={"devices": "str", "duration_s": "int"},
            doc=("Claim devices=\"dsp.A,fpga.hx1k\" duration_s=600. "
                 "The returned token is appended to the lease.token "
                 "stream in the artefact; the agent reads it and "
                 "passes it to subsequent `resume` ops."),
            run=_op_claim),
        "resume": Op(
            args={"token": "str"},
            doc=("First op of a follow-up plan: validates the token "
                 "and binds the session to the held devices, so the "
                 "session's eager-lock-acquire treats them as "
                 "owned. Other plans without the token fast-fail "
                 "with BusyError."),
            run=_op_resume),
        "release": Op(
            args={},
            optional_args={"token": "str"},
            doc=("Drop the lease (defaults to this session's own "
                 "lease). Other agents can claim the devices "
                 "immediately after."),
            run=_op_release),
        "list": Op(
            args={},
            doc=("Append the current lease table to the lease.list "
                 "stream as JSON."),
            run=_op_list),
    }

    def probe(self):
        # The lease plugin owns no real device. probe() must return at
        # least one spec so the registry / session layer can treat
        # `lease:claim` as a valid op routed at one canonical instance.
        return [{"id": "_default"}]

    def open(self, spec):
        return _LeaseHandle()

    def close(self, handle):
        pass


class _LeaseHandle:
    pass
