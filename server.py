# SPDX-License-Identifier: MIT
# server.py --- Job broker + introspection endpoints
# Copyright (c) 2026 Jakob Kastelic

import argparse
import hashlib
import sys
import io
import json
import os
import random
import re
import tarfile
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler


import paths
import plan

STATE_DIR = paths.state_dir()
INPUTS = os.path.join(STATE_DIR, "inputs")
OUTPUTS = os.path.join(STATE_DIR, "outputs")
DONE = os.path.join(STATE_DIR, "done")
STATUS = os.path.join(STATE_DIR, "status")
RELEASE = os.path.join(STATE_DIR, "release")
SWEEP = os.path.join(STATE_DIR, "sweep")
# Cancel markers. DELETE /jobs/<digest> drops a file here for the
# poller to pick up via GET /cancels. Cross-host friendly: the file
# system isn't shared between server and poller, so we exchange the
# state through HTTP instead.
CANCEL = os.path.join(STATE_DIR, "cancel")

# Examples dir is relative to this file -- bench-operator-owned starter plans.
EXAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples")
# Static UI lives next to server.py. Serving the dashboard from the
# same process as the broker means the agent's SSH tunnel reaches
# both endpoints with one port forward; no extra static-server to run.
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg":  "image/svg+xml",
    ".ico":  "image/x-icon",
    ".png":  "image/png",
}
# Names the poller is allowed to push into the STATUS dir. Anything
# else is rejected so a foreign POST can't drop arbitrary files there.
ALLOWED_STATUS_FILES = (
    "devices.json", "ops.json", "inflight.json", "bench.json",
    "leases.json")


SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_EXT_RE = re.compile(r"^[A-Za-z0-9]+$")
SAFE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

# Artefact uploads beyond this size are rejected. 256 MiB comfortably
# covers a full scope capture set + chunked logs; if we ever need more,
# the cap is the right knob to bump rather than letting the poller
# OOM the server with a runaway body.
MAX_ARTEFACT_BYTES = 256 * 1024 * 1024
# Cap on /submit body size (the whole packed tar -- plan.txt plus
# every blob it ships). 64 MiB comfortably covers FPGA bitstream +
# DSP loader + STM32 firmware + sdcard image families; anything
# larger should chunk via dfu:flash_layout pulling separate blobs.
MAX_PLAN_BYTES = 64 * 1024 * 1024
# Status snapshots are small JSON.
MAX_STATUS_BYTES = 4 * 1024 * 1024

# Hard ceiling on the queue depth so a runaway agent loop can't fill
# the disk before the poller picks up. Returns 429 when reached;
# legitimate workloads stay well below.
MAX_QUEUED_PLANS = 256
# Refuse new submissions / artefacts when the state-dir filesystem
# has less than this much free space. 1 GiB is comfortably above any
# single reasonable artefact and gives the operator headroom to clean
# up before the disk actually fills.
MIN_FREE_DISK_BYTES = 1 * 1024 * 1024 * 1024
# OUTPUTS GC: artefacts older than this auto-delete. 7 days is long
# enough that an agent catching up after a weekend still sees its
# results; short enough that a forgetful agent doesn't pile up
# tarballs forever.
OUTPUT_MAX_AGE_S = 7 * 24 * 3600
GC_INTERVAL_S = 3600.0  # background sweep cadence
# Allowed extension for artefact uploads. Used to also accept ".txt"
# for a manifest sentinel; that's gone -- agents poll completion via
# HEAD /outputs/<digest>.tar and read the manifest out of the tar.
ARTEFACT_EXTS = ("tar",)

# Serialize queue_job's check-and-write so two concurrent /submit calls
# for the same digest can't both pass the duplicate check and race on
# meta/body files. queue_job is fast (small disk writes), so coarse
# locking is fine.
_submit_lock = threading.Lock()


def _read_meta(path):
    meta = {}
    with open(path) as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k:
                meta[k] = v
    return meta


