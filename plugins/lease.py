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
    session.lease_just_claimed = True
    # If the caller asked for auto-release, mark the lease so the
    # session's finally block drops it on session end. Useful for
    # one-plan workflows ("claim, do work, release") that don't
    # want to leave the bench locked if something goes wrong
    # mid-plan; for cross-session holds, leave default (false).
    if bool(args.get("auto_release_on_session_end")):
        session.lease_release_on_end = True
    # The token is the credential. Don't write it to a stream or to
    # event-msg text -- both surface in the live /inflight feed any
    # other tunnel client can read every 2.5s, allowing trivial
    # cross-agent lease hijack while the claiming session is still
    # running. Manifest.lease_token (set by pack_artefact) is the
    # one delivery path; only whoever fetches /outputs/<digest>.tar
    # sees it. Event records claim WITHOUT the token.
    session.log_event(
        "LEASE", "lease:claim",
        f"devices={sorted(devices)} duration_s={duration_s} "
        f"(token in manifest)")


def _op_resume(session, h, args):
    # _prescan_lease_resume in session.py validates the token at
    # session start (before lock acquisition). At op time we just
    # log -- and we redact the token in the log line for the same
    # reason claim does (the resume-token would otherwise let a
    # second tunnel client double-resume the same lease).
    session.log_event("LEASE", "lease:resume", "(token redacted)")


def _op_release(session, h, args):
    token = _lease_token_from(args)
    if token is None:
        # Default to the session's own lease.
        token = getattr(session, "lease_token", None)
    if token is None:
        session.log_event("LEASE", "lease:release", "no-op (no token)")
        return
    dropped = session.registry.lease_release(token)
    # Don't emit token in the event -- a still-live token could be
    # learned by a tunnel client from /inflight even on the release
    # tick (a /inflight publish racing the unlink).
    session.log_event(
        "LEASE", "lease:release",
        f"devices={sorted(dropped)}")


def _op_list(session, h, args):
    # Strip tokens from the listing -- the listing is published into
    # the lease.list stream and that stream tail goes into /inflight.
    # Keeping device sets + remaining-time exposes everything an
    # operator needs to see "what's locked" without leaking a
    # credential another agent could resume against.
    import json as _json
    leases = [
        {k: v for k, v in entry.items() if k != "token"}
        for entry in session.registry.lease_list()
    ]
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
            optional_args={"auto_release_on_session_end": "bool"},
            doc=("Claim devices=\"dsp.A,fpga.hx1k\" duration_s=600. "
                 "The issued token is delivered ONLY in the artefact's "
                 "manifest.json under \"lease_token\" -- never in "
                 "streams or timeline events, so it doesn't leak via "
                 "the live /inflight feed to other tunnel clients. "
                 "Fetch the artefact, read manifest.json, pass the "
                 "token to subsequent `resume` ops. Pass "
                 "auto_release_on_session_end=true for one-plan "
                 "workflows where you want the bench unlocked on "
                 "session end (the default is to hold across plans)."),
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
