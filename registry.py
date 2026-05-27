# SPDX-License-Identifier: MIT
# registry.py --- Device-handle cache with explicit release
# Copyright (c) 2026 Jakob Kastelic

import collections
import threading
import time
import traceback

from plugin import BusyError


# Per-device open() wall-clock cap during verify_sweep. A device that
# enumerates but hangs on first byte-write would otherwise stall
# bench startup forever. 30s is long enough for slow USB enumeration
# + a heavy plugin's identity handshake; short enough that the
# operator doesn't think the bench is wedged.
VERIFY_OPEN_TIMEOUT_S = 30.0

# Per-plugin probe() wall-clock cap during refresh / refresh_plugin.
# A plugin's probe call into a third-party C extension (ftd2xx,
# pyusb, pyft4222) can hang in a kernel USB syscall if the device
# disappears mid-enumeration -- and `refresh()` runs on the main
# loop thread, so a stuck probe wedges the entire poller (no
# pickups, no cancels, no inflight publishes, just a process that
# looks alive but logs nothing). Wrap each probe in a thread with
# this cap so a hung driver eats one tick instead of the bench.
PROBE_TIMEOUT_S = 15.0

# Per-device pl.open() wall-clock cap during _Acquire (every session
# op that resolves a key calls pl.open via _Acquire). Same hazard
# as PROBE_TIMEOUT_S but on the session worker thread: a hung open
# (serial.Serial constructor on a flaky tty, ftd2xx.openByDescription
# on a wedged FTDI chip, pyvisa.open_resource on a scope mid-reboot)
# would otherwise hold dev_lock + a worker_slot indefinitely, with
# no way for the runtime_s deadline to fire because the session
# never reaches its next op boundary. 60s is more generous than
# PROBE_TIMEOUT_S since open includes identity handshakes (e.g. a
# real *IDN? round-trip on the scope).
OPEN_TIMEOUT_S = 60.0

_KEEP_PREVIOUS_SPECS = object()


def _run_with_wallclock_timeout(label, fn, timeout_s):
    """Run ``fn()`` in a daemon thread with a wall-clock cap.

    Returns ``(result, error_text, timed_out)``. On timeout the
    thread is leaked -- C-level blocking syscalls cannot be
    interrupted from Python -- but the caller gets control back so
    the rest of the bench keeps running. Use this for any third-
    party-C call that cannot be trusted to return promptly: probe,
    pl.open, verify_sweep's identity handshake.
    """
    box = [None, None]   # [result, exception_text]

    def _do():
        try:
            box[0] = fn()
        except Exception:
            box[1] = traceback.format_exc()

    t = threading.Thread(
        target=_do, daemon=True, name=f"timeout-{label}")
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        return None, None, True
    return box[0], box[1], False