def _read_file(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _request_path(raw_path):
    try:
        return urllib.parse.urlsplit(raw_path).path.lstrip("/")
    except ValueError:
        return None


_write_atomic = paths.write_atomic


def _disk_full():
    """Return True if STATE_DIR's filesystem has less than
    MIN_FREE_DISK_BYTES free. Used to gate /submit and artefact
    uploads with a clean 507 instead of letting _write_atomic
    fail mid-write with ENOSPC.
    """
    import shutil
    try:
        return shutil.disk_usage(STATE_DIR).free < MIN_FREE_DISK_BYTES
    except OSError:
        return False


def _queued_plan_count():
    try:
        return sum(1 for n in os.listdir(INPUTS) if n.endswith(".plan"))
    except FileNotFoundError:
        return 0


PRUNE_MIN_AGE_S = 30.0


def prune_stale_jobs():
    """Remove DONE/<digest>.plan files whose digests have no OUTPUTS
    and aren't currently in flight on the poller. These are the
    "running forever" entries -- the poller picked the job up at some
    point but no artefact is on the server (e.g. the poller crashed /
    restarted before posting, or the agent already DELETE'd it after
    fetching).

    Two protection layers:
    1) digest-in-inflight (poller's published JSON): covers any
       long-running session that's been live for >= one inflight
       publish tick (2.5s).
    2) DONE/.plan mtime younger than PRUNE_MIN_AGE_S: covers the
       gap before the first inflight push reaches the server. A
       lease op or quick failure can finish in ~100ms -- long
       before STATUS/inflight.json catches up -- and was
       previously vulnerable to "operator clicked clear-stale
       just after pickup", which deleted DONE/.plan, made the
       upload 409, and parked the artefact under refused/.

    Returns the count of plan records actually removed. Module-level
    so tests can exercise the protection logic without HTTP plumbing.
    """
    have_output = set()
    try:
        for n in os.listdir(OUTPUTS):
            d, _ = os.path.splitext(n)
            if SAFE_DIGEST_RE.match(d):
                have_output.add(d)
    except FileNotFoundError:
        pass
    protect = _inflight_digests()
    cutoff = time.time() - PRUNE_MIN_AGE_S
    removed = 0
    try:
        for n in list(os.listdir(DONE)):
            if not n.endswith(".plan"):
                continue
            digest = n[:-5]
            if not SAFE_DIGEST_RE.match(digest):
                continue
            if digest in have_output:
                continue
            if digest in protect:
                continue
            path = os.path.join(DONE, n)
            try:
                if os.stat(path).st_mtime > cutoff:
                    continue  # too fresh; presumed in-flight
            except FileNotFoundError:
                continue
            try:
                os.unlink(path)
                removed += 1
            except FileNotFoundError:
                pass
            try:
                os.unlink(os.path.join(DONE, f"{digest}.plan.meta"))
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass
    return removed


def _inflight_digests():
    """Read STATUS/inflight.json and return the set of digests the
    poller currently considers live. Used to protect mid-flight jobs
    from prune/idempotency-gate races: a long session has DONE/.plan
    written + OUTPUTS/.tar not yet posted, so it would otherwise look
    indistinguishable from a stale leftover.
    """
    raw = _read_file(os.path.join(STATUS, "inflight.json"))
    if not raw:
        return set()
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return set()
    if not isinstance(data, list):
        return set()
    out = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        d = entry.get("digest")
        if isinstance(d, str) and SAFE_DIGEST_RE.match(d):
            out.add(d)
    return out


def _gc_outputs(now=None):
    """Delete artefacts and DONE/<digest>.plan records whose mtime
    is older than OUTPUT_MAX_AGE_S. Best-effort; missing files are
    treated as already gone.
    """
    cutoff = (now or time.time()) - OUTPUT_MAX_AGE_S
    removed = 0
    for d in (OUTPUTS, DONE):
        try:
            names = list(os.listdir(d))
        except FileNotFoundError:
            continue
        for n in names:
            p = os.path.join(d, n)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.unlink(p)
                    removed += 1
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return removed


def _gc_loop():
    """Background sweep -- runs once per GC_INTERVAL_S, daemon thread.
    Single-tenant server, light enough to inline.
    """
    while True:
        time.sleep(GC_INTERVAL_S)
        try:
            n = _gc_outputs()
            if n:
                print(f"output GC: removed {n} stale file(s)")
        except Exception:
            import traceback as _tb
            _tb.print_exc()


# Two named per-submission knobs honoured by the runner. Anything
# else an agent might want to convey about a job goes in the in-plan
# `description "..."` line. Generic X-Test-* passthrough used to
# exist; nothing read it, so it's gone.
_META_HEADERS = ("Runtime", "Upload-Timeout")


def _meta_from_headers(headers):
    meta = {}
    for name in _META_HEADERS:
        v = headers.get(f"X-Test-{name}")
        if v is not None and v != "":
            meta[name.lower()] = v
    return meta


def queue_job(body, meta=None):
    digest = hashlib.sha256(body).hexdigest()
    dst = os.path.join(INPUTS, f"{digest}.plan")
    meta_path = f"{dst}.meta"
    # Whole check-and-write is under a single lock so two concurrent
    # /submit calls for the same digest can't both clear the meta file
    # and race on rewrites. The dedupe check (os.path.exists(dst))
    # would otherwise be a TOCTOU racing against another writer.
    with _submit_lock:
        if _queued_plan_count() >= MAX_QUEUED_PLANS:
            return digest, "queue_full"
        stale = [
            n for n in os.listdir(OUTPUTS)
            if n.startswith(f"{digest}.")
        ]
        if stale:
            return digest, "stale_outputs"
        if os.path.exists(dst):
            return digest, "duplicate"
        # Also reject if the same digest is already mid-flight: the
        # .plan is in DONE (poller picked it up), or the poller's
        # inflight feed still lists it. Without this, two identical
        # bodies submitted seconds apart would both dispatch, and
        # _active_sessions[digest] gets clobbered -- two real runs
        # collapse into one observable artefact in OUTPUTS.
        if os.path.exists(os.path.join(DONE, f"{digest}.plan")):
            return digest, "duplicate"
        if digest in _inflight_digests():
            return digest, "duplicate"
        # Write meta first so a poller racing to pick up the .plan
        # never sees an empty/old meta file -- pickup keys off .plan
        # presence.
        if meta:
            body_meta = "".join(
                f"{k}={v}\n" for k, v in meta.items()).encode()
            _write_atomic(meta_path, body_meta)
        else:
            try:
                os.remove(meta_path)
            except FileNotFoundError:
                pass
        _write_atomic(dst, body)
        # If a stale CANCEL/<digest> marker survives from a prior
        # poller crash (the artefact-upload path that normally
        # unlinks it never ran), unlink it now. Otherwise the next
        # poller pickup of this freshly-queued plan would have
        # _drain_cancels find the old marker and signal_cancel the
        # newborn session 2.5s in -- a phantom cancel with no
        # DELETE issued.
        try:
            os.unlink(os.path.join(CANCEL, digest))
        except FileNotFoundError:
            pass
    return digest, "queued"


_manifest_status_cache = {}  # tar_path -> (mtime, status_str)


def _peek_manifest_status(tar_path):
    """Pull manifest.json's status field out of an artefact tar
    cheaply, with a tiny mtime-keyed cache so /jobs polling doesn't
    re-tar-open the same files. Returns None on any error -- the
    dashboard falls back to the file-level "done" tag.
    """
    try:
        st = os.stat(tar_path)
    except OSError:
        return None
    cached = _manifest_status_cache.get(tar_path)
    if cached and cached[0] == st.st_mtime:
        return cached[1]
    try:
        with tarfile.open(tar_path, "r") as tf:
            f = tf.extractfile("manifest.json")
            if f is None:
                return None
            data = json.loads(f.read().decode("utf-8", "replace"))
            status = data.get("status")
    except (tarfile.TarError, KeyError, ValueError, OSError):
        status = None
    # Cap cache to a few thousand entries to avoid unbounded growth.
    if len(_manifest_status_cache) > 4096:
        _manifest_status_cache.clear()
    _manifest_status_cache[tar_path] = (st.st_mtime, status)
    return status


def parse_output_name(name):
    tail = name.rsplit("/", 1)[-1]
    digest, ext = os.path.splitext(tail)
    if not SAFE_DIGEST_RE.match(digest):
        return None, None
    if ext and not SAFE_NAME_RE.match(ext.lstrip(".")):
        return None, None
    return digest, ext


def delete_outputs(digest, ext=""):
    """Remove output files for ``digest``.

    DELETE /outputs/<digest> (no ext) drops every artefact AND the
    DONE/<digest>.plan record so the job disappears from /jobs.
    Single-call cleanup after an agent has fetched its artefact is
    the typical flow; calling DELETE /jobs/<digest> separately would
    just be more round-trips for the same outcome.

    DELETE /outputs/<digest>.<ext> (specific ext) drops only that
    file -- e.g. just the .tar or just the .txt -- and leaves the
    job record alone.

    Idempotency rule: only remove DONE/<digest>.plan if at least one
    OUTPUTS file was actually removed. Otherwise this DELETE is
    racing a fresh re-pickup of the same digest (queue_job allows
    re-submit once OUTPUTS are gone, the poller picks it up and
    creates a new DONE/.plan -- and a stale DELETE from the agent's
    "clean up after fetch" flow could otherwise yank that fresh
    DONE/.plan out from under the live session, making the eventual
    artefact upload 409.
    """
    names = ([f"{digest}{ext}"] if ext else [
        n for n in os.listdir(OUTPUTS) if n.startswith(f"{digest}.")
    ])
    removed = 0
    for n in names:
        try:
            os.remove(os.path.join(OUTPUTS, n))
            removed += 1
        except FileNotFoundError:
            pass
    if not ext and removed > 0:
        for tail in (".plan", ".plan.meta"):
            try:
                os.remove(os.path.join(DONE, f"{digest}{tail}"))
            except FileNotFoundError:
                pass
    return removed


# Per-request socket timeout. A handler that reads a body or writes a
# response over a stalled connection holds a thread; without a cap one
# misbehaving client can pin a thread until the OS TCP keepalive
# notices, which can be minutes.
REQUEST_TIMEOUT_S = 600.0


class _BoundedThreadingServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a bounded worker pool.

    The stdlib version spawns one daemon thread per request, unbounded.
    With a misbehaving agent uploading hundreds of MB to a slow disk,
    each thread holds the body in RAM. Cap the spawn rate so the
    server can't blow up under bad load.
    """
    daemon_threads = True
    request_queue_size = 64
    # The base class spawns a thread per request via process_request_-
    # thread; we're keeping that shape but capping concurrency. A
    # busier shape (futures.ThreadPoolExecutor) is overkill for a
    # single-bench tool.
    _max_workers = 16
    _active_workers = 0
    _worker_lock = threading.Lock()

    def process_request(self, request, client_address):
        # If we're at the cap, refuse the connection (close it
        # cleanly) rather than letting the queue grow without bound.
        with self._worker_lock:
            if self._active_workers >= self._max_workers:
                try:
                    request.sendall(
                        b"HTTP/1.1 503 Service Unavailable\r\n"
                        b"Retry-After: 5\r\n"
                        b"Content-Length: 0\r\n\r\n")
                except Exception:
                    pass
                self.shutdown_request(request)
                return
            self._active_workers += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            # Thread.start() can raise (RuntimeError under fd / memory
            # pressure, OSError EAGAIN). Without this rollback the
            # counter pins at _max_workers and every subsequent
            # connection 503s forever -- the bench HTTP wedges with
            # no diagnostic.
            with self._worker_lock:
                self._active_workers -= 1
            raise

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(REQUEST_TIMEOUT_S)
            super().process_request_thread(request, client_address)
        finally:
            with self._worker_lock:
                self._active_workers -= 1


class Handler(BaseHTTPRequestHandler):
    server_version = "test_serv/2"
    # Cap idle-keep-alive read; without this a half-closed connection
    # can hang on rfile.readline() for the OS TCP keepalive interval.
    timeout = REQUEST_TIMEOUT_S

    # Silence the default request log; keep errors only.
    def log_message(self, fmt, *args):
        pass

    # --- dispatch ---

    def do_GET(self):
        path = _request_path(self.path)
        if path is None:
            self.send_response(400); self.end_headers(); return
        if path == "devices":
            return self._send_json(
                _read_file(os.path.join(STATUS, "devices.json")) or b"[]")
        if path == "ops":
            return self._send_json(
                _read_file(os.path.join(STATUS, "ops.json")) or b"{}")
        if path == "inflight":
            return self._send_json(
                _read_file(os.path.join(STATUS, "inflight.json")) or b"[]")
        if path == "bench":
            return self._send_json(
                _read_file(os.path.join(STATUS, "bench.json")) or b"{}")
        if path == "leases":
            return self._send_json(
                _read_file(os.path.join(STATUS, "leases.json")) or b"[]")
        if path == "jobs":
            return self._list_jobs()
        if path == "cancels":
            return self._pull_cancels()
        if path == "examples":
            return self._list_examples()
        if path.startswith("examples/"):
            return self._fetch_example(path[len("examples/"):])
        if path == "help":
            return self._help()
        # Artefact tar extraction endpoints (web UI). Order matters:
        # manifest and file before the bare /outputs/<digest>.<ext>
        # fetch, since they're more specific.
        m = re.match(r"^outputs/([0-9a-f]{64})/manifest$", path)
        if m:
            return self._outputs_manifest(m.group(1))
        m = re.match(r"^outputs/([0-9a-f]{64})/file/(.+)$", path)
        if m:
            return self._outputs_file(m.group(1), m.group(2))
        if path.startswith("outputs/"):
            return self._fetch_output(path[len("outputs/"):])
        if path == "" or path == "index.html":
            return self._serve_static("index.html")
        if path.startswith("web/"):
            return self._serve_static(path[len("web/"):])
        # job pickup: GET /<ext>
        return self._pickup(path)

    def do_POST(self):
        path = _request_path(self.path)
        if path is None:
            self.send_response(400); self.end_headers(); return
        if path == "submit":
            return self._submit_job()
        # /devices/<id>/release
        m = re.match(r"^devices/([A-Za-z0-9._-]+)/release$", path)
        if m:
            return self._mark_release(m.group(1))
        # /sweep -- re-probe + re-verify all devices on the poller
        if path == "sweep":
            return self._mark_sweep()
        # /status/<name>.json -- poller pushes a status snapshot
        m = re.match(r"^status/([A-Za-z0-9._-]+\.json)$", path)
        if m:
            return self._receive_status(m.group(1))
        # artefact upload: /<digest>[.ext]
        return self._artefact(path)

    def do_HEAD(self):
        # Sole HEAD path: /outputs/<digest>.tar -- cheap completion
        # poll for clients waiting on a job to finish. 200 when the
        # tar is on disk, 404 otherwise. No body either way.
        path = _request_path(self.path)
        if path is None:
            self.send_response(400); self.end_headers(); return
        m = re.match(r"^outputs/([0-9a-f]{64})\.tar$", path)
        if not m:
            self.send_response(404)
            self.end_headers()
            return
        tar_path = os.path.join(OUTPUTS, f"{m.group(1)}.tar")
        if os.path.exists(tar_path):
            self.send_response(200)
            self.send_header("Content-Length", str(os.path.getsize(tar_path)))
        else:
            self.send_response(404)
        self.end_headers()

    def do_DELETE(self):
        path = _request_path(self.path)
        if path is None:
            self.send_response(400); self.end_headers(); return
        if path.startswith("outputs/"):
            return self._delete_outputs(path[len("outputs/"):])
        if path == "jobs":
            return self._prune_stale_jobs()
        if path == "jobs/all":
            return self._wipe_all_jobs()
        m = re.match(r"^jobs/([0-9a-f]{64})$", path)
        if m:
            return self._cancel_job(m.group(1))
        self.send_response(404)
        self.end_headers()

    # --- POST /submit ---
    #
    # Accepts either a packed plan tar (plan.txt + blobs) OR a plain
    # plan.txt body for the no-blobs case. The server sniffs the body
    # at submit time, packs a tar around plain text if needed, and
    # queues the result. One endpoint, one URL.

    def _submit_job(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > MAX_PLAN_BYTES:
            return self._send_json(
                json.dumps({"status": "too_large",
                            "limit": MAX_PLAN_BYTES}).encode(), status=413)
        if _disk_full():
            return self._send_json(
                json.dumps({"status": "disk_full"}).encode(), status=507)
        body = self.rfile.read(n) if n else b""
        if not body:
            return self._send_json(
                json.dumps({"status": "empty"}).encode(), status=400)
        if not plan.looks_like_tar(body):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tf:
                ti = tarfile.TarInfo("plan.txt")
                ti.size = len(body)
                tf.addfile(ti, io.BytesIO(body))
            body = buf.getvalue()
        meta = _meta_from_headers(self.headers)
        desc = plan.extract_description(body)
        if desc:
            # Hard-clamp at 4 KiB before round-tripping through the
            # /plan response header. http.client.MAXLINE is 64 KiB,
            # so a 70 KiB description would wedge every poller's
            # GET /plan with LineTooLong and stall the queue. Also
            # strip CR/LF -- a newline in a header value would split
            # the response and let the agent inject arbitrary
            # X-Test-* headers downstream.
            desc = desc.replace("\r", " ").replace("\n", " ")
            meta["description"] = desc[:4096]
        digest, status = queue_job(body, meta)

        if status == "queue_full":
            self.send_response(429)
            self.send_header("Retry-After", "30")
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"status": "queue_full",
                               "limit": MAX_QUEUED_PLANS}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if status == "stale_outputs":
            return self._send_json(
                json.dumps({"status": "stale_outputs",
                            "digest": digest}).encode(), status=409)
        if status == "duplicate":
            return self._send_json(
                json.dumps({"status": "duplicate",
                            "digest": digest}).encode(), status=409)
        return self._send_json(
            json.dumps({"status": "queued",
                        "digest": digest}).encode(), status=201)

    # --- POST /status/<name>.json -- poller publishes its registry view ---

    def _receive_status(self, name):
        if name not in ALLOWED_STATUS_FILES:
            self.send_response(400)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > MAX_STATUS_BYTES:
            self.send_response(413)
            self.end_headers()
            return
        body = self.rfile.read(n) if n else b""
        os.makedirs(STATUS, mode=0o700, exist_ok=True)
        _write_atomic(os.path.join(STATUS, name), body)
        self._send_json(b'{"status":"ok"}')

    # --- Browse an artefact tarball (web UI) ---

    def _outputs_manifest(self, digest):
        tar_path = os.path.join(OUTPUTS, f"{digest}.tar")
        if not os.path.exists(tar_path):
            self.send_response(404); self.end_headers(); return
        members = []
        try:
            with tarfile.open(tar_path, "r") as tf:
                for m in tf.getmembers():
                    if m.isfile():
                        members.append({"name": m.name, "size": m.size})
        except (tarfile.TarError, OSError) as e:
            self.send_response(500); self.end_headers()
            self.wfile.write(f"tar parse error: {e}".encode())
            return
        return self._send_json(json.dumps(members).encode())

    def _outputs_file(self, digest, member):
        # Reject obvious path-traversal attempts. tarfile.getmember will
        # only return members that actually exist in the archive, so the
        # belt is enough; this is the suspenders.
        if ".." in member.split("/") or member.startswith("/"):
            self.send_response(400); self.end_headers(); return
        tar_path = os.path.join(OUTPUTS, f"{digest}.tar")
        if not os.path.exists(tar_path):
            self.send_response(404); self.end_headers(); return
        try:
            with tarfile.open(tar_path, "r") as tf:
                ti = tf.getmember(member)
                if not ti.isfile():
                    self.send_response(400); self.end_headers(); return
                fobj = tf.extractfile(ti)
                data = fobj.read() if fobj is not None else b""
        except KeyError:
            self.send_response(404); self.end_headers(); return
        except (tarfile.TarError, OSError) as e:
            self.send_response(500); self.end_headers()
            self.wfile.write(f"tar read error: {e}".encode())
            return
        # Best-effort content-type. Browser-side renderer treats anything
        # text/* as inline, application/octet-stream as download.
        text_exts = (".log", ".jsonl", ".txt", ".tsv", ".plan")
        ext = os.path.splitext(member)[1].lower()
        if member.endswith(".json"):
            ctype = "application/json"
        elif ext in text_exts:
            ctype = "text/plain; charset=utf-8"
        else:
            ctype = "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         f'inline; filename="{os.path.basename(member)}"')
        self.end_headers()
        self.wfile.write(data)

    # --- GET / + GET /web/<file> -- static UI ---

    def _serve_static(self, rel):
        if not rel or "\x00" in rel:
            self.send_response(400); self.end_headers(); return
        # normpath + a startswith check is enough to prevent traversal --
        # any "../" gets resolved before we compare against WEB_DIR.
        path = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not (path == WEB_DIR or path.startswith(WEB_DIR + os.sep)):
            self.send_response(400); self.end_headers(); return
        body = _read_file(path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        ext = os.path.splitext(path)[1].lower()
        ctype = STATIC_CONTENT_TYPES.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # --- GET / pickup ---

    def _pickup(self, ext):
        if not ext or not SAFE_EXT_RE.match(ext):
            self.send_response(400)
            self.end_headers()
            return
        suffix = f".{ext}"
        # Two pollers (or two workers in one poller) racing on pickup
        # both list INPUTS and could both pick the same name. Make
        # claim-by-rename the atomic gate: rename to DONE first, only
        # the OS-winning rename gets to read the body. Loop a few
        # times so a rename collision doesn't surface as 204 when
        # other plans are still queued.
        for _ in range(8):
            try:
                names = [n for n in os.listdir(INPUTS)
                         if n.endswith(suffix)]
            except FileNotFoundError:
                names = []
            if not names:
                self.send_response(204)
                self.end_headers()
                return
            name = random.choice(names)
            src = os.path.join(INPUTS, name)
            dst = os.path.join(DONE, name)
            try:
                os.rename(src, dst)
            except FileNotFoundError:
                # Another worker grabbed this one between listdir and
                # rename. Loop and try a different file.
                continue
            meta_src = os.path.join(INPUTS, f"{name}.meta")
            meta_dst = os.path.join(DONE, f"{name}.meta")
            try:
                os.rename(meta_src, meta_dst)
            except FileNotFoundError:
                meta_dst = None
            with open(dst, "rb") as f:
                data = f.read()
            meta = (_read_meta(meta_dst)
                    if meta_dst and os.path.exists(meta_dst) else {})
            self.send_response(200)
            for k, v in meta.items():
                self.send_header(f"X-Test-{k.capitalize()}", v)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        # Fell through the retry budget -- treat as "nothing right
        # now"; the poller will retry on its next tick.
        self.send_response(204)
        self.end_headers()

    # --- POST / artefact ---

    def _artefact(self, path):
        tail = path.rsplit("/", 1)[-1]
        digest, url_ext = os.path.splitext(tail)
        # Digest must be a real 64-char hex sha256, not just "safe-ish"
        # filename bytes. Old SAFE_NAME_RE would have accepted dotted
        # paths and other shapes that don't belong in OUTPUTS.
        if not SAFE_DIGEST_RE.match(digest):
            self.send_response(400)
            self.end_headers()
            return
        ext_bare = url_ext.lstrip(".") if url_ext else "tar"
        if ext_bare not in ARTEFACT_EXTS:
            self.send_response(400)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > MAX_ARTEFACT_BYTES:
            self.send_response(413)
            self.end_headers()
            return
        if _disk_full():
            self.send_response(507)
            self.end_headers()
            return
        body = self.rfile.read(n) if n else b""
        # Idempotency gate: refuse the upload if no job record matches
        # the digest. Three signals that a record exists:
        #  - DONE/<digest>.plan  (poller picked it up, mid-flight)
        #  - INPUTS/<digest>.plan  (queued; second poller racing)
        #  - inflight.json contains digest  (defence in depth: a
        #    racing _prune_stale_jobs may have just deleted DONE/.plan
        #    while the session is still running)
        # Without this gate a "agent already fetched + DELETE'd, then
        # a poller-side retry of an earlier attempt belatedly arrives"
        # case would leave a phantom artefact in OUTPUTS and block the
        # next /submit of the same digest with stale_outputs.
        plan_present = (
            os.path.exists(os.path.join(DONE, f"{digest}.plan")) or
            os.path.exists(os.path.join(INPUTS, f"{digest}.plan")) or
            digest in _inflight_digests())
        if not plan_present:
            self.send_response(409)
            self.end_headers()
            self.wfile.write(b"no matching job record; refusing artefact\n")
            return
        # Atomic write so a half-uploaded artefact (poller crashed
        # mid-PUT) never appears in OUTPUTS as a partial file that the
        # dashboard would then try to render.
        _write_atomic(
            os.path.join(OUTPUTS, f"{digest}.{ext_bare}"), body)
        # Drop any stale cancel marker for this digest -- the job is
        # done now, so /cancels won't keep returning the marker and a
        # re-submit of the same digest won't trigger a phantom cancel.
        try:
            os.unlink(os.path.join(CANCEL, digest))
        except FileNotFoundError:
            pass
        self.send_response(200)
        self.end_headers()

    # --- GET/DELETE /outputs/<digest>[.ext] ---

    def _fetch_output(self, name):
        digest, ext = parse_output_name(name)
        if digest is None or not ext:
            self.send_response(400)
            self.end_headers()
            return
        body = _read_file(os.path.join(OUTPUTS, f"{digest}{ext}"))
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        ctype = "application/x-tar"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _delete_outputs(self, name):
        digest, ext = parse_output_name(name)
        if digest is None:
            self.send_response(400)
            self.end_headers()
            return
        removed = delete_outputs(digest, ext)
        return self._send_json(
            json.dumps({"status": "ok", "removed": removed}).encode())

    # --- release ---

    def _mark_sweep(self):
        os.makedirs(SWEEP, mode=0o700, exist_ok=True)
        with open(os.path.join(SWEEP, "now"), "wb"):
            pass
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"sweep queued\n")

    def _mark_release(self, device_id):
        if not SAFE_NAME_RE.match(device_id):
            self.send_response(400)
            self.end_headers()
            return
        os.makedirs(RELEASE, mode=0o700, exist_ok=True)
        with open(os.path.join(RELEASE, device_id), "wb") as f:
            pass
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"marked\n")

    # --- /jobs and /cancels ---

    def _list_jobs(self):
        """Enumerate every job the server has on disk and merge into one
        list. Status taxonomy:
          queued    plan is in INPUTS, poller hasn't picked it up.
          running   plan is in DONE (poller pulled it via /plan) but
                    no artefact in OUTPUTS yet. May actually be done
                    if the poller crashed before posting; the agent
                    can DELETE /jobs/<digest> to give up on it.
          done      at least one file in OUTPUTS for this digest.
        """
        def _try_meta(path):
            return _read_meta(path) if os.path.exists(path) else {}

        jobs = {}
        # Queued
        try:
            for n in os.listdir(INPUTS):
                if not n.endswith(".plan"):
                    continue
                digest = n[:-5]
                if not SAFE_DIGEST_RE.match(digest):
                    continue
                try:
                    st = os.stat(os.path.join(INPUTS, n))
                except OSError:
                    continue
                jobs[digest] = {
                    "digest": digest,
                    "status": "queued",
                    "queued_at": st.st_mtime,
                    "size_bytes": st.st_size,
                    "meta": _try_meta(
                        os.path.join(INPUTS, f"{n}.meta")),
                }
        except FileNotFoundError:
            pass
        # Picked up but maybe not finished
        try:
            for n in os.listdir(DONE):
                if not n.endswith(".plan"):
                    continue
                digest = n[:-5]
                if not SAFE_DIGEST_RE.match(digest) or digest in jobs:
                    continue
                try:
                    st = os.stat(os.path.join(DONE, n))
                except OSError:
                    continue
                jobs[digest] = {
                    "digest": digest,
                    "status": "running",
                    "picked_up_at": st.st_mtime,
                    "size_bytes": st.st_size,
                    "meta": _try_meta(
                        os.path.join(DONE, f"{n}.meta")),
                }
        except FileNotFoundError:
            pass
        # Output present -> done; promote any "running" entry
        try:
            seen_outputs = {}
            for n in os.listdir(OUTPUTS):
                digest, _ = os.path.splitext(n)
                if not SAFE_DIGEST_RE.match(digest):
                    continue
                try:
                    st = os.stat(os.path.join(OUTPUTS, n))
                except OSError:
                    continue
                prev = seen_outputs.get(digest, 0)
                if st.st_mtime > prev:
                    seen_outputs[digest] = st.st_mtime
            for digest, mtime in seen_outputs.items():
                entry = jobs.get(digest, {"digest": digest, "size_bytes": 0})
                entry["status"] = "done"
                entry["completed_at"] = mtime
                # Pull manifest.status out of the .tar so the dashboard
                # can distinguish ok / inert / errors / failed /
                # canceled. Cheap: small tar, manifest.json is the
                # first member usually. Failures here just leave the
                # entry as "done"; the agent can fetch the tar to see.
                tar_path = os.path.join(OUTPUTS, f"{digest}.tar")
                ms = _peek_manifest_status(tar_path)
                if ms:
                    entry["manifest_status"] = ms
                jobs[digest] = entry
        except FileNotFoundError:
            pass
        # Cancel-pending markers (in-flight cancels not yet acked)
        try:
            for n in os.listdir(CANCEL):
                if SAFE_DIGEST_RE.match(n) and n in jobs:
                    jobs[n]["cancel_pending"] = True
        except FileNotFoundError:
            pass
        out = sorted(
            jobs.values(),
            key=lambda j: max(j.get("queued_at", 0),
                              j.get("picked_up_at", 0),
                              j.get("completed_at", 0)),
            reverse=True)
        return self._send_json(json.dumps(out).encode())

    def _cancel_job(self, digest):
        """If the digest is queued (still in INPUTS), unlink it -- the
        poller will never pick it up. Otherwise, drop a marker in
        CANCEL for the poller to pull via /cancels. The artefact for an
        in-flight cancel will eventually appear in OUTPUTS with the
        cancel reason in errors.log.
        """
        # 1) queued? unlink immediately.
        # Only the .plan unlink drives the canceled_queued signal.
        # _pickup renames .plan first then .meta, so there's a brief
        # window where INPUTS has only .meta left -- if we counted a
        # .meta unlink as a successful cancel, we'd lie to the operator
        # when in fact the poller has already taken the .plan from
        # under us and is about to dispatch.
        queued_path = os.path.join(INPUTS, f"{digest}.plan")
        meta_path = queued_path + ".meta"
        canceled_queued = False
        try:
            os.unlink(queued_path)
            canceled_queued = True
        except FileNotFoundError:
            pass
        try:
            os.unlink(meta_path)
        except FileNotFoundError:
            pass
        if canceled_queued:
            return self._send_json(
                json.dumps({"status": "canceled_queued",
                            "digest": digest}).encode())
        # 2) in-flight? leave a marker, poller will see it.
        if os.path.exists(os.path.join(DONE, f"{digest}.plan")):
            os.makedirs(CANCEL, mode=0o700, exist_ok=True)
            with open(os.path.join(CANCEL, digest), "wb"):
                pass
            return self._send_json(
                json.dumps({"status": "cancel_signaled",
                            "digest": digest}).encode())
        # 3) unknown digest. Could be: never submitted, or already
        # done and DONE/.plan was cleaned up. Either way, nothing to
        # cancel.
        self.send_response(404)
        self.end_headers()

    def _prune_stale_jobs(self):
        removed = prune_stale_jobs()
        return self._send_json(
            json.dumps({"status": "ok", "removed": removed}).encode())

    def _wipe_all_jobs(self):
        """Nuke INPUTS, DONE, OUTPUTS, and CANCEL. Drops every job
        record the server has -- queued, running, done. A poller
        currently dispatching a job will still post the artefact
        when its session ends; that re-introduces the digest as
        a fresh `done` entry. This endpoint is just history reset,
        NOT a force-cancel; use the per-job cancel button to
        actually abort an in-flight session.
        """
        counts = {"inputs": 0, "done": 0, "outputs": 0, "cancel": 0}
        for label, d in (("inputs", INPUTS), ("done", DONE),
                         ("outputs", OUTPUTS), ("cancel", CANCEL)):
            try:
                names = list(os.listdir(d))
            except FileNotFoundError:
                continue
            for n in names:
                try:
                    os.unlink(os.path.join(d, n))
                    counts[label] += 1
                except (FileNotFoundError, IsADirectoryError):
                    pass
        return self._send_json(
            json.dumps({"status": "ok", **counts}).encode())

    def _pull_cancels(self):
        """Return the current set of cancel markers. Markers are NOT
        deleted on read -- the poller's signal_cancel is idempotent,
        and persistent markers close the pickup-vs-dispatch race
        (cancel arrives while a worker is mid-dispatch but hasn't
        registered the Session yet, so the next tick re-finds it).
        Markers are unlinked when the matching artefact arrives
        (see _artefact). Old markers are GC'd here as a safety net
        for jobs that never produced an artefact. The TTL must be
        longer than MAX_SESSION_S (3600s) plus upload buffer --
        otherwise a long-running session that gets a cancel can
        have the marker GC'd before the session even reaches a
        check point. 90 minutes is conservative.
        """
        cutoff = time.time() - 5400.0
        try:
            names = [n for n in os.listdir(CANCEL) if SAFE_DIGEST_RE.match(n)]
        except FileNotFoundError:
            names = []
        live = []
        for n in names:
            p = os.path.join(CANCEL, n)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.unlink(p)
                else:
                    live.append(n)
            except FileNotFoundError:
                pass
        return self._send_json(json.dumps(live).encode())

    # --- examples ---

    def _list_examples(self):
        # Only list plans whose @blob references all resolve in
        # examples/. The dashboard offers these in a dropdown; if a
        # plan that needs blobs we don't ship lands there, the
        # operator picks it, clicks submit, and gets an opaque 404.
        # Better to just hide it. Plans with no @blob refs (the
        # majority of bundled examples: inventory, lease_*, dsp_uart,
        # fpga_uart, fpga_blink, ...) always pass.
        try:
            entries = sorted(
                e for e in os.listdir(EXAMPLES) if e.endswith(".plan")
            )
        except FileNotFoundError:
            return self._send_json(b"[]")
        out = []
        for name in entries:
            text = _read_file(os.path.join(EXAMPLES, name))
            if text is None:
                continue
            try:
                refs = plan.collect_blob_refs(text.decode("utf-8"))
            except Exception:
                refs = set()
            missing = [
                r for r in refs
                if not os.path.exists(os.path.join(EXAMPLES, r))]
            if missing:
                continue
            out.append(name)
        return self._send_json(json.dumps(out).encode())

    def _fetch_example(self, name):
        # Two forms:
        #   foo.plan       -> raw plan text (text/plain), for the
        #                     dashboard's textarea + edit flow.
        #   foo.plan.tar   -> packed tar (plan.txt + every @blob the
        #                     plan references, pulled from examples/),
        #                     ready to POST straight to /submit. Lets
        #                     the dashboard run blob-bearing examples
        #                     end-to-end without an FS roundtrip.
        if name.endswith(".plan.tar"):
            return self._fetch_example_packed(name[:-4])
        if not name.endswith(".plan") or not SAFE_NAME_RE.match(name):
            self.send_response(400)
            self.end_headers()
            return
        body = _read_file(os.path.join(EXAMPLES, name))
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fetch_example_packed(self, name):
        if not name.endswith(".plan") or not SAFE_NAME_RE.match(name):
            self.send_response(400)
            self.end_headers()
            return
        plan_path = os.path.join(EXAMPLES, name)
        plan_text = _read_file(plan_path)
        if plan_text is None:
            self.send_response(404)
            self.end_headers()
            return
        # Parse to find @blob refs; reject if anything fails so a
        # broken example doesn't ship a half-built tar.
        try:
            ops = plan.parse_text(plan_text.decode("utf-8"))
        except plan.PlanError as e:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(f"plan parse failed: {e}".encode())
            return
        wanted = set()
        for op in ops:
            for v in op.args.values():
                if v.kind == "blob":
                    wanted.add(v.raw)
        # Pull each referenced blob from examples/. Reject if any is
        # missing so the agent doesn't get a "blob not in tar" failure
        # at run time -- they'd have to read the artefact to find out
        # the example was incomplete.
        blobs = {}
        cumulative = len(plan_text)
        for blob_name in sorted(wanted):
            if not SAFE_NAME_RE.match(blob_name):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(
                    f"unsafe blob name {blob_name!r}".encode())
                return
            data = _read_file(os.path.join(EXAMPLES, blob_name))
            if data is None:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(
                    f"example {name!r} references @{blob_name} "
                    f"which is not in examples/".encode())
                return
            cumulative += len(data)
            # Cap the resulting tar at MAX_PLAN_BYTES so a giant
            # blob in examples/ can't be auto-packed into a
            # multi-GiB response that OOMs the server. The agent who
            # wants this can still pack manually via submit.py.
            if cumulative > MAX_PLAN_BYTES:
                self.send_response(413)
                self.end_headers()
                self.wfile.write(
                    f"example {name!r} packed >{MAX_PLAN_BYTES}B "
                    f"(blobs too large); use submit.py + --blob "
                    f"manually".encode())
                return
            blobs[blob_name] = data
        body = plan.pack_tar(plan_text.decode("utf-8"), blobs)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _help(self):
        body = (b"test_serv: see README.md for the REST API.\n"
                b"Plan grammar + per-plugin op signatures: run a plan "
                b"containing `inventory` and read bench.ops.json from "
                b"the artefact.\n")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- util ---

    def _send_json(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    # Default port honours $TEST_SERV_PORT so a single shell-level env
    # can drive both server and poller (poller already reads the same
    # var). --port still wins if explicitly passed.
    default_port = int(os.environ.get("TEST_SERV_PORT", "8080"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=default_port)
    args = ap.parse_args()
    for d in (INPUTS, OUTPUTS, DONE, STATUS, RELEASE, SWEEP, CANCEL):
        os.makedirs(d, mode=0o700, exist_ok=True)
    print(f"state dir: {STATE_DIR}")
    threading.Thread(target=_gc_loop, daemon=True).start()
    try:
        srv = _BoundedThreadingServer(
            ("127.0.0.1", args.port), Handler)
    except OSError as e:
        # EADDRINUSE: print one actionable line instead of a stock
        # OSError traceback. Most common path for an operator
        # accidentally double-launching the server.
        if e.errno in (98, 48):  # EADDRINUSE Linux/BSD
            print(f"port {args.port} is already in use. Either stop "
                  f"the existing process or pass --port=N to pick "
                  f"another.", file=sys.stderr)
            sys.exit(2)
        raise
    print(f"listening on 127.0.0.1:{args.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
