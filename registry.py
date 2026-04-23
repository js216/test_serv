# SPDX-License-Identifier: MIT
# registry.py --- Lazy device-handle cache with TTL and explicit release
# Copyright (c) 2026 Jakob Kastelic

import threading
import time
import traceback

from plugin import BusyError


class DeviceRegistry:
    """Tracks device specs and lends handles on demand.

    ``probe()`` of each plugin is called on refresh to populate ``specs``.
    Handles are opened lazily the first time ``acquire()`` is called and
    kept in ``cache`` for ``ttl`` seconds after the last release so
    back-to-back ops don't pay the open cost every time.

    A background reaper closes expired handles when no session holds
    them. ``release_now()`` drops a specific handle immediately (the
    endpoint the bench tech hits before opening PuTTY on the DSP UART).
    """

    def __init__(self, plugins, ttl_s=5.0):
        # plugins: {name: DevicePlugin}
        self.plugins = plugins
        self.ttl_s = ttl_s
        self.lock = threading.Lock()
        self.specs = {}        # "dsp.A" -> (plugin_name, spec dict)
        self.cache = {}        # "dsp.A" -> (handle, opened_at, last_used, refs)
        self.per_dev_lock = {} # "dsp.A" -> threading.RLock
        self.verify_results = {}  # "dsp.A" -> {t, ok, err, latency_ms}
        self._stop = threading.Event()
        self._reaper = threading.Thread(target=self._reap, daemon=True)
        self._reaper.start()

    def stop(self):
        self._stop.set()

    def refresh(self):
        """Rescan every plugin's probe() and update specs."""
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
                if key not in found:
                    entry = self.cache.get(key)
                    if entry is None or entry[3] == 0:
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
            raise LookupError(
                f"ambiguous: {plugin_name} has {len(candidates)} instances; "
                f"specify id"
            )
        return candidates[0]

    def list_devices(self):
        with self.lock:
            out = []
            for key, (pname, spec) in sorted(self.specs.items()):
                entry = self.cache.get(key)
                status = "open" if (entry and entry[3] > 0) else (
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
            t0 = time.monotonic()
            entry = {"t": time.time(), "ok": False, "verified": False,
                     "err": None, "latency_ms": 0.0}
            try:
                with self.acquire(key) as handle:
                    entry["verified"] = bool(
                        getattr(handle, "_identity_verified", False))
                entry["ok"] = True
            except Exception as e:
                entry["err"] = f"{type(e).__name__}: {e}"
            entry["latency_ms"] = (time.monotonic() - t0) * 1e3
            with self.lock:
                self.verify_results[key] = entry
        return {k: self.verify_results.get(k) for k in keys}

    def acquire(self, key):
        """Context manager: returns (handle, per_device_lock_held).

        Callers must wrap usage in ``with registry.acquire(key) as h:``.
        """
        return _Acquire(self, key)

    def release_now(self, key):
        """Force-close a cached handle. In-use handles are left alone."""
        with self.lock:
            entry = self.cache.get(key)
            if entry is None:
                return False
            if entry[3] > 0:
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
        now = time.monotonic()
        self.cache[key] = [handle, now, now, 1]
        return handle

    def _close_if_cached_locked(self, key):
        entry = self.cache.pop(key, None)
        if entry is None:
            return
        handle, _, _, refs = entry
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

    def _reap(self):
        while not self._stop.wait(0.5):
            now = time.monotonic()
            with self.lock:
                for key in list(self.cache):
                    handle, opened_at, last_used, refs = self.cache[key]
                    if refs == 0 and (now - last_used) > self.ttl_s:
                        self._close_if_cached_locked(key)


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
                    entry[3] += 1
                    entry[2] = time.monotonic()
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
                    entry[3] -= 1
                    entry[2] = time.monotonic()
        finally:
            if self._lock is not None:
                self._lock.release()