class DeviceRegistry:
    """Tracks device specs and lends handles on demand.

    ``probe()`` of each plugin is called on refresh to populate ``specs``.
    Handles are opened lazily the first time ``acquire()`` is called and
    stay cached so back-to-back ops within the same session don't pay
    the open cost every time. Sessions force-close all touched devices
    in their finally block (``release_now``), so handles don't linger
    across job boundaries.
    """

    def __init__(self, plugins):
        # plugins: {name: DevicePlugin}
        self.plugins = plugins
        self.lock = threading.Lock()
        self.specs = {}        # "dsp.A" -> (plugin_name, spec dict)
        # cache entry shape: [handle, refs]. No TTL; lifetime is bounded
        # by session.run_all's release_now sweep at session end.
        self.cache = {}
        self.per_dev_lock = {} # "dsp.A" -> threading.RLock
        self.verify_results = {}  # "dsp.A" -> {t, ok, err, latency_ms}
        # Counter of keys currently claimed by active sessions for
        # the whole session lifetime. Refresh checks counter[key] > 0
        # in addition to cache refs before dropping a vanished spec,
        # so a USB blip mid-session can't yank a device out from
        # under the session while ops are still scheduled to run on
        # it. Counter (not set) so two concurrent sessions on the
        # same key don't share one membership entry that the first
        # one to finish can revoke for the still-running peer.
        self.pinned_specs = collections.Counter()
        # Keys whose plugin.open() hung in verify_sweep. The sweep
        # thread is still inside _Acquire holding dev_lock and there's
        # no way to interrupt a C-level USB syscall from Python -- so
        # subsequent acquires on this key would block forever on the
        # held RLock. _Acquire short-circuits with BusyError instead.
        # Cleared only by a poller restart; the operator gets a clear
        # "replug + restart" signal in /devices verify column.
        self.quarantined = set()

    def _probe_with_timeout(self, pname, pl):
        """Call ``pl.probe()`` with a wall-clock cap so a hung
        third-party driver (ftd2xx, pyusb, etc) can't wedge the
        main loop. Returns the spec list on success, or
        _KEEP_PREVIOUS_SPECS on exception/timeout. A successful []
        means "devices are absent"; a failed probe means "unknown,
        keep the last good view".

        Guards against firing a second probe of the same plugin
        while the prior probe's leaked thread is still inside the
        C library. Two concurrent libusb listdev calls on a flapping
        bus is exactly the input that drives the underlying drivers
        into the corrupt state we're trying to avoid.
        """
        if not hasattr(self, "_probe_inflight"):
            self._probe_inflight = set()
            self._probe_inflight_lock = threading.Lock()
        with self._probe_inflight_lock:
            if pname in self._probe_inflight:
                # Prior leaked probe is still running; don't pile on.
                return _KEEP_PREVIOUS_SPECS
            self._probe_inflight.add(pname)

        def _do():
            try:
                return pl.probe() or []
            finally:
                with self._probe_inflight_lock:
                    self._probe_inflight.discard(pname)

        result, err, timed_out = _run_with_wallclock_timeout(
            f"probe-{pname}", _do, PROBE_TIMEOUT_S)
        if timed_out:
            print(f"refresh: {pname}.probe() timed out after "
                  f"{PROBE_TIMEOUT_S:.0f}s "
                  f"(thread leaked, will skip this plugin until it "
                  f"returns)")
            return _KEEP_PREVIOUS_SPECS
        if err:
            print(f"refresh: {pname}.probe() raised:\n{err}")
            return _KEEP_PREVIOUS_SPECS
        return result or []

    def _keep_existing_plugin_specs_locked(self, found, pname):
        for key, val in self.specs.items():
            if val[0] == pname:
                found[key] = val

    def refresh(self):
        """Rescan every plugin's probe() and update specs."""
        # Drop any cached config snapshot so all plugins probe()d in
        # this single tick see one coherent view of config.json. A
        # mid-tick non-atomic editor save would otherwise let plugin
        # A see the old config and plugin B the new (or briefly the
        # empty fallback while the file's being rewritten).
        try:
            import config as _config
            _config.load_invalidate()
        except Exception:
            pass
        found = {}
        for pname, pl in self.plugins.items():
            specs = self._probe_with_timeout(pname, pl)
            if specs is _KEEP_PREVIOUS_SPECS:
                with self.lock:
                    self._keep_existing_plugin_specs_locked(found, pname)
                continue
            for spec in specs:
                did = spec.get("id")
                if did is None:
                    continue
                key = f"{pname}.{did}"
                found[key] = (pname, spec)
        with self.lock:
            # drop specs for devices that have vanished *and* are not in use
            for key in list(self.specs):
                if (key not in found
                        and self.pinned_specs.get(key, 0) <= 0):
                    entry = self.cache.get(key)
                    if entry is None or entry[1] == 0:
                        self.specs.pop(key, None)
                        self._close_if_cached_locked(key)
            for key, val in found.items():
                self.specs[key] = val
                self.per_dev_lock.setdefault(key, threading.RLock())

    def refresh_plugin(self, name):
        """Targeted re-probe of a single plugin.

        Cheaper than a full refresh and safe to call mid-session.
        Lets rapidly-changing presence (e.g. MP135 flipping into DFU
        mode after a bench_mcu:reset_dut) become visible to later ops
        without waiting for the background 15 s refresh tick.
        """
        try:
            import config as _config
            _config.load_invalidate()
        except Exception:
            pass
        pl = self.plugins.get(name)
        if pl is None:
            return
        specs = self._probe_with_timeout(name, pl)
        if specs is _KEEP_PREVIOUS_SPECS:
            return
        found = {}
        for spec in specs:
            did = spec.get("id")
            if did is None:
                continue
            key = f"{name}.{did}"
            found[key] = (name, spec)
        with self.lock:
            # Drop vanished instances for this plugin only, and only
            # when nobody is holding them.
            for key in list(self.specs):
                plugin_name, _ = self.specs[key]
                if plugin_name != name:
                    continue
                if (key not in found
                        and self.pinned_specs.get(key, 0) <= 0):
                    entry = self.cache.get(key)
                    if entry is None or entry[1] == 0:
                        self.specs.pop(key, None)
                        self._close_if_cached_locked(key)
            for key, val in found.items():
                self.specs[key] = val
                self.per_dev_lock.setdefault(key, threading.RLock())

    def resolve(self, plugin_name, spec_id=None):
        """Return the full device key ``"plugin.id"`` for a job reference.

        With ``spec_id=None``, requires a unique instance in the plugin.
        """
        with self.lock:
            candidates = [k for k, (p, _) in self.specs.items()
                          if p == plugin_name
                          and (spec_id is None or k.endswith(f".{spec_id}"))]
        if not candidates:
            raise LookupError(
                f"no device matches {plugin_name}"
                + (f".{spec_id}" if spec_id else ""))
        if len(candidates) > 1 and spec_id is None:
            real_candidates = [
                k for k in candidates
                if k.rsplit(".", 1)[-1] != "any"]
            if len(real_candidates) == 1:
                return real_candidates[0]
            if real_candidates:
                candidates = real_candidates
        if len(candidates) > 1 and spec_id is None:
            ids = ", ".join(sorted(c.split(".", 1)[1] for c in candidates))
            raise LookupError(
                f"ambiguous: {plugin_name} has {len(candidates)} "
                f"instances ({ids}); use `{plugin_name}.<id>:op` "
                f"to disambiguate")
        return candidates[0]

    def list_devices(self):
        with self.lock:
            out = []
            for key, (pname, spec) in sorted(self.specs.items()):
                entry = self.cache.get(key)
                status = "open" if (entry and entry[1] > 0) else (
                    "cached" if entry else "closed")
                verify = self.verify_results.get(key)
                out.append({
                    "id": key,
                    "plugin": pname,
                    "spec": spec,
                    "status": status,
                    "verify": verify,
                })
        return out

    def verify_sweep(self):
        """Open + immediately close every probed device once, recording
        whether its plugin's identity handshake (if any) succeeded.

        Runs serially -- some plugins share the same physical USB bus, so
        parallel opens risk driver-level contention.  Cost: O(N_devices)
        handshakes; each is ~tens of ms for the VCP devices and one SCPI
        round-trip for the scope.  Safe to run at startup *and* on demand.
        """
        with self.lock:
            keys = sorted(self.specs.keys())
        for key in keys:
            # Skip devices currently held by a running session; the
            # sweep's open+close would either block for the duration of
            # that session (stalling the sweep) or race with its
            # in-flight ops. Report the in-use status verbatim.
            with self.lock:
                entry = self.cache.get(key)
                in_use = entry is not None and entry[1] > 0
            if in_use:
                with self.lock:
                    self.verify_results[key] = {
                        "t": time.time(), "ok": None,
                        "verified": False, "err": "(in use by running job)",
                        "latency_ms": 0.0}
                continue
            t0 = time.monotonic()
            entry = {"t": time.time(), "ok": False, "verified": False,
                     "err": None, "latency_ms": 0.0}
            # Run each open() with a wall-clock timeout so a hung USB
            # device (rare but does happen on bad cables / power-
            # glitched chips) doesn't wedge poller startup forever.
            # On timeout we mark the key quarantined; subsequent
            # acquires fail fast with BusyError instead of hanging on
            # the dev_lock the leaked thread is still holding.
            def _do():
                with self.acquire(key) as handle:
                    return bool(
                        getattr(handle, "_identity_verified", False))

            verified, err_text, timed_out = _run_with_wallclock_timeout(
                f"verify-{key}", _do, VERIFY_OPEN_TIMEOUT_S)
            if timed_out:
                with self.lock:
                    self.quarantined.add(key)
                entry["err"] = (f"open timed out after "
                                f"{VERIFY_OPEN_TIMEOUT_S:.0f}s "
                                f"(device hung; key quarantined -- "
                                f"clear via POST /devices/{key}/release "
                                f"after replug, or restart the poller)")
            elif err_text is not None:
                # Render the last line of the traceback for the
                # operator (the full traceback is in the timeout
                # helper but verify_results is meant to be a
                # one-glance summary).
                last = err_text.strip().splitlines()[-1] if err_text else ""
                entry["err"] = last
            else:
                entry["verified"] = bool(verified)
                entry["ok"] = True
                # The sweep's whole point is "open it once and put it
                # back as if untouched"; close before we move on so a
                # multi-device sweep doesn't end with every device
                # cached open.
                self.release_now(key)
            entry["latency_ms"] = (time.monotonic() - t0) * 1e3
            with self.lock:
                self.verify_results[key] = entry
        return {k: self.verify_results.get(k) for k in keys}

    def acquire(self, key):
        """Context manager: returns the open handle.

        Callers must wrap usage in ``with registry.acquire(key) as h:``.
        """
        return _Acquire(self, key)

    def release_now(self, key):
        """Force-close a cached handle. In-use handles are left alone."""
        with self.lock:
            entry = self.cache.get(key)
            if entry is None:
                return False
            if entry[1] > 0:
                return False
            self._close_if_cached_locked(key)
            return True

    def close_all(self):
        with self.lock:
            for key in list(self.cache):
                self._close_if_cached_locked(key)

    # --- internals ---

    def _close_if_cached_locked(self, key):
        entry = self.cache.pop(key, None)
        if entry is None:
            return
        handle, refs = entry
        if refs > 0:
            # should not happen with ref=0 gate, but be safe
            self.cache[key] = entry
            return
        pname, _spec = self.specs.get(key, (None, None))
        pl = self.plugins.get(pname) if pname else None
        if pl is not None:
            try:
                pl.close(handle)
            except Exception:
                traceback.print_exc()


