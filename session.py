# SPDX-License-Identifier: MIT
# session.py --- Execute a parsed plan: ops, streams, artefact tar
# Copyright (c) 2026 Jakob Kastelic

import io
import json
import os
import re
import tarfile
import threading
import time
import traceback
import uuid
from datetime import datetime

from plan import PlanError
from plugin import Op as OpSchema
from plugin import BusyError, decode_args


DEFAULT_SESSION_S = 600.0
MAX_SESSION_S = 3600.0


def make_manifest(*, status, t0_monotonic, t0_wall, runtime_s,
                  streams, n_ops, n_errors, required_devices,
                  expectations=None, message=None,
                  files=None, run_id=None, plan_digest=None,
                  code_digest=None, blob_digests=None,
                  lease_token=None, checks=None, inert_reason=None):
    """Single source of truth for the artefact ``manifest.json``
    shape. Both the session's success path and the poller's failure-
    artefact path emit through here so the two never drift on field
    names or types.

    ``status`` is the discriminator:
        "ok"        n_errors=0 and at least one check ran
        "inert"     n_errors=0 but the plan made no machine-checkable
                    claim (no *_expect, no scope:capture summary, no
                    msc:verify, etc.) -- the run survived but proved
                    nothing
        "errors"    n_errors > 0
        "failed"    poller refused before run_all even started
                    (parse error, validation, lease conflict)
        "canceled"  signal_cancel landed mid-run
    ``checks[]`` carries machine-readable assertion records (hit /
    miss / timeout per *_expect or similar), distinct from
    ``expectations[]`` which is free-form claims set by the
    ``expect`` plan verb.
    """
    iso = datetime.fromtimestamp(t0_wall).isoformat(timespec="milliseconds")
    out = {
        "status": status,
        "t0_monotonic": t0_monotonic,
        "t0_wall_iso": iso,
        "t0_wall_unix": t0_wall,
        "runtime_s": runtime_s,
        "streams": sorted(streams),
        "files": sorted(files or []),
        "n_ops": n_ops,
        "n_errors": n_errors,
        "required_devices": sorted(required_devices),
        "expectations": list(expectations or []),
        "checks": list(checks or []),
        "bench_id": os.environ.get("TEST_SERV_BENCH_ID"),
        "run_id": run_id,
        "plan_digest": plan_digest,
        "code_digest": code_digest,
        "blob_digests": dict(blob_digests or {}),
    }
    if lease_token is not None:
        out["lease_token"] = lease_token
    if inert_reason is not None:
        out["inert_reason"] = inert_reason
    if message is not None:
        out["message"] = message
    return out


class StopSession(Exception):
    """Raised by a plan op (currently uart_expect with end_session=true)
    to terminate the rest of the plan cleanly. _run_block catches it
    at the top level and treats it as a normal end-of-plan, not an
    error or a cancellation.
    """


# Per-stream cap: a runaway UART (kernel-panic loop, baud-rate flood)
# could otherwise grow Stream.records without bound and OOM the bench
# host before the session deadline expires. 64 MiB is well above any
# legitimate per-stream payload (scope.csv at 5000 points, full DSP
# UART bursts, big PRBS mismatch logs) and keeps a multi-stream
# session under the 256 MiB artefact upload cap.
STREAM_MAX_BYTES = 64 * 1024 * 1024


