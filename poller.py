# SPDX-License-Identifier: MIT
# poller.py --- Bench-host loop: pick up .plan jobs, run sessions, post tars
# Copyright (c) 2026 Jakob Kastelic

import hashlib
import http.client
import json
import os
import signal
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import paths
import plan
import plugins
from plugin import Op
from registry import DeviceRegistry
from session import Session, pack_artefact


STATE_DIR = paths.state_dir()
STATUS = os.path.join(STATE_DIR, "status")
RELEASE = os.path.join(STATE_DIR, "release")
SWEEP = os.path.join(STATE_DIR, "sweep")
LOG = os.path.join(STATE_DIR, "log.txt")
# Artefacts produced by run_all are spooled here before the POST to
# the server. If the POST fails (server restart, SSH-tunnel hiccup),
# the file stays on bench-side disk and the next main-loop tick
# retries -- so a transient outage at the end of a 20-minute flash
# test doesn't lose the artefact.
PENDING = os.path.join(STATE_DIR, "pending_uploads")

# digest -> Session for jobs the poller has picked up and finished
# constructing. _drain_cancels signals against this map; the server
# keeps cancel markers across drain ticks (and the artefact-upload
# path unlinks them), so a cancel that arrives mid-dispatch -- before
# the Session is registered here -- is found again on the next tick
# once the Session has landed in the map.
_active_sessions = {}
_active_lock = threading.Lock()


class _Tee:
    # Errors we must swallow so a closed-stream / pipe-closed during
    # process teardown doesn't blow up the worker thread. Anything else
    # (including OSError from a full disk) surfaces -- otherwise a
    # disk-full bench would silently stop logging.
    _SAFE_TEE_ERRORS = (BrokenPipeError, ValueError)

    def __init__(self, *streams):
        self._streams = streams
    def write(self, s):
        for st in self._streams:
            try:
                st.write(s)
                st.flush()
            except self._SAFE_TEE_ERRORS:
                pass
        return len(s)
    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except self._SAFE_TEE_ERRORS:
                pass


# log.txt rotation: if the file already has >LOG_ROTATE_BYTES at
# startup, rename it to log.txt.1 (replacing any prior .1) and start
# fresh. Bounds bench-host disk usage from a runaway plugin trace
# without forcing the operator to wire up logrotate.
LOG_ROTATE_BYTES = 64 * 1024 * 1024


