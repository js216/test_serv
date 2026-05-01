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
    """Decode ``device=...`` into a registry key.

    Accepts:
      * a plain plugin name (e.g. ``mp135``)  -- must currently
        resolve to a unique instance via registry.resolve; lease
        applies to that resolved key.
      * a fully-qualified ``plugin.id`` key (e.g. ``msc.evb``) --
        the *plugin* must be loaded but the instance does NOT need
        to currently exist. The lease becomes "dormant": stored in
        the registry's lease table, but with no immediate effect
        until the device enumerates. When it does, foreign agents'
        lock acquires are blocked.

    Dormant claims are how an agent reserves a future-mode device
    key for the same physical board across firmware swaps -- e.g.
    pre-claim ``msc.evb`` while the SoC is still in DFU, so no
    one else can grab the freshly-appeared MSC drive between flash
    and the agent's follow-up plan.
    """
    raw = args.get("device")
    if not raw:
        raise ValueError("missing device=...")
    if "." not in raw:
        return registry.resolve(raw)
    plugin_name, _, spec_id = raw.partition(".")
    if plugin_name not in registry.plugins:
        raise ValueError(f"unknown plugin {plugin_name!r}")
    if not spec_id:
        raise ValueError(f"empty spec id in {raw!r}")
    return raw


def _publish_now(session):
    """Best-effort: push fresh status to the server so the dashboard
    reflects this op's effect without waiting for the 15 s tick."""
    publish = getattr(session.registry, "publish_status", None)
    if callable(publish):
        try:
            publish()
        except Exception:
            pass


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
    _publish_now(session)


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
    _publish_now(session)


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
        "`lease:claim device=plugin.id duration_s=N` (repeat per "
        "device in the same plan to grow the lease set). The token is "
        "written to stream `lease.claim` in the artefact tarball. In "
        "subsequent plans B, C, ..., make `lease:resume token=<tok>` "
        "the FIRST op to take ownership again -- the session's "
        "device-lock acquire skips the lease gate and proceeds. When "
        "done, `lease:release token=<tok>` drops the hold. Leases "
        "auto-expire at min(duration_s, MAX_SESSION_S=3600s) "
        "regardless. Other agents whose plans touch a leased device "
        "get a fast BusyError.\n"
        "\n"
        "DORMANT CLAIMS for devices that don't currently exist:\n"
        "  When `device=plugin.id` is fully qualified (a dot is in the "
        "value), the plugin must be loaded but the *instance* does NOT "
        "need to be currently enumerated. The lease is recorded "
        "anyway; the moment the device appears (e.g. after a firmware "
        "swap or reset), the lease starts gating other agents'\n"
        "acquisitions of that key.\n"
        "\n"
        "  This is how you reserve a future-mode key for the same "
        "physical board across firmware changes. Example for the EVB "
        "STM32MP135, which presents different USB device keys in "
        "different modes (`dfu.evb`, `msc.evb`, `mp135.evb`, "
        "`ssh.target`):\n"
        "\n"
        "    # Plan A: pre-claim every key the workflow will touch,\n"
        "    # even ones that don't exist yet (board is in DFU now).\n"
        "    lease:claim device=dfu.evb    duration_s=1800\n"
        "    lease:claim device=msc.evb    duration_s=1800  # dormant\n"
        "    lease:claim device=mp135.evb  duration_s=1800\n"
        "    lease:claim device=ssh.target duration_s=1800  # dormant\n"
        "    dfu.evb:flash_layout layout=@flash.tsv\n"
        "    # Token T now covers all four keys.\n"
        "\n"
        "    # Plan B (same agent): bootloader is up, msc.evb is\n"
        "    # enumerated -- our lease already covers it.\n"
        "    lease:resume token=<T>\n"
        "    msc.evb:write data=@sdcard.img\n"
        "\n"
        "  Plain `device=plugin` (no dot) still requires the instance "
        "to currently exist and be unique -- it resolves via the "
        "registry exactly like a normal device op. Use the qualified "
        "`plugin.id` form whenever multiple instances are configured "
        "(see the bench's current device list for available ids).")

    ops = {
        "claim": Op(
            args={"device": "ident", "duration_s": "int"},
            doc=("Add `device` (plugin or plugin.id) to this session's "
                 "lease for up to duration_s seconds (clamped to "
                 "MAX_SESSION_S). First call mints the token; later "
                 "calls in the same session add to the same token. "
                 "The token is appended to stream `lease.claim` "
                 "(JSON).\n"
                 "\n"
                 "Plain `plugin` requires the instance to currently "
                 "exist and be unique. Fully-qualified `plugin.id` "
                 "(with a dot) records a DORMANT lease even when the "
                 "instance is not currently enumerated -- gates "
                 "future acquisitions the moment the device appears. "
                 "Use this to reserve a key across firmware swaps "
                 "(see plugin doc)."),
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