class Stream:
    """A time-stamped byte stream.

    Background-filler threads append ``(t_ns, bytes)`` records via
    ``append()``; ``snapshot()`` copies out the current contents
    without stopping collection. ``close()`` seals further appends.

    Capped at STREAM_MAX_BYTES total per stream; once the cap is
    hit, oldest records are dropped to make room for new ones, with
    a one-shot ``[STREAM TRUNCATED, dropped Nb]`` marker injected so
    a reader knows what happened.
    """
    def __init__(self, name, t0):
        self.name = name
        self.t0 = t0
        self.records = []
        self.lock = threading.Lock()
        self.closed = False
        # Running byte total to avoid an O(n) sum on every append.
        self._size = 0
        self._dropped = 0

    def append(self, data):
        if not data:
            return
        t = time.monotonic() - self.t0
        with self.lock:
            if self.closed:
                return
            self.records.append((t, bytes(data)))
            self._size += len(data)
            while self._size > STREAM_MAX_BYTES and len(self.records) > 1:
                _, dropped = self.records.pop(0)
                self._size -= len(dropped)
                self._dropped += len(dropped)
            # Truncation marker is rendered on every snapshot rather
            # than stored in records[]: a previous version inserted a
            # marker record at index 0 on the first cap-hit, but the
            # next eviction silently dropped that marker (it was the
            # oldest record), and the sticky _truncation_warned flag
            # never re-inserted it. Net result: a stream that
            # truncated multiple times rendered without any marker.
            # Now we just track the dropped byte count; snapshot()
            # synthesizes the marker.

    def close(self):
        with self.lock:
            self.closed = True

    def snapshot_bytes(self):
        with self.lock:
            data = b"".join(d for _, d in self.records)
            if self._dropped:
                marker = (f"[STREAM TRUNCATED: dropped {self._dropped} "
                          f"oldest bytes; cap={STREAM_MAX_BYTES}B]\n"
                          ).encode()
                data = marker + data
            return data

    def tail_bytes(self, n):
        """Return the last ``n`` bytes of this stream without
        materializing the full payload first. _publish_inflight calls
        this every 2.5s while a session is live; with a 64 MiB stream
        the naive ``snapshot_bytes()[-n:]`` allocates 64 MiB per tick
        and stalls UART drain threads on the lock for tens of ms.
        """
        out = []
        remaining = n
        with self.lock:
            for _, data in reversed(self.records):
                if remaining <= 0:
                    break
                if len(data) >= remaining:
                    # data is already an immutable bytes from append();
                    # slicing returns a fresh bytes object, no extra
                    # copy needed.
                    out.append(data[-remaining:])
                    remaining = 0
                else:
                    out.append(data)
                    remaining -= len(data)
        return b"".join(reversed(out))

    def snapshot_timestamped(self):
        with self.lock:
            recs = list(self.records)
            if self._dropped:
                marker = (f"[STREAM TRUNCATED: dropped {self._dropped} "
                          f"oldest bytes; cap={STREAM_MAX_BYTES}B]\n"
                          ).encode()
                recs.insert(0, (0.0, marker))
            return recs


