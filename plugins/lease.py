# SPDX-License-Identifier: MIT
# lease.py --- Cross-session device claims for extended-debug workflows
# Copyright (c) 2026 Jakob Kastelic

import json
import uuid

from plugin import DevicePlugin, Op


class LeaseHandle:
    """Synthetic handle. The lease plugin has no hardware backing -- it
    exposes the registry's lease registry as a plan-level interface."""

    def __init__(self):
        self._identity_verified = True


def _get_token(args, key="token"):
    v = args.get(key)
    if v is None or not isinstance(v, str):
        raise ValueError(f"missing {key!r}")
    return v


def _resolve_device_arg(args, registry):
    """Decode ``device=ident`` into a registry key. Accepts either
    ``plugin.id`` directly (e.g. ``mp135.0``) or just ``plugin``
    (delegated to ``registry.resolve`` to require uniqueness).
    """
    raw = args.get("device")
    if not raw:
        raise ValueError("missing device=...")
    if "." in raw:
        plugin_name, _, spec_id = raw.partition(".")
        return registry.resolve(plugin_name, spec_id)
    return registry.resolve(raw)


def _op_claim(session, h, args):
    key = _resolve_device_arg(args, session.registry)
    duration_s = float(args["duration_s"])
    if session.lease_token is None:
        session.lease_token = f"lease-{uuid.uuid4().hex[:16]}"
    expires_at = session.registry.lease_add(
        session.lease_token, key, duration_s)
    session.log_event(
        "LEASE", "lease:claim",
        f"token={session.lease_token!r} +{key} expires_at={expires_at:.1f}")
    payload = {
        "token": session.lease_token,
        "device": key,
        "expires_at_monotonic": expires_at,
    }
    session.stream("lease.claim").append(
        (json.dumps(payload, indent=2) + "\n").encode())


def _op_resume(session, h, args):
    # Real validation runs in Session._prescan_lease_resume so the lease
    # gate sees the token before any locks are taken; this op-time path
    # exists for plan readability and for the timeline log.
    token = _get_token(args)
    if session.lease_token != token:
        raise RuntimeError(
            f"lease:resume must be the FIRST op in the plan; "
            f"session token is {session.lease_token!r}")
    held = session.registry.lease_resume(token)
    if held is None:
        raise RuntimeError(f"lease {token!r} expired between prescan and op")
    session.log_event(
        "LEASE", "lease:resume",
        f"token={token!r} devices={sorted(held)}")


def _op_release(session, h, args):
    token = _get_token(args)
    devices = session.registry.lease_drop(token)
    session.log_event(
        "LEASE", "lease:release",
        f"token={token!r} freed={sorted(devices)}")
    session.stream("lease.release").append(
        (json.dumps({"token": token,
                     "freed_devices": sorted(devices)}, indent=2)
         + "\n").encode())


def _op_list(session, h, args):
    snapshot = session.registry.lease_list()
    session.stream("lease.list").append(
        (json.dumps(snapshot, indent=2) + "\n").encode())
    session.log_event(
        "LEASE", "lease:list", f"{len(snapshot)} active")


class LeasePlugin(DevicePlugin):
    name = "lease"
    doc = (
        "Hold one or more devices across multiple plan submissions for "
        "extended debugging. Workflow: in plan A, run "
        "`lease:claim device=plugin.id duration_s=N` (repeat per device "
        "in the same plan to grow the lease set). The token is written "
        "to stream `lease.claim` in the artefact tarball. In subsequent "
        "plans B, C, ..., make `lease:resume token=<tok>` the FIRST op "
        "to take ownership again -- the session's device-lock acquire "
        "skips the lease gate and proceeds. When done, `lease:release "
        "token=<tok>` drops the hold. Leases auto-expire at "
        "min(duration_s, MAX_SESSION_S=3600s) regardless. Other agents "
        "whose plans touch a leased device get a fast BusyError.")

    ops = {
        "claim": Op(
            args={"device": "ident", "duration_s": "int"},
            doc=("Add `device` (plugin.id) to this session's lease for "
                 "up to duration_s seconds (clamped to MAX_SESSION_S). "
                 "First call mints the token; later calls in the same "
                 "session add to the same token. The token is appended "
                 "to stream `lease.claim` (JSON)."),
            run=_op_claim),
        "resume": Op(
            args={"token": "str"},
            doc=("Take over an existing token's lease. MUST be the "
                 "first op in the plan; raises if not."),
            run=_op_resume),
        "release": Op(
            args={"token": "str"},
            doc=("Free every device held by `token`. Idempotent; an "
                 "unknown/expired token returns an empty set in stream "
                 "`lease.release`."),
            run=_op_release),
        "list": Op(
            args={},
            doc=("Snapshot all active leases (token, devices, "
                 "expires_in_s) into stream `lease.list`."),
            run=_op_list),
    }

    def probe(self):
        return [{"id": "manager"}]

    def open(self, spec):
        return LeaseHandle()

    def close(self, handle):
        pass
