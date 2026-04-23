# SPDX-License-Identifier: MIT
# poller.py --- Bench-host loop: pick up .plan jobs, run sessions, post tars
# Copyright (c) 2026 Jakob Kastelic

import hashlib
import json
import os
import signal
import time
import traceback
import urllib.request
from datetime import datetime

import plan
import plugins
from plugin import Op
from registry import DeviceRegistry
from session import Session, pack_artefact


STATE_DIR = os.environ.get(
    "TEST_SERV_DIR",
    f"/tmp/test_serv-{os.getenv('USER', 'anon')}",
)
STATUS = os.path.join(STATE_DIR, "status")
RELEASE = os.path.join(STATE_DIR, "release")
SWEEP = os.path.join(STATE_DIR, "sweep")

HTTP_PORT = int(os.environ.get("TEST_SERV_PORT", "8080"))
POLL_INTERVAL_S = 2.5
DEVICE_REFRESH_S = 15.0


def _get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.read(), dict(r.headers)


def _post(url, data):
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req) as r:
        return r.status


def _write_atomic(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)


def _publish_status(registry, plugins_by_name):
    os.makedirs(STATUS, mode=0o700, exist_ok=True)
    devices = registry.list_devices()
    _write_atomic(os.path.join(STATUS, "devices.json"),
                  json.dumps(devices, indent=2).encode())
    ops_map = {}
    for name, pl in plugins_by_name.items():
        ops_map[name] = {
            "doc": pl.doc,
            "ops": {op_name: {"args": op.args, "doc": op.doc}
                    for op_name, op in pl.ops.items()},
        }
    _write_atomic(os.path.join(STATUS, "ops.json"),
                  json.dumps(ops_map, indent=2).encode())


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


def _print_device_table(verify_map, registry):
    """Pretty-print: plugin.id   status   latency   identity/error."""
    rows = registry.list_devices()
    if not rows:
        print("  (no devices present)")
        return
    w_id = max(len(r["id"]) for r in rows)
    for r in rows:
        v = r.get("verify") or {}
        ok = v.get("ok")
        mark = "OK   " if ok else ("FAIL " if ok is False else "?    ")
        lat = f"{v.get('latency_ms', 0):7.1f} ms" if v else "       --"
        tail = (v.get("err") if v and v.get("err")
                else (v.get("ok") and "identity verified")
                or "(not yet verified)")
        print(f"  [{mark}] {r['id']:<{w_id}}  {lat}  {tail}")


def _dispatch(payload, headers, registry, plugins_by_name):
    job_id = hashlib.sha256(payload).hexdigest()
    print(datetime.now(), "pickup", job_id[:12], f"{len(payload)} B")
    try:
        parsed = plan.load_tar(payload)
    except plan.PlanError as e:
        tar, txt = _failure_artefact(job_id, f"plan parse failed: {e}")
        _post_artefacts(job_id, tar, txt)
        return

    try:
        _validate_against_plugins(parsed, plugins_by_name, registry)
    except Exception as e:
        tar, txt = _failure_artefact(job_id, f"validation: {e}")
        _post_artefacts(job_id, tar, txt)
        return

    session = Session(registry, parsed)
    session.run_all(plugins_by_name)

    tar, manifest_text = pack_artefact(session)
    _post_artefacts(job_id, tar, manifest_text.encode())


def _validate_against_plugins(parsed, plugins_by_name, registry):
    """Reject jobs referencing unknown devices/ops before any hardware ran."""
    def walk(ops):
        for op in ops:
            if op.device is not None:
                if op.device not in plugins_by_name:
                    raise ValueError(f"line {op.lineno}: unknown device "
                                     f"{op.device!r}")
                pl = plugins_by_name[op.device]
                if op.verb in ("open", "close"):
                    pass    # synthesized; needs a probed instance
                elif op.verb not in pl.ops:
                    raise ValueError(f"line {op.lineno}: {op.device!r} has "
                                     f"no op {op.verb!r}")
                # Check that the device has a present instance at all.
                try:
                    registry.resolve(op.device)
                except LookupError as e:
                    raise ValueError(f"line {op.lineno}: {e}")
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


def _post_artefacts(job_id, tar_bytes, sentinel_bytes):
    base = f"http://localhost:{HTTP_PORT}"
    try:
        _post(f"{base}/{job_id}.tar", tar_bytes)
    except Exception:
        print(datetime.now(), f"POST .tar failed:\n{traceback.format_exc()}")
    try:
        _post(f"{base}/{job_id}.txt", sentinel_bytes)
    except Exception:
        print(datetime.now(), f"POST .txt failed:\n{traceback.format_exc()}")


def main():
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

    try:
        while True:
            _drain_release_markers(registry)
            _drain_sweep_markers(registry, plugins_by_name)

            if time.monotonic() - last_refresh > DEVICE_REFRESH_S:
                registry.refresh()
                _publish_status(registry, plugins_by_name)
                last_refresh = time.monotonic()

            try:
                status, body, headers = _get(f"{base}/plan")
            except Exception:
                print(datetime.now(), "GET /plan failed")
                time.sleep(POLL_INTERVAL_S)
                continue

            if status == 204 or not body:
                time.sleep(POLL_INTERVAL_S)
                continue

            _dispatch(body, headers, registry, plugins_by_name)
    except KeyboardInterrupt:
        pass
    finally:
        registry.stop()
        registry.close_all()


if __name__ == "__main__":
    main()
