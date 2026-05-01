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
        # Set by the poller in its main(); session._run_inventory and
        # any other on-demand caller can fire it to push the current
        # devices/ops/leases JSONs to the server right now instead of
        # waiting for the next 15s refresh tick.
        self.publish_status = None
        # Lease registry: token -> {"devices": set, "expires_at": monotonic}.
        # Persists across sessions so an agent can hold devices for an
        # extended debug window. Reaper expires entries; lease_blocks_us()
        # gates other agents at session start.
        self.leases = {}
        self._lease_cv = threading.Condition()
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

    def refresh_plugin(self, name):
        """Targeted re-probe of a single plugin.

        Cheaper than a full refresh and safe to call mid-session.
        Lets rapidly-changing presence (e.g. MP135 flipping into DFU
        mode after a bench_mcu:reset_dut) become visible to later ops
        without waiting for the background 15 s refresh tick.
        """
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
            # Skip devices currently held by a running session; the
            # sweep's open+close would either block for the duration of
            # that session (stalling the sweep) or race with its
            # in-flight ops. Report the in-use status verbatim.
            with self.lock:
                entry = self.cache.get(key)
                in_use = entry is not None and entry[3] > 0
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
            self._evict_expired_leases()

    # --- leases ---

    def _evict_expired_leases(self):
        """Drop expired lease entries and wake anyone waiting on a release."""
        with self._lease_cv:
            now = time.monotonic()
            dead = [t for t, l in self.leases.items() if l["expires_at"] < now]
            for t in dead:
                del self.leases[t]
            if dead:
                self._lease_cv.notify_all()

    def lease_blocks_us(self, key, my_token):
        """Return the lease token currently holding ``key`` if it would
        block a session whose ``my_token`` is ``my_token`` (or None for
        non-lease sessions). Returns None if free or owned by us. Eagerly
        evicts expired leases as a side-effect.
        """
        with self._lease_cv:
            now = time.monotonic()
            for t, l in list(self.leases.items()):
                if l["expires_at"] < now:
                    del self.leases[t]
                    continue
                if key in l["devices"]:
                    return None if t == my_token else t
            return None

    def lease_add(self, token, key, duration_s):
        """Add ``key`` to the lease identified by ``token`` (creating the
        lease if absent). Caller must currently hold the per_dev_lock for
        ``key`` -- enforced indirectly by the fact that ``lease:claim``
        runs as a session op, and the session is already holding it.
        ``duration_s`` is clamped to ``[1, MAX_LEASE_S]``.
        """
        from session import MAX_SESSION_S
        duration_s = max(1.0, min(float(duration_s), MAX_SESSION_S))
        with self._lease_cv:
            now = time.monotonic()
            for t, l in list(self.leases.items()):
                if key in l["devices"] and l["expires_at"] > now and t != token:
                    raise RuntimeError(
                        f"{key!r} is already leased to {t!r}")
            l = self.leases.setdefault(
                token, {"devices": set(), "expires_at": 0.0})
            l["devices"].add(key)
            new_exp = now + duration_s
            if new_exp > l["expires_at"]:
                l["expires_at"] = new_exp
            return l["expires_at"]

    def lease_drop(self, token):
        """Drop every entry held by ``token``. Returns the device set
        that was held (empty set if the token was unknown/expired)."""
        with self._lease_cv:
            l = self.leases.pop(token, None)
            self._lease_cv.notify_all()
            return set(l["devices"]) if l else set()

    def lease_resume(self, token):
        """Validate that ``token`` has an active lease. Returns the
        device-key set on success, or ``None`` if the token is unknown
        or already expired.
        """
        with self._lease_cv:
            l = self.leases.get(token)
            now = time.monotonic()
            if l is None or l["expires_at"] < now:
                return None
            return set(l["devices"])

    def lease_list(self):
        """Snapshot of active leases, suitable for inclusion in artefacts.

        Includes ``expires_at_walltime`` (seconds since unix epoch) on
        top of ``expires_in_s`` so a browser/UI can render a smooth
        countdown without round-tripping the bench's monotonic clock.
        """
        with self._lease_cv:
            now_mono = time.monotonic()
            now_wall = time.time()
            out = []
            for t, l in self.leases.items():
                if l["expires_at"] <= now_mono:
                    continue
                rem = l["expires_at"] - now_mono
                out.append({
                    "token": t,
                    "devices": sorted(l["devices"]),
                    "expires_in_s": rem,
                    "expires_at_walltime": now_wall + rem,
                })
            return out


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
