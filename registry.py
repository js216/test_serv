# SPDX-License-Identifier: MIT
# registry.py --- Device-handle cache with explicit release
# Copyright (c) 2026 Jakob Kastelic

import collections
import threading
import time
import traceback

from plugin import BusyError


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

    def stop(self):
        # Kept for API compatibility with the older TTL-reaper version;
        # there's no background thread to stop now.
        pass

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
            try:
                specs = pl.probe() or []
            except Exception:
                traceback.print_exc()
                specs = []
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
        try:
            specs = pl.probe() or []
        except Exception:
            traceback.print_exc()
            specs = []
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
            try:
                with self.acquire(key) as handle:
                    entry["verified"] = bool(
                        getattr(handle, "_identity_verified", False))
                entry["ok"] = True
                # The sweep's whole point is "open it once and put it
                # back as if untouched"; close before we move on so a
                # multi-device sweep doesn't end with every device
                # cached open.
                self.release_now(key)
            except Exception as e:
                entry["err"] = f"{type(e).__name__}: {e}"
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

    def _open_locked(self, key):
        pname, spec = self.specs[key]
        pl = self.plugins[pname]
        handle = pl.open(spec)
        self.cache[key] = [handle, 1]
        return handle

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
            dev_lock = reg.per_dev_lock.setdefault(key, threading.RLock())
        # Take per-device lock *outside* the registry lock so different
        # devices can be acquired concurrently.
        dev_lock.acquire()
        try:
            with reg.lock:
                entry = reg.cache.get(key)
                if entry is None:
                    handle = reg._open_locked(key)
                else:
                    handle = entry[0]
                    entry[1] += 1
        except BusyError:
            dev_lock.release()
            raise
        except Exception:
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
