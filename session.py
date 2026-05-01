# SPDX-License-Identifier: MIT
# session.py --- Execute a parsed plan: ops, streams, artefact tar
# Copyright (c) 2026 Jakob Kastelic

import io
import json
import tarfile
import threading
import time
import traceback
import uuid
from datetime import datetime

from plan import PlanError
from plugin import Op as OpSchema
from plugin import BusyError, decode_args, DeviceLostError


DEFAULT_SESSION_S = 600.0
MAX_SESSION_S = 3600.0


class Stream:
    """A time-stamped byte stream.

    Background-filler threads append ``(t_ns, bytes)`` records via
    ``append()``; ``snapshot()`` copies out the current contents
    without stopping collection. ``close()`` seals further appends.
    """
    def __init__(self, name, t0):
        self.name = name
        self.t0 = t0
        self.records = []
        self.lock = threading.Lock()
        self.closed = False

    def append(self, data):
        if not data:
            return
        t = time.monotonic() - self.t0
        with self.lock:
            if not self.closed:
                self.records.append((t, bytes(data)))

    def close(self):
        with self.lock:
            self.closed = True

    def snapshot_bytes(self):
        with self.lock:
            return b"".join(d for _, d in self.records)

    def snapshot_timestamped(self):
        with self.lock:
            return list(self.records)


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
        self.streams = {}
        self.events = []       # list of dicts for timeline.log
        self.ops_log = []      # list of dicts for ops.jsonl
        self.pinned = {}       # device_key -> context manager (for open/close)
        self.touched_keys = set()
        self.errors = []
        self.lock = threading.Lock()
        self.early_done = False
        self.session_id = f"sess-{uuid.uuid4().hex[:12]}"
        # Set by a leading lease:resume op or minted by the first lease:claim;
        # gates session lock acquisition against the registry's lease registry
        # so other agents can't acquire devices we're holding across sessions.
        self.lease_token = None

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

    def signal_early_done(self, reason=""):
        self.early_done = True
        self.log_event("CTRL", "session", f"early_done: {reason}")

    def _prescan_lease_resume(self):
        """If the plan starts with ``lease:resume token=...``, take over
        that token's lease before any locks are acquired so the lease
        gate treats us as the owner.  Anything past op 0 is ignored to
        keep the contract simple: resume must be the first op.
        """
        if not self.plan.ops:
            return
        op = self.plan.ops[0]
        if op.device != "lease" or op.verb != "resume":
            return
        tok = op.args.get("token")
        if tok is None:
            raise PlanError("lease:resume requires token=...")
        token = tok.raw if hasattr(tok, "raw") else str(tok)
        held = self.registry.lease_resume(token)
        if held is None:
            raise BusyError(
                f"lease {token!r} is unknown or expired; cannot resume")
        self.lease_token = token
        self.log_event(
            "LEASE", "session",
            f"resume token={token!r} devices={sorted(held)}")

    # --- execution ---

    def run_all(self, plugins):
        # Agent picks down via X-Test-Runtime, bench enforces up via
        # MAX_SESSION_S so a rogue agent can't camp on a device.
        budget_s = min(self.runtime_s or DEFAULT_SESSION_S, MAX_SESSION_S)
        deadline = self.t0 + budget_s
        # Pre-scan for `lease:resume token=...` so the lease gate below
        # treats this session as the lease's owner. Validation (token
        # exists / not expired) runs again at op time as a safety net.
        self._prescan_lease_resume()
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
        self._deferred_locks = []     # filled by _run_device_op

        self.log_event(
            "LOCK", "session",
            f"acquire now={eager_keys}  "
            f"deferred={sorted(self._deferred_names)}"
            if eager_keys or self._deferred_names else "(no devices)")
        # Pre-flight lease check: refuse fast if any required device is
        # leased to a different agent. Cheap; the post-acquire re-check
        # below closes the race window where someone leases between
        # this peek and our lock grab.
        eager_acquired = []
        try:
            for k in eager_keys:
                blocker = self.registry.lease_blocks_us(k, self.lease_token)
                if blocker is not None:
                    raise BusyError(
                        f"{k} is leased to {blocker!r}; resume that lease "
                        f"or wait for it to expire")
            for lk, k in zip(eager_locks, eager_keys):
                lk.acquire()
                eager_acquired.append(lk)
                blocker = self.registry.lease_blocks_us(k, self.lease_token)
                if blocker is not None:
                    raise BusyError(
                        f"{k} was leased to {blocker!r} between pre-flight "
                        f"and lock acquire")
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
        self.log_event("LOCK", "session", "acquired; running ops")
        try:
            self._run_block(self.plan.ops, plugins, deadline)
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
            for key in sorted(self.touched_keys):
                try:
                    if self.registry.release_now(key):
                        self.log_event("CLOSE", "session", key)
                except Exception:
                    traceback.print_exc()
            for s in self.streams.values():
                s.close()
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
            if self.early_done:
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

    def _resolve_device(self, plugin_name):
        """Resolve with one targeted re-probe on miss.

        Lets plans whose earlier ops caused a device to appear (DUT
        reset into DFU, board coming online, ...) find it at op time
        without waiting for the background refresh tick.  If the
        device is in the session's deferred set, this also promotes
        it to 'locked for the remainder of the session' so subsequent
        ops on the same device keep the job-atomic guarantee.
        """
        try:
            key = self.registry.resolve(plugin_name)
        except LookupError:
            self.registry.refresh_plugin(plugin_name)
            key = self.registry.resolve(plugin_name)

        if plugin_name in getattr(self, "_deferred_names", set()):
            blocker = self.registry.lease_blocks_us(key, self.lease_token)
            if blocker is not None:
                raise BusyError(
                    f"{key} is leased to {blocker!r}; resume that lease "
                    f"or wait for it to expire")
            with self.registry.lock:
                lk = self.registry.per_dev_lock.setdefault(
                    key, threading.RLock())
            lk.acquire()
            blocker = self.registry.lease_blocks_us(key, self.lease_token)
            if blocker is not None:
                lk.release()
                raise BusyError(
                    f"{key} was leased to {blocker!r} between pre-flight "
                    f"and lock acquire")
            self._deferred_locks.append(lk)
            self._deferred_names.discard(plugin_name)
            self.log_event("LOCK", "session",
                           f"deferred acquire {key}")
        self.touched_keys.add(key)
        return key

    def _run_device_op(self, op, plugins):
        if op.device not in plugins:
            raise PlanError(f"unknown device {op.device!r}")
        plugin = plugins[op.device]

        if op.verb in ("open", "close"):
            key = self._resolve_device(op.device)
            if op.verb == "open":
                if key in self.pinned:
                    return
                cm = self.registry.acquire(key)
                cm.__enter__()
                self.pinned[key] = cm
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
        if v == "barrier":
            tag = op.args.get("tag")
            self.log_event("BARRIER", "ctrl",
                           tag.raw if tag is not None else "")
        elif v == "mark":
            tag = op.args.get("tag")
            self.log_event("MARK", "ctrl",
                           tag.raw if tag is not None else "")
        elif v == "delay":
            ms = op.args.get("ms")
            if ms is None:
                raise PlanError("delay: missing ms=")
            time.sleep(max(0.0, ms.as_int() / 1000.0))
        elif v == "wall_time":
            self._run_wall_time()
        elif v == "inventory":
            self._run_inventory(op, plugins)
        elif v == "fork":
            self._run_fork(op, plugins, deadline)
        elif v == "join":
            # join is a no-op: fork block already joins at its end.
            pass
        else:
            raise PlanError(f"unknown control verb {v!r}")

    def _run_wall_time(self):
        now = datetime.now().astimezone()
        rec = {
            "iso": now.isoformat(timespec="microseconds"),
            "unix_s": time.time(),
            "tz": now.tzname(),
        }
        self.stream("bench.time.json").append(
            (json.dumps(rec, indent=2, sort_keys=True) + "\n").encode())
        self.log_event("TIME", "ctrl", rec["iso"])

    def _run_inventory(self, op, plugins):
        verify = op.args.get("verify")
        refresh = op.args.get("refresh")
        if refresh is None or refresh.as_bool():
            self.registry.refresh()
        if verify is not None and verify.as_bool():
            self.registry.verify_sweep()
        # Also push the freshly probed state to the server right away so
        # an `inventory` op from the web UI updates the dashboard
        # without waiting for the next 15 s tick.
        publish = getattr(self.registry, "publish_status", None)
        if callable(publish):
            try:
                publish()
            except Exception:
                traceback.print_exc()

        devices = self.registry.list_devices()
        ops_map = {
            "_control": {
                "doc": "Plan control verbs handled by the session runner.",
                "ops": {
                    "barrier": {
                        "args": {},
                        "optional_args": {"tag": "ident"},
                        "doc": "Add a barrier checkpoint to timeline.log.",
                    },
                    "delay": {
                        "args": {"ms": "int"},
                        "optional_args": {},
                        "doc": "Sleep for ms milliseconds.",
                    },
                    "fork": {
                        "args": {"name": "ident"},
                        "optional_args": {},
                        "doc": "Start a fork block; terminated by end.",
                    },
                    "inventory": {
                        "args": {},
                        "optional_args": {
                            "refresh": "bool",
                            "verify": "bool",
                        },
                        "doc": (
                            "Return bench.devices.json and bench.ops.json "
                            "streams. Defaults: refresh=true verify=false."
                        ),
                    },
                    "join": {
                        "args": {},
                        "optional_args": {},
                        "doc": "No-op; fork blocks join at end.",
                    },
                    "mark": {
                        "args": {},
                        "optional_args": {"tag": "ident"},
                        "doc": "Add a named checkpoint to timeline.log.",
                    },
                    "wall_time": {
                        "args": {},
                        "optional_args": {},
                        "doc": "Return bench.time.json with poller wall time.",
                    },
                },
            },
        }
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

        self.stream("bench.devices.json").append(
            (json.dumps(devices, indent=2, sort_keys=True) + "\n").encode())
        self.stream("bench.ops.json").append(
            (json.dumps(ops_map, indent=2, sort_keys=True) + "\n").encode())
        self.log_event(
            "INVENTORY", "ctrl",
            f"devices={len(devices)} plugins={len(ops_map)}")

    def _run_fork(self, fork_op, plugins, deadline):
        """Run each child of the fork body in its own thread; join at end."""
        children = fork_op.body
        # static check: no two concurrent ops on the same device.
        devices_per_branch = [
            {o.device for o in [child] if o.device is not None}
            for child in children
        ]
        # (children are top-level ops, no nested fork here per MAX_DEPTH=2)
        seen = set()
        for ds in devices_per_branch:
            clash = ds & seen
            if clash:
                raise PlanError(
                    f"fork line {fork_op.lineno}: device {clash!r} "
                    f"used in two concurrent branches"
                )
            seen |= ds

        errs = []
        threads = []
        for child in children:
            t = threading.Thread(
                target=self._fork_child_runner,
                args=(child, plugins, deadline, errs),
                daemon=True,
            )
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=MAX_SESSION_S)
        for e in errs:
            self.errors.append(e)

    def _fork_child_runner(self, op, plugins, deadline, errs):
        try:
            self._run_one(op, plugins, deadline)
        except Exception:
            errs.append(traceback.format_exc())


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
            printable = chunk.decode("utf-8", errors="replace").replace(
                "\n", "\\n").replace("\r", "\\r")
            all_rows.append((t, f"STREAM   {name:<20} {printable!r}"))

    all_rows.sort(key=lambda r: r[0])
    lines = [f"{t:8.3f}  {body}" for t, body in all_rows]
    return "\n".join(lines) + "\n"


def pack_artefact(session):
    """Build the .tar artefact. Returns (tar_bytes, manifest_text)."""
    buf = io.BytesIO()
    from plan import required_devices as _req_devs
    manifest = {
        "t0_monotonic": session.t0,
        "runtime_s": time.monotonic() - session.t0,
        "streams": sorted(session.streams.keys()),
        "n_ops": len(session.ops_log),
        "n_errors": len(session.errors),
        "early_done": session.early_done,
        "required_devices": sorted(_req_devs(session.plan)),
    }
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
        for name, stream in session.streams.items():
            safe = name.replace("/", "_")
            _add(tf, f"streams/{safe}.bin", stream.snapshot_bytes())
            # Binary index: repeated (f8 t_s, u4 len) then payloads follow.
            idx = io.BytesIO()
            import struct
            for t, data in stream.snapshot_timestamped():
                idx.write(struct.pack("<dI", t, len(data)))
            _add(tf, f"streams/{safe}.ts", idx.getvalue())

    return buf.getvalue(), manifest_text


def _add(tf, name, data):
    ti = tarfile.TarInfo(name=name)
    ti.size = len(data)
    ti.mtime = 0
    tf.addfile(ti, io.BytesIO(data))