class _Acquire:
    def __init__(self, registry, key):
        self.registry = registry
        self.key = key
        self.handle = None
        self._lock = None

    def __enter__(self):
        reg = self.registry
        key = self.key
        with reg.lock:
            if key not in reg.specs:
                raise LookupError(f"device {key!r} not present")
            if key in reg.quarantined:
                raise BusyError(
                    f"device {key!r} quarantined: open hung in a "
                    f"prior verify sweep, the dev_lock is leaked. "
                    f"Replug the device and restart the poller.")
            dev_lock = reg.per_dev_lock.setdefault(key, threading.RLock())
        # Take per-device lock *outside* the registry lock so different
        # devices can be acquired concurrently.
        dev_lock.acquire()
        try:
            # Read the spec / plugin under reg.lock and check the cache,
            # but release reg.lock BEFORE calling pl.open(). A hung
            # USB open syscall would otherwise hold reg.lock for the
            # whole kernel-timeout window, blocking every other reg
            # consumer (list_devices, refresh, status publisher, every
            # other session's acquire) -- the bench would freeze even
            # though only one device is sick. The per-device dev_lock
            # is held throughout, so two threads can't race to open
            # the same key.
            with reg.lock:
                # Re-check quarantine: a thread that was queued on
                # dev_lock.acquire() while a concurrent _Acquire's
                # pl.open() was hanging would otherwise pass the
                # outer quarantine check, then proceed to also call
                # pl.open() against the still-wedged driver. Without
                # this re-check, N concurrent waiters each pay one
                # OPEN_TIMEOUT_S before recovering. With it, the
                # first timeout's quarantine flag fast-fails the
                # rest immediately.
                if key in reg.quarantined:
                    raise BusyError(
                        f"device {key!r} quarantined while waiting "
                        f"on dev_lock; a prior open timed out")
                entry = reg.cache.get(key)
                if entry is not None:
                    handle = entry[0]
                    entry[1] += 1
                    self._lock = dev_lock
                    self.handle = handle
                    return handle
                pname, spec = reg.specs[key]
                pl = reg.plugins[pname]
            # Plugin call OUTSIDE reg.lock, with a wall-clock cap.
            # A hung pl.open in C (serial.Serial constructor on a
            # flaky tty, ftd2xx.openByDescription on a wedged FTDI,
            # pyvisa.open_resource on a scope mid-reboot) would
            # otherwise hold dev_lock + a worker_slot indefinitely
            # with no way for the session's runtime_s deadline to
            # fire (deadline is op-boundary; we never reach the
            # next op). Quarantine the key so subsequent acquires
            # fast-fail rather than queue up behind the leaked
            # thread.
            handle, err_text, timed_out = _run_with_wallclock_timeout(
                f"open-{key}", lambda: pl.open(spec), OPEN_TIMEOUT_S)
            if timed_out:
                # Mark the key quarantined BEFORE releasing dev_lock:
                # the next acquire that beats us to the lock would
                # otherwise queue up against the still-wedged driver
                # too. The quarantine check at the top of __enter__
                # fast-fails before dev_lock.acquire, so subsequent
                # callers BusyError immediately.
                # The leaked thread is running pl.open(), not holding
                # dev_lock (only this thread did, at line ~507) -- so
                # the BusyError handler below correctly releases it.
                with reg.lock:
                    reg.quarantined.add(key)
                    # Mirror the quarantine into verify_results so
                    # the dashboard's verify column shows the device
                    # as not-OK. Without this, list_devices() carries
                    # whatever the last verify_sweep wrote (often a
                    # stale "ok"), and the operator looking at a
                    # green device row has zero clue why their plans
                    # keep failing with "quarantined".
                    reg.verify_results[key] = {
                        "t": time.time(),
                        "ok": False,
                        "verified": False,
                        "err": (f"quarantined: pl.open() timed out "
                                f"after {OPEN_TIMEOUT_S:.0f}s mid-"
                                f"session; clear via POST /devices/"
                                f"{key}/release after replug"),
                        "latency_ms": OPEN_TIMEOUT_S * 1e3,
                    }
                raise BusyError(
                    f"{key} pl.open() timed out after "
                    f"{OPEN_TIMEOUT_S:.0f}s; quarantined")
            if err_text is not None:
                last = err_text.strip().splitlines()[-1] if err_text else ""
                raise RuntimeError(f"{key} pl.open() failed: {last}")
            with reg.lock:
                # Cache may have been populated concurrently by
                # another acquirer, but we held dev_lock the whole
                # time so that's impossible; install fresh.
                reg.cache[key] = [handle, 1]
        except BaseException:
            dev_lock.release()
            raise
        self._lock = dev_lock
        self.handle = handle
        return handle

    def __exit__(self, *exc):
        reg = self.registry
        try:
            with reg.lock:
                entry = reg.cache.get(self.key)
                if entry is not None:
                    entry[1] -= 1
        finally:
            if self._lock is not None:
                self._lock.release()
