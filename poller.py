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


class _Tee:
    def __init__(self, *streams):
        self._streams = streams
    def write(self, s):
        for st in self._streams:
            try:
                st.write(s)
                st.flush()
            except Exception:
                pass
        return len(s)
    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:
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


def _write_atomic(path, data):
    # Unique tempfile per call so concurrent writers (the periodic
    # _publish_status from the main loop and on-demand calls from
    # session worker threads) don't collide on a shared "<path>.tmp"
    # name -- the second one would FileNotFoundError when its tmp got
    # renamed away by the first.
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(
        dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # os.replace is atomic on POSIX and overwrites-if-exists on
        # Windows (unlike os.rename, which raises FileExistsError).
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def _publish_status(registry, plugins_by_name):
    os.makedirs(STATUS, mode=0o700, exist_ok=True)
    devices = json.dumps(registry.list_devices(), indent=2).encode()
    _write_atomic(os.path.join(STATUS, "devices.json"), devices)
    _push_status("devices.json", devices)

    leases = json.dumps(registry.lease_list(), indent=2).encode()
    _write_atomic(os.path.join(STATUS, "leases.json"), leases)
    _push_status("leases.json", leases)

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


def _drain_release_markers(registry):
    try:
        names = os.listdir(RELEASE)
    except FileNotFoundError:
        return
    for n in names:
        path = os.path.join(RELEASE, n)
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
        tar, txt = _failure_artefact(job_id, f"plan parse failed: {e}")
        _post_artefacts(job_id, tar, txt, upload_s)
        return

    needed = sorted(plan.required_devices(parsed))
    devs = ",".join(needed) if needed else "(none)"
    print(datetime.now(), tag,
          f"pickup {len(payload)} B  devices={devs}")

    try:
        _validate_against_plugins(parsed, plugins_by_name, registry)
    except Exception as e:
        tar, txt = _failure_artefact(job_id, f"validation: {e}")
        _post_artefacts(job_id, tar, txt, upload_s)
        return

    session = Session(registry, parsed, runtime_s=runtime_s)
    session.run_all(plugins_by_name)

    tar, manifest_text = pack_artefact(session)
    _post_artefacts(job_id, tar, manifest_text.encode(), upload_s)


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
    def walk(ops):
        for op in ops:
            if op.device is not None:
                if op.device not in plugins_by_name:
                    raise ValueError(f"line {op.lineno}: unknown device "
                                     f"{op.device!r}")
                pl = plugins_by_name[op.device]
                if op.verb in ("open", "close"):
                    pass
                elif op.verb not in pl.ops:
                    raise ValueError(f"line {op.lineno}: {op.device!r} has "
                                     f"no op {op.verb!r}")
            walk(op.body)
    walk(parsed.ops)


def _failure_artefact(job_id, message):
    import io
    import tarfile
    buf = io.BytesIO()
    manifest = {
        "status": "failed",
        "message": message,
        "job_id": job_id,
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
    return buf.getvalue(), manifest_bytes


def _post_artefacts(job_id, tar_bytes, sentinel_bytes,
                    timeout_s=DEFAULT_UPLOAD_S):
    base = f"http://localhost:{HTTP_PORT}"
    try:
        _post(f"{base}/{job_id}.tar", tar_bytes, timeout=timeout_s)
    except Exception:
        print(datetime.now(), f"POST .tar failed:\n{traceback.format_exc()}")
    try:
        _post(f"{base}/{job_id}.txt", sentinel_bytes, timeout=timeout_s)
    except Exception:
        print(datetime.now(), f"POST .txt failed:\n{traceback.format_exc()}")


def main():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    log_f = open(LOG, "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = _Tee(sys.__stdout__, log_f)
    sys.stderr = _Tee(sys.__stderr__, log_f)
    print(datetime.now(), f"logging to {LOG}")
    print(datetime.now(), "loading plugins...")
    plugins_by_name = plugins.load_all()
    print(datetime.now(), "plugins:", sorted(plugins_by_name.keys()))

    registry = DeviceRegistry(plugins_by_name)
    # Wire the on-demand publisher: session._run_inventory and any other
    # in-process caller can push fresh status to the server immediately
    # without waiting for the periodic refresh tick.
    registry.publish_status = lambda: _publish_status(
        registry, plugins_by_name)
    registry.refresh()
    print(datetime.now(), "startup verify sweep:")
    registry.verify_sweep()
    _print_device_table(registry.verify_results, registry)
    _publish_status(registry, plugins_by_name)

    def _sighup(signum, frame):
        print(datetime.now(), "SIGHUP: refresh plugins + devices")
        try:
            new_plugins = plugins.load_all()
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

    # Useful concurrency is bounded by the number of distinct devices
    # we have -- any two jobs competing for the same device serialize
    # on the registry's per-device lock anyway. Gate pick-up with a
    # semaphore sized to that ceiling so we never spawn threads that
    # can only sit on locks. Jobs beyond the cap stay queued on disk
    # in inputs/.
    max_active = max(1, len(plugins_by_name))
    worker_slot = threading.Semaphore(max_active)
    print(datetime.now(),
          f"dispatch: at most {max_active} job(s) active "
          f"(= number of plugins)")

    def _worker(body, headers):
        try:
            _dispatch(body, headers, registry, plugins_by_name)
        finally:
            worker_slot.release()

    try:
        while True:
            _drain_release_markers(registry)
            _drain_sweep_markers(registry, plugins_by_name)

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