class Session:
    """Run-time state of a single job.

    Holds the device registry, live streams, the shared monotonic
    ``t0``, and the per-op log. Op ``run`` callables receive ``self``
    as the first argument plus a device handle and decoded args.
    """
    def __init__(self, registry, plan, runtime_s=None):
        self.registry = registry
        self.plan = plan
        self.runtime_s = runtime_s
        self.t0 = time.monotonic()
        # Wall-clock anchor so timeline.log + manifest can be
        # correlated to lab clocks (dmesg, syslog, oscilloscope
        # screenshots) at 3am. monotonic alone is meaningless to
        # a human reading an old artefact.
        self.t0_wall = time.time()
        self.streams = {}
        self.events = []       # list of dicts for timeline.log
        self.ops_log = []      # list of dicts for ops.jsonl
        self.pinned = {}       # device_key -> context manager (for open/close)
        self.touched_keys = set()
        self.errors = []
        # Plan-level claims recorded by the `expect "<text>"` control
        # verb. Surface as manifest.expectations[] so a future operator
        # reading an artefact can see what the plan was *asserting*,
        # not just what bytes happened to flow.
        self.expectations = []
        # Inventory snapshots: when an `inventory` op runs it sets
        # these dicts; pack_artefact emits them as bench.devices.json
        # and bench.ops.json files in the tar. Old code wrote them
        # into Stream objects, which forced Stream.replace() to exist
        # for one caller; that's gone now.
        self.bench_devices = None
        self.bench_ops = None
        # If the plan starts with `lease:resume token=...`, the
        # session's lock-acquire phase treats the token's held
        # devices as owned (so lease_blocks_acquire returns None
        # for our own token), and lease:claim sets this on success
        # so a same-plan release defaults to it.
        self.lease_token = None
        # True iff lease_token was issued by THIS session (lease:claim);
        # False if we just resumed someone else's claim. Only the
        # claiming run's manifest publishes the token.
        self.lease_just_claimed = False
        # True iff lease:claim auto_release_on_session_end=true was
        # set; the finally block drops the lease on session end.
        self.lease_release_on_end = False
        # Plan/run identity, set by the dispatcher before run_all.
        # Surfaces in manifest.json so an aggregator can group runs
        # of the same plan without parsing plan.txt.
        self.plan_digest = None
        self.code_digest = None
        # Machine-readable check records appended by *_expect ops
        # and other plugins that want to assert something. Each entry
        # is a dict {kind, target, claim, status, evidence}.
        # Distinct from self.expectations (free-form prose set by the
        # `expect` control verb).
        self.checks = []
        self.lock = threading.Lock()
        self.session_id = f"sess-{uuid.uuid4().hex[:12]}"
        # Set by signal_cancel(). _run_block tests it at op boundaries;
        # long-blocking ops (delay, *_expect polling loops) wait on
        # cancel_event so they wake up immediately on cancel instead
        # of running their full timeout. Subprocess-based ops
        # (dfu cubeprog flash, large msc/fpga MPSSE I/O) still don't
        # observe the signal until the op returns.
        self.canceled = False
        self.cancel_event = threading.Event()

    # --- timeline recording ---

    def log_event(self, kind, source, msg):
        t = time.monotonic() - self.t0
        with self.lock:
            self.events.append({
                "t": t, "kind": kind, "source": source, "msg": msg,
            })

    def stream(self, name):
        with self.lock:
            s = self.streams.get(name)
            if s is None:
                s = Stream(name, self.t0)
                self.streams[name] = s
        return s

    def record_check(self, kind, target, claim, status, evidence=None):
        """Append a structured check record. Plugins call this when an
        op makes (or fails to make) a machine-checkable assertion.

        ``kind`` -- short verb-like string, e.g. "uart_expect",
            "scope_summary", "flash_verify".
        ``target`` -- device key the check ran against, or "" for
            session-global.
        ``claim`` -- one-line text describing what we asserted.
        ``status`` -- "hit" / "miss" / "timeout" / "ok" / "fail".
        ``evidence`` -- optional small dict (matched bytes, count,
            duty %), small enough to live in the manifest.
        """
        rec = {
            "kind": kind, "target": target, "claim": claim,
            "status": status,
            "t": time.monotonic() - self.t0,
        }
        if evidence is not None:
            rec["evidence"] = evidence
        with self.lock:
            self.checks.append(rec)
        self.log_event(
            "CHECK", f"{target}:{kind}" if target else kind,
            f"{status}: {claim}")

    def signal_early_done(self, reason=""):
        # Plugins (uart_expect with end_session=true) raise this to
        # short-circuit the rest of the plan. _run_block catches it
        # at the top level and treats it as clean termination.
        raise StopSession(reason)

    def bail_if_canceled(self, where):
        """Plugin helper: raise RuntimeError if the session is canceled.
        Cheaper to call once per chunk than to manage a local helper.
        """
        if self.canceled:
            raise RuntimeError(
                f"{where} canceled via DELETE /jobs/<digest>")

    def signal_cancel(self):
        """Request the session abort. Idempotent and thread-safe.
        Called from the poller's cancel-marker drain when DELETE
        /jobs/<digest> arrives mid-flight. Sets self.canceled (which
        _run_block tests at op boundaries) and self.cancel_event
        (which long-blocking ops should wait on instead of using
        plain time.sleep, so they wake immediately on cancel).
        """
        self.canceled = True
        self.cancel_event.set()
        self.log_event("CTRL", "session", "cancel requested")

    def _prescan_lease_resume(self):
        """If the plan contains ``lease:resume token=...`` anywhere,
        validate the token against the registry's live lease table and
        bind ``self.lease_token`` BEFORE the eager-acquire path runs.
        The binding makes ``lease_blocks_acquire`` return None for our
        own held devices, so we can re-acquire the locks the previous
        session released. A bad / expired token raises ``BusyError``.

        We scan the whole plan, not just op[0], so a natural
        ``description "..."`` line above ``lease:resume`` doesn't
        silently skip the binding -- which would then fail eager
        acquire with a confusing "device is leased to '...'" error.
        Only the first ``lease:resume`` is honored; subsequent ones
        are no-ops at op time anyway.
        """
        op = next((o for o in self.plan.ops
                   if o.device == "lease" and o.verb == "resume"),
                  None)
        if op is None:
            return
        v = op.args.get("token")
        if v is None:
            raise BusyError("lease:resume requires token=...")
        token = v.raw if hasattr(v, "raw") else str(v)
        held = self.registry.lease_resume(token)  # raises on bad token
        self.lease_token = token
        # Don't include the token in the event message -- the live
        # /inflight feed exposes events to any tunnel client every
        # 2.5s, and the token is the lease credential. Same redaction
        # rule as plugins/lease.py:_op_resume.
        self.log_event(
            "LEASE", "session",
            f"resumed devices={sorted(held)} (token redacted)")

    # --- execution ---

    def run_all(self, plugins):
        # Agent picks down via X-Test-Runtime, bench enforces up via
        # MAX_SESSION_S so a rogue agent can't camp on a device.
        budget_s = min(self.runtime_s or DEFAULT_SESSION_S, MAX_SESSION_S)
        deadline = self.t0 + budget_s
        # If the plan starts with `lease:resume token=...`, validate
        # the token now so the eager-acquire path treats the lease's
        # devices as ours. A bad/expired token fails the session
        # before any locks are touched, and other agents racing to
        # grab the held device fast-fail via lease_blocks_acquire.
        try:
            self._prescan_lease_resume()
        except BusyError as e:
            self.errors.append(traceback.format_exc())
            self.log_event("ERROR", "session", f"lease: {e}")
            for s in self.streams.values():
                s.close()
            return
        # Job-atomic device locking: grab every device the plan
        # references up front and hold the locks for the whole session.
        # A job that needs {dsp, fpga} therefore pauses any other job
        # that touches dsp or fpga until it finishes -- no surprise
        # interleaving between a multi-device test's individual ops.
        # Keys are sorted for a consistent global acquisition order so
        # two jobs sharing a subset of devices cannot deadlock each
        # other.
        #
        # Some devices may not yet be present at session start (e.g. a
        # plan that resets MP135 into DFU *then* talks to dfu). For
        # each needed plugin we do a targeted probe; devices that
        # resolve now get locked here, devices that don't become
        # "deferred" and get locked lazily the first time an op for
        # them runs (_run_device_op handles that path). Deferred locks
        # are held for the rest of the session, same as eager ones --
        # so the overall semantics is still job-atomic once the
        # device shows up.
        from plan import required_devices
        needed = sorted(required_devices(self.plan))
        for name in needed:
            self.registry.refresh_plugin(name)

        eager_keys = []
        self._deferred_names = set()
        for name in needed:
            try:
                eager_keys.append(self.registry.resolve(name))
            except LookupError:
                self._deferred_names.add(name)
        with self.registry.lock:
            eager_locks = [
                self.registry.per_dev_lock.setdefault(k, threading.RLock())
                for k in eager_keys]
            # Pin the eager keys so background refresh can't drop the
            # spec mid-session if the device transiently disappears
            # from probe (USB blip, mode change). Deferred keys are
            # added in _resolve_device when they show up.
            self._pinned_specs = set(eager_keys)
            for k in self._pinned_specs:
                self.registry.pinned_specs[k] += 1
        self._deferred_locks = []     # filled by _run_device_op

        self.log_event(
            "LOCK", "session",
            f"acquire now={eager_keys}  "
            f"deferred={sorted(self._deferred_names)}"
            if eager_keys or self._deferred_names else "(no devices)")
        # Lease check: if any eager key is leased to a different
        # token, fast-fail before acquiring locks. A competing plan
        # gets a clean BusyError instead of blocking on the per-
        # device RLock for the whole lease window.
        for k in eager_keys:
            blocker = self.registry.lease_blocks_acquire(k, self.lease_token)
            if blocker is not None:
                msg = (f"{k} is leased to {blocker!r}; "
                       f"resume that lease or wait for it to expire")
                self.errors.append(msg + "\n")
                self.log_event("ERROR", "session", msg)
                with self.registry.lock:
                    for kk in self._pinned_specs:
                        n = self.registry.pinned_specs.get(kk, 0) - 1
                        if n > 0:
                            self.registry.pinned_specs[kk] = n
                        else:
                            self.registry.pinned_specs.pop(kk, None)
                for s in self.streams.values():
                    s.close()
                return
        eager_acquired = []
        try:
            for lk in eager_locks:
                lk.acquire()
                eager_acquired.append(lk)
        except Exception:
            for held in reversed(eager_acquired):
                held.release()
            self.errors.append(traceback.format_exc())
            self.log_event(
                "ERROR", "session",
                f"lock acquire: {self.errors[-1].splitlines()[-1]}")
            for s in self.streams.values():
                s.close()
            return
        # Track which RESOLVED KEYS are currently locked for the whole
        # session. Eager keys are in by definition; deferred resolves
        # add to this set as ops fire. Gating on resolved keys (not on
        # plugin names) is what makes a multi-instance plan like
        # `fpga.hx1k:program` then `fpga.hx8k:program` actually hold
        # both dev_locks for the run. The previous implementation
        # discarded the plugin NAME after the first deferred resolve,
        # so any second instance of the same plugin only had its
        # dev_lock for the duration of each individual op -- a parallel
        # session could win the lock between ops, breaking the
        # job-atomic invariant the README promises.
        self._session_locked_keys = set(eager_keys)
        self.log_event("LOCK", "session", "acquired; running ops")
        try:
            self._run_block(self.plan.ops, plugins, deadline)
        except StopSession as e:
            # Plan asked to short-circuit (uart_expect end_session=true).
            # Clean termination, not an error.
            self.log_event("CTRL", "session",
                           f"early_done: {e.args[0] if e.args else ''}")
        except Exception:
            self.errors.append(traceback.format_exc())
            self.log_event("ERROR", "session",
                           f"top-level: {self.errors[-1].splitlines()[-1]}")
        finally:
            for key, cm in list(self.pinned.items()):
                try:
                    cm.__exit__(None, None, None)
                except Exception:
                    pass
            self.pinned.clear()
            # release_now closes each touched device's cached handle
            # (refs are zero by now -- pinning released above, op-level
            # acquires released as each op returned). Each plugin's
            # close() does its own pre-close flush, so a canceled
            # session tears down cleanly without a separate cleanup
            # hook.
            for key in sorted(self.touched_keys):
                try:
                    if self.registry.release_now(key):
                        self.log_event("CLOSE", "session", key)
                except Exception:
                    traceback.print_exc()
            # Drop the spec-pin so a later refresh can finally evict
            # any device that vanished. Done after release_now so the
            # eviction path doesn't race with our close.
            with self.registry.lock:
                for k in getattr(self, "_pinned_specs", set()):
                    n = self.registry.pinned_specs.get(k, 0) - 1
                    if n > 0:
                        self.registry.pinned_specs[k] = n
                    else:
                        # Drop the key entirely so refresh's
                        # `key not in pinned_specs` check is true,
                        # not just zero-but-present.
                        self.registry.pinned_specs.pop(k, None)
            for s in self.streams.values():
                s.close()
            # If the lease was claimed in this session with
            # auto_release_on_session_end=true, drop the lease now
            # so the bench unlocks on session end rather than
            # holding for the full duration_s. Default is the
            # original cross-session-hold behaviour.
            if (getattr(self, "lease_release_on_end", False)
                    and self.lease_token is not None):
                self.registry.lease_release(self.lease_token)
                self.log_event("LEASE", "session",
                               "auto-released on session end")
            # Release deferred locks (acquired mid-session) first, then
            # eager locks. Reverse of acquisition order in both lists.
            for lk in reversed(self._deferred_locks):
                lk.release()
            for lk in reversed(eager_locks):
                lk.release()
            self.log_event("LOCK", "session", "released")

    def _run_block(self, ops, plugins, deadline):
        for op in ops:
            if time.monotonic() > deadline:
                self.log_event("ERROR", "session",
                               f"session exceeded {deadline - self.t0:.0f}s "
                               f"deadline")
                return
            if self.canceled:
                # Record where the cancel landed, with elapsed time.
                # The last completed op in ops_log gives the operator
                # the "what was actually running when the cancel
                # arrived" context they want at 3am.
                elapsed = time.monotonic() - self.t0
                last = self.ops_log[-1] if self.ops_log else None
                last_str = (
                    f"; last completed op: line {last['line']} "
                    f"{last['device'] or 'ctrl'}:{last['verb']} "
                    f"({last['status']})"
                    if last else "")
                msg = (f"canceled at op {op.lineno} "
                       f"({op.device or 'ctrl'}:{op.verb}) "
                       f"after {elapsed:.1f}s{last_str}")
                self.log_event("ERROR", "session", msg)
                self.errors.append(msg + "\n")
                return
            self._run_one(op, plugins, deadline)

    def _run_one(self, op, plugins, deadline):
        t0 = time.monotonic() - self.t0
        rec = {"line": op.lineno, "device": op.device, "verb": op.verb,
               "t_start": t0, "t_end": None, "status": "ok", "err": None}
        try:
            if op.device is None:
                self._run_control(op, plugins, deadline)
            else:
                self._run_device_op(op, plugins)
        except StopSession:
            # Plan asked to short-circuit (uart_expect end_session=true).
            # Record the op as ok and let the exception propagate to
            # _run_block / run_all so the rest of the plan stops cleanly.
            rec["t_end"] = time.monotonic() - self.t0
            self.ops_log.append(rec)
            self.log_event(
                "OP", f"{op.device or 'ctrl'}:{op.verb}",
                f"ok ({(rec['t_end']-rec['t_start'])*1e3:.1f} ms) "
                f"[end_session]")
            raise
        except Exception as e:
            rec["status"] = "error"
            rec["err"] = f"{type(e).__name__}: {e}"
            self.errors.append(traceback.format_exc())
            self.log_event("ERROR",
                           f"{op.device or 'ctrl'}:{op.verb}",
                           rec["err"])
        rec["t_end"] = time.monotonic() - self.t0
        self.ops_log.append(rec)
        if rec["status"] == "ok":
            self.log_event(
                "OP",
                f"{op.device or 'ctrl'}:{op.verb}",
                f"ok ({(rec['t_end']-rec['t_start'])*1e3:.1f} ms)",
            )

    # --- device ops ---

    def _resolve_device(self, device_ref):
        """Resolve with one targeted re-probe on miss.

        ``device_ref`` is either a bare plugin name (``mp135``) when
        the plan has a unique instance, or a fully-qualified
        ``plugin.id`` form (``mp135.evb``) when the plan needs to
        target one specific instance among several.

        Lets plans whose earlier ops caused a device to appear (DUT
        reset into DFU, board coming online, ...) find it at op time
        without waiting for the background refresh tick.  If the
        device is in the session's deferred set, this also promotes
        it to 'locked for the remainder of the session' so subsequent
        ops on the same device keep the job-atomic guarantee.
        """
        from plan import split_device_ref
        plugin_name, spec_id = split_device_ref(device_ref)
        try:
            key = self.registry.resolve(plugin_name, spec_id)
        except LookupError:
            self.registry.refresh_plugin(plugin_name)
            key = self.registry.resolve(plugin_name, spec_id)

        # Gate on the resolved KEY, not the plugin name. A plan that
        # touches two instances of the same plugin (e.g. fpga.hx1k +
        # fpga.hx8k) hits this path twice with different keys; both
        # need session-long locks to keep the job-atomic invariant.
        if key not in getattr(self, "_session_locked_keys", set()):
            blocker = self.registry.lease_blocks_acquire(
                key, self.lease_token)
            if blocker is not None:
                raise BusyError(
                    f"{key} is leased to {blocker!r}; resume that "
                    f"lease or wait for it to expire")
            with self.registry.lock:
                lk = self.registry.per_dev_lock.setdefault(
                    key, threading.RLock())
                # Pin the spec for the rest of the session, same as
                # eager keys, so refresh can't evict it on a transient
                # disappearance.
                self._pinned_specs.add(key)
                self.registry.pinned_specs[key] += 1
            lk.acquire()
            self._deferred_locks.append(lk)
            self._session_locked_keys.add(key)
            self._deferred_names.discard(plugin_name)
            self.log_event("LOCK", "session",
                           f"deferred acquire {key}")
        self.touched_keys.add(key)
        return key

    def _run_device_op(self, op, plugins):
        from plan import split_device_ref
        plugin_name, _ = split_device_ref(op.device)
        if plugin_name not in plugins:
            raise PlanError(f"unknown device {op.device!r}")
        plugin = plugins[plugin_name]

        if op.verb in ("open", "close"):
            key = self._resolve_device(op.device)
            if op.verb == "open":
                if key in self.pinned:
                    return
                cm = self.registry.acquire(key)
                cm.__enter__()
                # If anything between __enter__ and the pinned dict
                # store raises (asynchronous signal, MemoryError),
                # we'd leak the bumped refcount + held dev_lock with
                # no cm reference left to undo it. Tie the two
                # together so the handle is always reachable from
                # pinned, or rolled back.
                try:
                    self.pinned[key] = cm
                except BaseException:
                    cm.__exit__(None, None, None)
                    raise
                self.log_event("OPEN", op.device, key)
            else:
                cm = self.pinned.pop(key, None)
                if cm is not None:
                    cm.__exit__(None, None, None)
                    self.log_event("CLOSE", op.device, key)
            return

        if op.verb not in plugin.ops:
            raise PlanError(f"{op.device}: unknown op {op.verb!r}")
        schema = plugin.ops[op.verb]
        decoded = decode_args(schema, op.args, self.plan.blobs)

        key = self._resolve_device(op.device)
        if key in self.pinned:
            # Pinned: use the existing handle; don't open/close.
            handle = self.pinned[key].handle
            schema.run(self, handle, decoded)
        else:
            with self.registry.acquire(key) as handle:
                schema.run(self, handle, decoded)

    # --- control verbs ---

    def _run_control(self, op, plugins, deadline):
        v = op.verb
        if v == "mark":
            tag = op.args.get("tag")
            self.log_event("MARK", "ctrl",
                           tag.raw if tag is not None else "")
        elif v == "delay":
            ms = op.args.get("ms")
            if ms is None:
                raise PlanError("delay: missing ms=")
            # Wait on cancel_event instead of time.sleep so DELETE
            # /jobs/<digest> on a sleeping session aborts immediately.
            # Returns True if the event fires, False on timeout --
            # we don't care which since _run_block re-tests
            # self.canceled at the next iteration.
            self.cancel_event.wait(max(0.0, ms.as_int() / 1000.0))
        elif v == "inventory":
            self._run_inventory(op, plugins)
        elif v == "description":
            # Op-time no-op: the server already extracted the text
            # from the plan body at submit time and stuffed it into
            # the job's meta dict. Logging it to the timeline keeps
            # the artefact self-describing too.
            text = op.args.get("text")
            self.log_event(
                "DESC", "ctrl",
                text.raw if (text and hasattr(text, "raw")) else "")
        elif v == "expect":
            # Records a human-readable claim about what this plan
            # asserts. Surfaces in manifest.expectations[] so future
            # readers see the test's intent, not just the byte flow.
            text = op.args.get("text")
            claim = text.raw if (text and hasattr(text, "raw")) else ""
            if claim:
                self.expectations.append(claim)
            self.log_event("EXPECT", "ctrl", claim)
        else:
            raise PlanError(f"unknown control verb {v!r}")

    def _run_inventory(self, op, plugins):
        # inventory always refreshes the device list. Identity sweeps
        # (which actually open every device) live in POST /sweep so an
        # agent who just wants the device map doesn't pay for I/O on
        # every call.
        #
        # The poller's main loop pushes status to the server on a
        # 15s tick. inventory used to also publish on demand so the
        # dashboard refreshed faster, but having two refresh paths
        # made dashboards see stale data only sometimes; the tick is
        # the single source of truth now.
        self.registry.refresh()

        devices = self.registry.list_devices()
        # Build the _control plugin entry from the single source of
        # truth in plan.CONTROL_VERB_SPECS so a new control verb only
        # has to be defined once. Adding a verb means: update
        # CONTROL_VERB_SPECS in plan.py + add a case in _run_control;
        # the dashboard's Ops panel and the artefact's bench.ops.json
        # update automatically.
        from plan import CONTROL_VERB_SPECS
        control_ops = {
            verb: {
                "args": dict(spec["args"]),
                "optional_args": dict(spec["optional_args"]),
                "doc": spec["doc"],
            }
            for verb, spec in CONTROL_VERB_SPECS.items()
        }
        ops_map = {
            "_control": {
                "doc": (
                    "Plan control verbs handled by the session runner.\n"
                    "\n"
                    "Plan grammar reminders:\n"
                    "  device:op args...      run plugin op against a\n"
                    "                         currently-unique instance.\n"
                    "  device.id:op args...   run plugin op against a\n"
                    "                         specific instance, e.g.\n"
                    "                         `mp135.evb:uart_open` when\n"
                    "                         the bench has both\n"
                    "                         `mp135.evb` and\n"
                    "                         `mp135.custom`. The bare\n"
                    "                         `mp135:uart_open` form\n"
                    "                         errors with `ambiguous: 2\n"
                    "                         instances` when more than\n"
                    "                         one is configured.\n"
                    "  control-verb args...   no device prefix, e.g.\n"
                    "                         `delay ms=500` or\n"
                    "                         `inventory`.\n"
                    "\n"
                    "Available instance ids per plugin appear in this\n"
                    "session's bench.devices.json (each entry has both\n"
                    "an `id` like `mp135.evb` and a `description` if\n"
                    "the operator put one in config.json)."),
                "ops": control_ops,
            },
        }
        # Job-lifecycle / REST docs used to be jammed in here as a
        # fake "_jobs" plugin. They aren't plan ops, so they don't
        # belong in the inventory. Agents fetch them via GET /help
        # on the server instead -- see server.py:_help.
        for name, pl in sorted(plugins.items()):
            ops_map[name] = {
                "doc": pl.doc,
                "ops": {
                    op_name: {
                        "args": schema.args,
                        "optional_args": schema.optional_args or {},
                        "doc": schema.doc,
                    }
                    for op_name, schema in sorted(pl.ops.items())
                },
            }

        # Stash on the session; pack_artefact emits as bench.devices.json
        # and bench.ops.json files in the artefact tar. (Old code wrote
        # them into Stream objects and required a Stream.replace path
        # for one caller; that special case is gone now.) A second
        # `inventory` op overwrites these with the latest snapshot,
        # which is the intended semantics.
        self.bench_devices = devices
        self.bench_ops = ops_map
        self.log_event(
            "INVENTORY", "ctrl",
            f"devices={len(devices)} plugins={len(ops_map)}")
        # Record a check so the canonical first-plan walkthrough
        # (which is just `inventory`) produces manifest.status="ok"
        # rather than the operator-hostile "inert" badge. The probe
        # itself is a real assertion -- we proved that the bench's
        # plugin layer answered without error. Status uses the same
        # "hit"/"miss" vocabulary as *_expect ops.
        self.record_check(
            "inventory", "ctrl",
            "device probe ran without error",
            "hit",
            {"n_devices": len(devices), "n_plugins": len(ops_map)})


