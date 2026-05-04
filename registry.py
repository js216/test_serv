# SPDX-License-Identifier: MIT
# registry.py --- Device-handle cache with explicit release
# Copyright (c) 2026 Jakob Kastelic

import collections
import threading
import time
import traceback

from plugin import BusyError


# Hard ceiling on the live lease count. Far above any legitimate
# bench load (one operator, a handful of held devices), well below
# what would noticeably slow lease_blocks_acquire's linear scan.
MAX_LEASES = 256

# Per-device open() wall-clock cap during verify_sweep. A device that
# enumerates but hangs on first byte-write would otherwise stall
# bench startup forever. 30s is long enough for slow USB enumeration
# + a heavy plugin's identity handshake; short enough that the
# operator doesn't think the bench is wedged.
VERIFY_OPEN_TIMEOUT_S = 30.0


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
        # Cross-session leases. token -> {"devices": set(keys),
        #   "expires_at": monotonic deadline}.
        # A lease promises that the listed devices won't be acquired
        # by another agent until expires_at -- so the holder can
        # close one plan and submit a follow-up "resume" plan
        # without losing the device to a competing claimant. State
        # is in-memory; a poller restart loses all leases (the
        # operator can re-claim).
        self.leases = {}
        self._leases_lock = threading.Lock()
        # Keys whose plugin.open() hung in verify_sweep. The sweep
        # thread is still inside _Acquire holding dev_lock and there's
        # no way to interrupt a C-level USB syscall from Python -- so
        # subsequent acquires on this key would block forever on the
        # held RLock. _Acquire short-circuits with BusyError instead.
        # Cleared only by a poller restart; the operator gets a clear
        # "replug + restart" signal in /devices verify column.
        self.quarantined = set()

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
            # Run each open() in a thread with a wall-clock timeout so
            # a hung USB device (rare but does happen on bad cables /
            # power-glitched chips) doesn't wedge poller startup
            # forever. The thread is left running on timeout -- C-side
            # blocking syscalls can't be interrupted from Python -- so
            # we mark the key quarantined; subsequent acquires fail
            # fast with BusyError instead of hanging on the dev_lock
            # the leaked thread is still holding.
            verified_box = [False]
            err_box = [None]

            def _do():
                try:
                    with self.acquire(key) as handle:
                        verified_box[0] = bool(
                            getattr(handle, "_identity_verified", False))
                except Exception as e:
                    err_box[0] = e

            t = threading.Thread(target=_do, daemon=True)
            t.start()
            t.join(timeout=VERIFY_OPEN_TIMEOUT_S)
            if t.is_alive():
                with self.lock:
                    self.quarantined.add(key)
                entry["err"] = (f"open timed out after "
                                f"{VERIFY_OPEN_TIMEOUT_S:.0f}s "
                                f"(device hung; key quarantined -- "
                                f"replug + restart poller to clear)")
            elif err_box[0] is not None:
                e = err_box[0]
                entry["err"] = f"{type(e).__name__}: {e}"
            else:
                entry["verified"] = verified_box[0]
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

    # --- leases ---

    def lease_claim(self, devices, duration_s):
        """Reserve ``devices`` for the next ``duration_s`` seconds.
        Returns the token. Caller (session) is expected to acquire
        the per-device locks for the rest of THIS session as usual;
        the lease just gates *other* agents at lock-acquire time.
        """
        import uuid
        # Reject lease-plugin internals: the lease plugin probes a
        # `lease._default` pseudo-device so the registry can route
        # `lease:claim` ops; an agent claiming `lease._default`
        # itself would lock every other session out of the lease
        # subsystem (their lease:claim/release/list would all try
        # to acquire lease._default and fast-fail on the lease
        # check). Block it explicitly.
        for k in devices:
            if k.startswith("lease."):
                raise BusyError(
                    f"lease:claim cannot target the lease plugin's "
                    f"own pseudo-device ({k!r})")
        # Validate device names against the live spec set so an agent
        # can't claim an arbitrary string ("dsp.A,fakeval,etc"). This
        # also caps the per-claim device list: if the spec set is
        # bounded (it is -- one row per probed instance), so is the
        # claim.
        with self.lock:
            valid = set(self.specs)
        unknown = [k for k in devices if k not in valid]
        if unknown:
            raise BusyError(
                f"lease:claim references unknown device(s): "
                f"{sorted(unknown)}; known: {sorted(valid)}")
        token = uuid.uuid4().hex[:16]
        now = time.monotonic()
        with self._leases_lock:
            self._leases_evict_expired_locked(now)
            # Cap the live lease count so a malicious or buggy agent
            # can't fill memory + slow lease_blocks_acquire (which is
            # O(N_leases) per device key).
            if len(self.leases) >= MAX_LEASES:
                raise BusyError(
                    f"lease table full ({len(self.leases)} >= "
                    f"{MAX_LEASES}); wait for some to expire")
            for k in devices:
                holder = self._lease_holder_locked(k, now)
                if holder is not None and holder != token:
                    raise BusyError(
                        f"{k} is leased to another token "
                        f"(expires in "
                        f"{self.leases[holder]['expires_at']-now:.0f}s)")
            self.leases[token] = {
                "devices": set(devices),
                "expires_at": now + max(1.0, float(duration_s)),
            }
        return token

    def lease_resume(self, token):
        """Validate ``token`` against the live lease table. Returns
        the held device set, or raises ``BusyError`` if the lease
        is unknown or expired.
        """
        now = time.monotonic()
        with self._leases_lock:
            self._leases_evict_expired_locked(now)
            entry = self.leases.get(token)
            if entry is None:
                raise BusyError(f"lease {token!r} unknown or expired")
            return set(entry["devices"])

    def lease_release(self, token):
        """Drop a lease early. Returns the device set that was held
        (empty if the token wasn't live).
        """
        with self._leases_lock:
            entry = self.leases.pop(token, None)
            return set(entry["devices"]) if entry else set()

    def lease_blocks_acquire(self, key, my_token):
        """Return the holding-token if some *other* agent has a
        live lease on ``key``, else ``None``. Called from
        session.run_all's eager-acquire path so a competing plan
        fast-fails instead of blocking on the per-device lock.
        """
        now = time.monotonic()
        with self._leases_lock:
            self._leases_evict_expired_locked(now)
            holder = self._lease_holder_locked(key, now)
            return None if (holder is None or holder == my_token) else holder

    def lease_list(self):
        """Snapshot of live leases for inspection. Returns a list of
        ``{token, devices, expires_in_s}`` dicts.
        """
        now = time.monotonic()
        with self._leases_lock:
            self._leases_evict_expired_locked(now)
            return [
                {
                    "token": t,
                    "devices": sorted(e["devices"]),
                    "expires_in_s": e["expires_at"] - now,
                }
                for t, e in self.leases.items()
            ]

    def _leases_evict_expired_locked(self, now):
        dead = [t for t, e in self.leases.items()
                if e["expires_at"] < now]
        for t in dead:
            self.leases.pop(t, None)

    def _lease_holder_locked(self, key, now):
        for t, e in self.leases.items():
            if e["expires_at"] >= now and key in e["devices"]:
                return t
        return None

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
                entry = reg.cache.get(key)
                if entry is not None:
                    handle = entry[0]
                    entry[1] += 1
                    self._lock = dev_lock
                    self.handle = handle
                    return handle
                pname, spec = reg.specs[key]
                pl = reg.plugins[pname]
            # Plugin call OUTSIDE reg.lock.
            handle = pl.open(spec)
            with reg.lock:
                # Cache may have been populated concurrently by
                # another acquirer, but we held dev_lock the whole
                # time so that's impossible; install fresh.
                reg.cache[key] = [handle, 1]
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