def _rotate_log_if_large(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size < LOG_ROTATE_BYTES:
        return
    backup = path + ".1"
    try:
        if os.path.exists(backup):
            os.unlink(backup)
        os.rename(path, backup)
    except OSError:
        pass

HTTP_PORT = int(os.environ.get("TEST_SERV_PORT", "8080"))
POLL_INTERVAL_S = 2.5
DEVICE_REFRESH_S = 15.0
DEFAULT_UPLOAD_S = 600.0
MAX_UPLOAD_S = 3600.0
# Show a dotted progress line on stdout for transfers >= this size, with
# one dot per ``PROGRESS_BYTES_PER_DOT``. Tee'd into log.txt so even
# headless runs keep a record of how long the big SD-card-image moves
# actually took.
PROGRESS_THRESHOLD = 1 << 20         # 1 MiB
PROGRESS_BYTES_PER_DOT = 256 * 1024  # 256 KiB
_HTTP_CHUNK = 64 * 1024              # send/recv granularity


def _progress_dots(label, total, advance):
    """Render a progress line incrementally. Call with ``advance=0`` for
    the header, then with the byte count after each transfer chunk; call
    once more with ``advance < 0`` to terminate the line. Single-line,
    flushes after every dot so SSH-tunnelled tail -f stays live.
    """
    if advance == 0:
        sys.stdout.write(
            f"\n{datetime.now()} {label} {total >> 10} KiB ")
        sys.stdout.flush()
        return
    if advance < 0:
        sys.stdout.write(" done\n")
        sys.stdout.flush()
        return
    while advance >= PROGRESS_BYTES_PER_DOT:
        sys.stdout.write(".")
        sys.stdout.flush()
        advance -= PROGRESS_BYTES_PER_DOT


def _get(url, timeout=30.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        cl = r.getheader("Content-Length")
        try:
            cl_int = int(cl) if cl is not None else None
        except ValueError:
            cl_int = None
        if cl_int is not None and cl_int >= PROGRESS_THRESHOLD:
            buf = bytearray()
            tally = 0
            _progress_dots(f"GET {url}", cl_int, 0)
            while len(buf) < cl_int:
                chunk = r.read(min(_HTTP_CHUNK, cl_int - len(buf)))
                if not chunk:
                    break
                buf += chunk
                tally += len(chunk)
                if tally >= PROGRESS_BYTES_PER_DOT:
                    _progress_dots("", 0, tally)
                    tally %= PROGRESS_BYTES_PER_DOT
            _progress_dots("", 0, -1)
            body = bytes(buf)
        else:
            body = r.read()
        return r.status, body, dict(r.headers)


def _post(url, data, timeout=DEFAULT_UPLOAD_S):
    if len(data) >= PROGRESS_THRESHOLD:
        return _post_streamed(url, data, timeout)
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def _post_streamed(url, data, timeout):
    """POST using http.client so we can send the body in chunks and
    print a progress line. urllib's ``urlopen(data=bytes)`` writes the
    whole body in one syscall with no hook for progress.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http":
        # Fall back rather than reimplement TLS; we only ever talk to
        # localhost via the SSH tunnel today.
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    host = parsed.hostname or "localhost"
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.connect()
        conn.putrequest("POST", path)
        conn.putheader("Content-Length", str(len(data)))
        conn.putheader("Content-Type", "application/octet-stream")
        conn.endheaders()
        sent = 0
        tally = 0
        _progress_dots(f"POST {url}", len(data), 0)
        mv = memoryview(data)
        while sent < len(data):
            n = min(_HTTP_CHUNK, len(data) - sent)
            conn.send(mv[sent:sent + n])
            sent += n
            tally += n
            if tally >= PROGRESS_BYTES_PER_DOT:
                _progress_dots("", 0, tally)
                tally %= PROGRESS_BYTES_PER_DOT
        _progress_dots("", 0, -1)
        resp = conn.getresponse()
        # Drain so the connection can be reused / closed cleanly.
        resp.read()
        return resp.status
    finally:
        conn.close()


def _meta_float(headers, key, default, hard_max):
    """Pull X-Test-<Key> from response headers (case-insensitive),
    parse as a float, clamp to ``[1.0, hard_max]``. Falls back to
    ``default`` on missing/garbage values.
    """
    needle = f"x-test-{key.lower()}"
    val = None
    for k, v in headers.items():
        if k.lower() == needle:
            val = v
            break
    if val is None:
        return default
    try:
        n = float(val)
    except (TypeError, ValueError):
        return default
    return max(1.0, min(n, hard_max))


_write_atomic = paths.write_atomic


def _push_status(name, body):
    """Push a status snapshot to the server. Best-effort -- the bench
    keeps running if the server is unreachable; the local copy under
    STATE_DIR/status/ is still authoritative for tail/inspection.
    Short timeout: status pushes are tiny and a wedged tunnel
    shouldn't block the refresh loop.
    """
    base = f"http://localhost:{HTTP_PORT}"
    try:
        _post(f"{base}/status/{name}", body, timeout=10.0)
    except Exception:
        # Don't traceback-spam the log on every refresh tick if the
        # server is offline; one line is enough.
        print(datetime.now(),
              f"status/{name} push failed (server unreachable?)")


def _snapshot_inflight():
    """Snapshot the in-flight sessions for the dashboard's live tail.

    Bounded per session: last 32 events, last 2KiB per stream, max
    8 streams. Operator gets a "what's actually happening RIGHT NOW"
    view without the artefact-fetch cycle. Read-only over session
    state, no mutations.
    """
    INFLIGHT_EVENTS = 32
    INFLIGHT_STREAM_BYTES = 2048
    INFLIGHT_MAX_STREAMS = 8
    out = []
    with _active_lock:
        sessions = list(_active_sessions.items())
    for digest, sess in sessions:
        try:
            with sess.lock:
                events = list(sess.events[-INFLIGHT_EVENTS:])
                stream_names = list(sess.streams.keys())
            stream_tails = {}
            for name in stream_names[:INFLIGHT_MAX_STREAMS]:
                s = sess.streams.get(name)
                if s is None:
                    continue
                raw = s.snapshot_bytes()[-INFLIGHT_STREAM_BYTES:]
                stream_tails[name] = raw.decode(
                    "utf-8", errors="replace")
            out.append({
                "digest": digest,
                "elapsed_s": time.monotonic() - sess.t0,
                "n_ops": len(sess.ops_log),
                "n_errors": len(sess.errors),
                "events": [
                    {"t": e["t"], "kind": e["kind"],
                     "source": e["source"], "msg": e["msg"]}
                    for e in events],
                "stream_tails": stream_tails,
            })
        except Exception:
            # Don't let a single half-built Session break the whole
            # snapshot.
            traceback.print_exc()
    return out


def _publish_inflight():
    """Publish only the inflight snapshot. Cheap; called on every
    poll-loop tick (2.5s) so the dashboard's live tail tracks the
    bench in near-real-time. Skips entirely when no session is
    active so an idle bench doesn't churn the network.
    """
    with _active_lock:
        any_active = bool(_active_sessions)
    inflight = json.dumps(_snapshot_inflight()).encode()
    os.makedirs(STATUS, mode=0o700, exist_ok=True)
    _write_atomic(os.path.join(STATUS, "inflight.json"), inflight)
    if any_active:
        _push_status("inflight.json", inflight)


def _publish_status(registry, plugins_by_name):
    os.makedirs(STATUS, mode=0o700, exist_ok=True)
    devices = json.dumps(registry.list_devices(), indent=2).encode()
    _write_atomic(os.path.join(STATUS, "devices.json"), devices)
    _push_status("devices.json", devices)

    _publish_inflight()

    ops_map = {}
    for name, pl in plugins_by_name.items():
        ops_map[name] = {
            "doc": pl.doc,
            "ops": {op_name: {"args": op.args,
                              "optional_args": op.optional_args or {},
                              "doc": op.doc}
                    for op_name, op in pl.ops.items()},
        }
    ops = json.dumps(ops_map, indent=2).encode()
    _write_atomic(os.path.join(STATUS, "ops.json"), ops)
    _push_status("ops.json", ops)


_signaled_cancels = set()  # digests this poller has already signalled
_signaled_lock = threading.Lock()


def _drain_cancels():
    """Pull cancel markers and signal the matching active sessions.

    The server now keeps cancel markers across reads so a marker that
    arrives mid-dispatch is found again on the next tick once the
    Session is registered. To avoid re-signalling (and re-logging)
    the same cancel every 2.5s for the duration of pack+spool+upload,
    track which digests we've already signalled in this process.
    Entries are evicted when the session leaves _active_sessions.
    """
    base = f"http://localhost:{HTTP_PORT}"
    try:
        status, body, _ = _get(f"{base}/cancels", timeout=10.0)
    except Exception:
        return
    if status != 200 or not body:
        return
    try:
        digests = json.loads(body.decode())
    except Exception:
        return
    # Garbage-collect _signaled_cancels for jobs we no longer track.
    with _signaled_lock, _active_lock:
        _signaled_cancels.intersection_update(_active_sessions)
    for d in digests:
        with _signaled_lock:
            if d in _signaled_cancels:
                continue
        with _active_lock:
            sess = _active_sessions.get(d)
        if sess is None:
            # Job already completed (artefact upload unlinks the
            # marker server-side) or hasn't been picked up by this
            # poller yet. Either way, nothing to signal right now;
            # next tick will re-check.
            continue
        sess.signal_cancel()
        with _signaled_lock:
            _signaled_cancels.add(d)
        print(datetime.now(), f"[{d[:8]}] cancel signaled")


def _drain_release_markers(registry):
    try:
        names = os.listdir(RELEASE)
    except FileNotFoundError:
        return
    import re as _re
    safe = _re.compile(r"^[A-Za-z0-9._-]+$")
    for n in names:
        path = os.path.join(RELEASE, n)
        # Defence in depth: server-side validation already gates the
        # marker filename, but if RELEASE is ever populated by another
        # path (dropped file, future feature) we don't want to call
        # release_now("..").
        if not safe.match(n):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        try:
            ok = registry.release_now(n)
            os.remove(path)
            print(datetime.now(), "release",
                  n, "ok" if ok else "(was not cached / in use)")
        except Exception:
            traceback.print_exc()


def _drain_sweep_markers(registry, plugins_by_name):
    """Honour a REST-triggered re-sweep: re-probe + verify + republish."""
    try:
        names = os.listdir(SWEEP)
    except FileNotFoundError:
        return
    if not names:
        return
    print(datetime.now(), "sweep requested via REST")
    registry.refresh()
    _print_device_table(registry.verify_sweep(), registry)
    _publish_status(registry, plugins_by_name)
    for n in names:
        try:
            os.remove(os.path.join(SWEEP, n))
        except OSError:
            pass


_SPEC_LOCATOR_KEYS = (
    "serial_port", "resource", "ft4222_desc", "ft2232h_desc",
    "ip", "usb_serial",
)


def _describe_spec(spec):
    """Render the spec's most user-visible identifier (COM port, VISA
    resource, FTDI descriptor, IP, ...).  Empty string if none found.
    """
    for k in _SPEC_LOCATOR_KEYS:
        v = spec.get(k)
        if v:
            return str(v)
    return ""


def _print_device_table(verify_map, registry):
    """Pretty-print: plugin.id   location   latency   status/identity."""
    rows = registry.list_devices()
    if not rows:
        print("  (no devices present)")
        return
    w_id = max(len(r["id"]) for r in rows)
    locs = [_describe_spec(r["spec"]) for r in rows]
    w_loc = max([len(x) for x in locs] + [0])
    for r, loc in zip(rows, locs):
        v = r.get("verify") or {}
        ok = v.get("ok")
        mark = "OK   " if ok else ("FAIL " if ok is False else "?    ")
        lat = f"{v.get('latency_ms', 0):7.1f} ms" if v else "       --"
        if v and v.get("err"):
            tail = v["err"]
        elif v and v.get("ok"):
            tail = ("identity verified" if v.get("verified")
                    else "open ok (plugin has no identity handshake)")
        else:
            tail = "(not yet verified)"
        print(f"  [{mark}] {r['id']:<{w_id}}  {loc:<{w_loc}}  "
              f"{lat}  {tail}")


def _dispatch(payload, headers, registry, plugins_by_name):
    job_id = hashlib.sha256(payload).hexdigest()
    tag = f"[{job_id[:8]}]"
    from session import DEFAULT_SESSION_S, MAX_SESSION_S
    runtime_s = _meta_float(headers, "Runtime",
                            DEFAULT_SESSION_S, MAX_SESSION_S)
    upload_s = _meta_float(headers, "Upload-Timeout",
                           DEFAULT_UPLOAD_S, MAX_UPLOAD_S)
    try:
        parsed = plan.load_tar(payload)
    except plan.PlanError as e:
        print(datetime.now(), tag,
              f"pickup {len(payload)} B  devices=?  parse failed: {e}")
        tar = _failure_artefact(job_id, f"plan parse failed: {e}")
        _post_artefact(job_id, tar, upload_s)
        return

    needed = sorted(plan.required_devices(parsed))
    devs = ",".join(needed) if needed else "(none)"
    print(datetime.now(), tag,
          f"pickup {len(payload)} B  devices={devs}")

    try:
        _validate_against_plugins(parsed, plugins_by_name, registry)
    except Exception as e:
        tar = _failure_artefact(job_id, f"validation: {e}")
        _post_artefact(job_id, tar, upload_s)
        return

    session = Session(registry, parsed, runtime_s=runtime_s)
    with _active_lock:
        _active_sessions[job_id] = session
    try:
        session.run_all(plugins_by_name)
        tar, _manifest_text = pack_artefact(session)
        _post_artefact(job_id, tar, upload_s)
    finally:
        with _active_lock:
            _active_sessions.pop(job_id, None)


def _validate_against_plugins(parsed, plugins_by_name, registry):
    """Reject jobs with unknown plugins or op names before running.

    Device *instance* presence is NOT checked here: the registry's
    view of which devices are plugged in refreshes only every
    DEVICE_REFRESH_S seconds, so a plan whose earlier ops switch the
    board into a new mode (e.g. bench_mcu:send data="r" puts MP135 in
    DFU) would unfairly fail validation if instance presence were
    required up front. Missing instances surface as op-time errors
    via session._run_device_op, which does a targeted re-probe of the
    relevant plugin before giving up.
    """
    for op in parsed.ops:
        if op.device is None:
            continue
        plugin_name, _ = plan.split_device_ref(op.device)
        if plugin_name not in plugins_by_name:
            known = ", ".join(sorted(plugins_by_name)) or "(none)"
            raise ValueError(
                f"line {op.lineno}: unknown device {op.device!r}; "
                f"known plugins: {known}")
        pl = plugins_by_name[plugin_name]
        if op.verb in ("open", "close"):
            continue
        if op.verb not in pl.ops:
            ops_known = ", ".join(sorted(pl.ops)) or "(none)"
            raise ValueError(
                f"line {op.lineno}: {op.device!r} has no op "
                f"{op.verb!r}; known ops: {ops_known}")


def _failure_artefact(job_id, message):
    import io
    import tarfile
    buf = io.BytesIO()
    now_wall = time.time()
    iso = datetime.fromtimestamp(now_wall).isoformat(timespec="milliseconds")
    # Mirror the success-path manifest shape so a script reading
    # /outputs/<digest>.tar's manifest.json sees the same keys
    # whether the run succeeded, errored, or never started. status
    # is the discriminator.
    manifest = {
        "status": "failed",
        "message": message,
        "job_id": job_id,
        "t0_monotonic": time.monotonic(),
        "t0_wall_iso": iso,
        "t0_wall_unix": now_wall,
        "runtime_s": 0.0,
        "streams": [],
        "n_ops": 0,
        "n_errors": 1,
        "required_devices": [],
        "bench_id": os.environ.get("TEST_SERV_BENCH_ID"),
    }
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo("manifest.json")
        ti.size = len(manifest_bytes)
        tf.addfile(ti, io.BytesIO(manifest_bytes))
        err = message.encode() + b"\n"
        ti = tarfile.TarInfo("errors.log")
        ti.size = len(err)
        tf.addfile(ti, io.BytesIO(err))
    return buf.getvalue()


def _spool_artefact(job_id, tar_bytes):
    """Write the artefact to PENDING/<digest>.tar atomically.

    Returns the spool path. The next main-loop tick (or the immediate
    upload attempt) reads from this path; on success the file is
    unlinked. On failure the file stays so a later tick can retry.
    """
    os.makedirs(PENDING, mode=0o700, exist_ok=True)
    path = os.path.join(PENDING, f"{job_id}.tar")
    paths.write_atomic(path, tar_bytes)
    return path


def _post_spooled(spool_path, timeout_s=DEFAULT_UPLOAD_S):
    """Try POSTing one spooled artefact. On 2xx, unlink it. On 4xx
    (permanent refusal -- e.g. server returns 409 because the agent
    already cleaned up that digest) also unlink: a retry would just
    fail the same way. On 5xx / connection errors leave the file
    and log; the next main-loop tick will retry.
    """
    name = os.path.basename(spool_path)
    base = f"http://localhost:{HTTP_PORT}"
    try:
        with open(spool_path, "rb") as f:
            body = f.read()
    except FileNotFoundError:
        return False
    try:
        _post(f"{base}/{name}", body, timeout=timeout_s)
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            print(datetime.now(),
                  f"POST {name} refused with {e.code} (giving up): {e}")
            try:
                os.unlink(spool_path)
            except FileNotFoundError:
                pass
            return False
        print(datetime.now(),
              f"POST {name} failed with {e.code} (will retry): {e}")
        return False
    except Exception:
        print(datetime.now(),
              f"POST {name} failed (will retry):",
              traceback.format_exc().splitlines()[-1])
        return False
    try:
        os.unlink(spool_path)
    except FileNotFoundError:
        pass
    return True


def _drain_pending_uploads(timeout_s=DEFAULT_UPLOAD_S):
    """Try to POST every spooled artefact. Called on each main-loop
    tick so a transient server outage doesn't lose work.
    """
    try:
        names = sorted(os.listdir(PENDING))
    except FileNotFoundError:
        return
    for n in names:
        if not n.endswith(".tar"):
            continue
        _post_spooled(os.path.join(PENDING, n), timeout_s=timeout_s)


def _post_artefact(job_id, tar_bytes, timeout_s=DEFAULT_UPLOAD_S):
    """Spool the artefact to disk first, then try to upload. If the
    upload fails, the file stays in PENDING and the next main-loop
    tick's _drain_pending_uploads picks it up.
    """
    spool = _spool_artefact(job_id, tar_bytes)
    _post_spooled(spool, timeout_s=timeout_s)


def main():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    os.makedirs(PENDING, mode=0o700, exist_ok=True)
    _rotate_log_if_large(LOG)
    log_f = open(LOG, "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = _Tee(sys.__stdout__, log_f)
    sys.stderr = _Tee(sys.__stderr__, log_f)
    print(datetime.now(), f"state dir: {STATE_DIR}")
    print(datetime.now(), f"logging to {LOG}")
    print(datetime.now(), "loading plugins...")
    plugins_by_name = plugins.load_all()
    print(datetime.now(), "plugins:", sorted(plugins_by_name.keys()))

    registry = DeviceRegistry(plugins_by_name)
    registry.refresh()
    print(datetime.now(), "startup verify sweep:")
    registry.verify_sweep()
    _print_device_table(registry.verify_results, registry)
    _publish_status(registry, plugins_by_name)

    def _sighup(signum, frame):
        print(datetime.now(), "SIGHUP: refresh plugins + devices")
        # Refuse the reload while sessions are running -- live plugin
        # modules with cached USB handles would be swapped under
        # them. The operator's edits land on the next idle window.
        with _active_lock:
            if _active_sessions:
                print(datetime.now(),
                      f"SIGHUP: deferring -- {len(_active_sessions)} "
                      f"session(s) still running")
                return
        try:
            new_plugins = plugins.load_all(reload=True)
            plugins_by_name.clear()
            plugins_by_name.update(new_plugins)
            registry.plugins = plugins_by_name
            registry.refresh()
            _publish_status(registry, plugins_by_name)
        except Exception:
            traceback.print_exc()

    try:
        signal.signal(signal.SIGHUP, _sighup)
    except (AttributeError, ValueError):
        # windows / non-main thread: skip
        pass

    last_refresh = time.monotonic()
    base = f"http://localhost:{HTTP_PORT}"

    # Useful concurrency is bounded by the number of distinct device
    # instances -- any two jobs competing for the same device serialize
    # on the registry's per-device lock anyway. Two jobs touching
    # different *instances* of the same plugin (mp135.evb vs
    # mp135.custom, dfu.evb vs dfu.custom, fpga.hx1k vs fpga.hx8k) can
    # run in parallel; the cap is over the spec count, not the plugin
    # count. Floor of 2 so a sparsely-populated bench still serves
    # back-to-back submissions. Jobs beyond the cap stay queued on
    # disk in inputs/.
    max_active = max(2, len(registry.specs))
    worker_slot = threading.Semaphore(max_active)
    print(datetime.now(),
          f"dispatch: at most {max_active} job(s) active "
          f"(= number of device instances)")

    def _worker(body, headers):
        try:
            _dispatch(body, headers, registry, plugins_by_name)
        finally:
            worker_slot.release()

    try:
        while True:
            _drain_release_markers(registry)
            _drain_sweep_markers(registry, plugins_by_name)
            _drain_cancels()
            _drain_pending_uploads()
            # Refresh the live-tail snapshot every poll tick so the
            # dashboard sees in-flight events + stream tails with
            # ~2.5s latency rather than waiting for the artefact.
            _publish_inflight()

            if time.monotonic() - last_refresh > DEVICE_REFRESH_S:
                registry.refresh()
                _publish_status(registry, plugins_by_name)
                last_refresh = time.monotonic()

            # Block until a worker slot opens so we don't hold the job
            # payload in memory unread, and so disk-queued jobs don't
            # accumulate live Python threads all blocked on locks.
            worker_slot.acquire()
            try:
                status, body, headers = _get(f"{base}/plan")
            except Exception:
                print(datetime.now(), "GET /plan failed")
                worker_slot.release()
                time.sleep(POLL_INTERVAL_S)
                continue

            if status == 204 or not body:
                worker_slot.release()
                time.sleep(POLL_INTERVAL_S)
                continue

            t = threading.Thread(target=_worker, args=(body, headers),
                                 daemon=True)
            t.start()
    except KeyboardInterrupt:
        pass
    finally:
        registry.stop()
        registry.close_all()


if __name__ == "__main__":
    main()