# ---- artefact packing ----

def render_timeline(session, bytes_budget_per_stream=8192):
    """Produce a single human-sortable timeline.log string from events +
    streams. Streams are inlined up to the per-stream byte budget; full
    bytes remain in streams/*.bin in the tar.
    """
    all_rows = []
    for ev in session.events:
        all_rows.append((ev["t"], f"{ev['kind']:<8} {ev['source']:<20} {ev['msg']}"))

    for name, stream in session.streams.items():
        inlined = 0
        for t, data in stream.snapshot_timestamped():
            if inlined >= bytes_budget_per_stream:
                break
            take = min(len(data), bytes_budget_per_stream - inlined)
            chunk = data[:take]
            inlined += take
            # Leading '>' marks stream-chunk rows so an operator can
            # cleanly grep events out of streams: `grep -v ' > '`.
            # Single repr() round so a literal backslash and a real
            # newline render distinguishably -- the manual
            # replace("\n","\\n") then repr() collapsed both 0x5C 0x6E
            # and 0x0A to the same characters in the timeline.
            printable = repr(chunk.decode("utf-8", errors="replace"))
            all_rows.append((t, f"> STREAM {name:<20} {printable}"))

    all_rows.sort(key=lambda r: r[0])
    # Render every row twice: a session-relative seconds offset (cheap
    # to scan visually for relative timing) plus the wall-clock ISO
    # timestamp (so an oncall engineer can correlate to dmesg /
    # syslog / scope screenshots taken at the same wall time).
    wall = session.t0_wall
    lines = []
    for t, body in all_rows:
        iso = datetime.fromtimestamp(wall + t).isoformat(
            timespec="milliseconds")
        lines.append(f"{iso}  {t:8.3f}  {body}")
    return "\n".join(lines) + "\n"


_STREAM_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_stream_name(name):
    # Defence in depth: stream names today come from hardcoded literals
    # in plugins, but if a future op ever passed a user string into
    # session.stream(name) we don't want artefact-tar path traversal.
    safe = _STREAM_NAME_RE.sub("_", name).lstrip(".") or "_unnamed"
    return safe


def pack_artefact(session):
    """Build the .tar artefact. Returns (tar_bytes, manifest_text)."""
    buf = io.BytesIO()
    import hashlib as _hashlib
    from plan import required_devices as _req_devs
    n_errors = len(session.errors)
    # Top-level files in the tar that aren't streams.
    files = ["manifest.json", "timeline.log", "ops.jsonl", "plan.txt"]
    if n_errors:
        files.append("errors.log")
    if session.bench_devices is not None:
        files.append("bench.devices.json")
    if session.bench_ops is not None:
        files.append("bench.ops.json")
    # Per-blob digests so a six-month-old artefact is reproducible:
    # plan.txt says `@blink.ldr`, manifest.blob_digests says which
    # blink.ldr that was.
    blob_digests = {
        name: _hashlib.sha256(data).hexdigest()
        for name, data in (getattr(session.plan, "blobs", {}) or {}).items()
    }
    # Status discriminator: "canceled" vs "errors" vs "inert" vs
    # "ok". "canceled" wins over "errors" so a script reading the
    # manifest can distinguish "operator gave up mid-run" from "DUT
    # misbehaved". The cancel reason still lives in errors.log for
    # debug context. A run that produced no machine-checkable claim
    # (no *_expect, no scope:capture, no msc:verify, ...) is "inert"
    # -- it survived but proved nothing. The reader who scripts on
    # `status == "ok"` then knows the run actually checked something.
    if session.canceled:
        status = "canceled"
        inert_reason = None
    elif n_errors:
        status = "errors"
        inert_reason = None
    elif not session.checks:
        status = "inert"
        # Self-explaining artefact: tell a future reader why the run
        # is inert so they don't have to grep the README to find out
        # what "inert" means.
        inert_reason = (
            "no machine-checkable assertions ran -- "
            "add a *:uart_expect / scope:capture / msc:verify or "
            "another op that calls session.record_check() to make "
            "the run produce a verifiable result")
    else:
        status = "ok"
        inert_reason = None
    manifest = make_manifest(
        status=status,
        t0_monotonic=session.t0,
        t0_wall=session.t0_wall,
        runtime_s=time.monotonic() - session.t0,
        streams=list(session.streams.keys()),
        files=files,
        n_ops=len(session.ops_log),
        n_errors=n_errors,
        required_devices=_req_devs(session.plan),
        expectations=session.expectations,
        checks=session.checks,
        run_id=session.session_id,
        plan_digest=session.plan_digest,
        code_digest=session.code_digest,
        blob_digests=blob_digests,
        # Only the *claiming* run publishes the lease token. A resume
        # run wouldn't add anything an agent doesn't already know,
        # but would re-expose the credential.
        lease_token=(session.lease_token if session.lease_just_claimed
                     else None),
        inert_reason=inert_reason,
    )
    manifest_text = json.dumps(manifest, indent=2) + "\n"

    with tarfile.open(fileobj=buf, mode="w") as tf:
        _add(tf, "manifest.json", manifest_text.encode())
        _add(tf, "timeline.log", render_timeline(session).encode())
        ops_body = "\n".join(json.dumps(r) for r in session.ops_log) + "\n"
        _add(tf, "ops.jsonl", ops_body.encode())
        # Echo the plan back so a client can diff against what it sent
        # and confirm the poller executed the same text it received.
        plan_text = getattr(session.plan, "text", "") or ""
        _add(tf, "plan.txt", plan_text.encode())
        if session.errors:
            _add(tf, "errors.log",
                 ("\n\n".join(session.errors) + "\n").encode())
        if session.bench_devices is not None:
            _add(tf, "bench.devices.json", (json.dumps(
                session.bench_devices, indent=2, sort_keys=True)
                + "\n").encode())
        if session.bench_ops is not None:
            _add(tf, "bench.ops.json", (json.dumps(
                session.bench_ops, indent=2, sort_keys=True)
                + "\n").encode())
        for name, stream in session.streams.items():
            _add(tf, f"streams/{_safe_stream_name(name)}.bin",
                 stream.snapshot_bytes())

    return buf.getvalue(), manifest_text


def _add(tf, name, data):
    ti = tarfile.TarInfo(name=name)
    ti.size = len(data)
    ti.mtime = 0
    tf.addfile(ti, io.BytesIO(data))
