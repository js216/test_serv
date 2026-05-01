# SPDX-License-Identifier: MIT
# server.py --- Job broker + introspection endpoints
# Copyright (c) 2026 Jakob Kastelic

import argparse
import hashlib
import io
import json
import os
import random
import re
import tarfile
import tempfile
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler


import paths

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
# Static UI lives next to server.py.
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
ALLOWED_STATUS_FILES = ("devices.json", "ops.json", "leases.json")


SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_EXT_RE = re.compile(r"^[A-Za-z0-9]+$")
SAFE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


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


def _write_atomic(path, body):
    # Unique tempfile per call so concurrent writers don't collide on a
    # shared "<path>.inprogress" name; ThreadingHTTPServer dispatches
    # multiple POST /status/<...> in parallel, and the second would
    # FileNotFoundError when its tmp got renamed away by the first.
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(
        dir=d, prefix=os.path.basename(path) + ".", suffix=".inprogress")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def queue_job(body, meta=None):
    digest = hashlib.sha256(body).hexdigest()
    dst = os.path.join(INPUTS, f"{digest}.plan")
    stale = [
        n for n in os.listdir(OUTPUTS)
        if n.startswith(f"{digest}.")
    ]
    if stale:
        return digest, "stale_outputs"
    if os.path.exists(dst):
        return digest, "duplicate"

    meta_path = f"{dst}.meta"
    try:
        os.remove(meta_path)
    except FileNotFoundError:
        pass
    if meta:
        with open(meta_path, "w") as f:
            f.write("".join(f"{k}={v}\n" for k, v in meta.items()))
            f.flush()
            os.fsync(f.fileno())
    _write_atomic(dst, body)
    return digest, "queued"


def parse_output_name(name):
    tail = name.rsplit("/", 1)[-1]
    digest, ext = os.path.splitext(tail)
    if not SAFE_DIGEST_RE.match(digest):
        return None, None
    if ext and not SAFE_NAME_RE.match(ext.lstrip(".")):
        return None, None
    return digest, ext


def delete_outputs(digest, ext=""):
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
    # When the caller drops *all* outputs for a digest (no specific
    # ext), also clear the DONE/<digest>.plan record. Otherwise the
    # job sits in the listing as "running" forever -- the poller
    # picked it up at some point and the .plan stayed in DONE even
    # though everyone else has forgotten about it.
    if not ext:
        for tail in (".plan", ".plan.meta"):
            try:
                os.remove(os.path.join(DONE, f"{digest}{tail}"))
            except FileNotFoundError:
                pass
    return removed


class Handler(BaseHTTPRequestHandler):
    server_version = "test_serv/2"

    # Silence the default request log; keep errors only.
    def log_message(self, fmt, *args):
        pass

    # --- dispatch ---

    def do_GET(self):
        path = self.path.lstrip("/")
        if path == "devices":
            return self._send_json(
                _read_file(os.path.join(STATUS, "devices.json")) or b"[]")
        if path == "ops":
            return self._send_json(
                _read_file(os.path.join(STATUS, "ops.json")) or b"{}")
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
        if path == "scope/signals":
            return self._scope_signals()
        if path == "" or path == "index.html":
            return self._serve_static("index.html")
        if path.startswith("web/"):
            return self._serve_static(path[len("web/"):])
        # job pickup: GET /<ext>
        return self._pickup(path)

    def do_POST(self):
        path = self.path.lstrip("/")
        if path == "submit":
            return self._submit_job()
        if path == "submit-text":
            return self._submit_text_job()
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

    def do_DELETE(self):
        path = self.path.lstrip("/")
        if path.startswith("outputs/"):
            return self._delete_outputs(path[len("outputs/"):])
        if path == "jobs":
            return self._prune_stale_jobs()
        m = re.match(r"^jobs/([0-9a-f]{64})$", path)
        if m:
            return self._cancel_job(m.group(1))
        self.send_response(404)
        self.end_headers()

    # --- POST /submit ---

    def _submit_job(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        meta = {}
        for k, v in self.headers.items():
            if k.lower().startswith("x-test-"):
                meta[k[len("X-Test-"):].lower()] = v
        digest, status = queue_job(body, meta)

        if status == "stale_outputs":
            return self._send_json(
                json.dumps({
                    "status": "stale_outputs",
                    "digest": digest,
                }).encode(), status=409)
        if status == "duplicate":
            return self._send_json(
                json.dumps({
                    "status": "duplicate",
                    "digest": digest,
                }).encode(), status=409)
        return self._send_json(
            json.dumps({
                "status": "queued",
                "digest": digest,
            }).encode(), status=201)

    # --- POST /submit-text -- pack a plain plan.txt body server-side.
    # Convenience for the web UI and any agent that doesn't want to
    # build a tar; no blobs supported. Equivalent to /submit with a
    # `plan.txt`-only tarball.
    def _submit_text_job(self):
        n = int(self.headers.get("Content-Length") or 0)
        text = self.rfile.read(n) if n else b""
        if not text:
            return self._send_json(
                json.dumps({"status": "empty"}).encode(), status=400)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            ti = tarfile.TarInfo("plan.txt")
            ti.size = len(text)
            tf.addfile(ti, io.BytesIO(text))
        body = buf.getvalue()
        meta = {}
        for k, v in self.headers.items():
            if k.lower().startswith("x-test-"):
                meta[k[len("X-Test-"):].lower()] = v
        digest, status = queue_job(body, meta)
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
        self.end_headers()
        self.wfile.write(body)

    # --- GET / pickup ---

    def _pickup(self, ext):
        if not ext or not SAFE_EXT_RE.match(ext):
            self.send_response(400)
            self.end_headers()
            return
        suffix = f".{ext}"
        names = [n for n in os.listdir(INPUTS) if n.endswith(suffix)]
        if not names:
            self.send_response(204)
            self.end_headers()
            return
        name = random.choice(names)
        src = os.path.join(INPUTS, name)
        meta_src = os.path.join(INPUTS, f"{name}.meta")
        with open(src, "rb") as f:
            data = f.read()
        meta = _read_meta(meta_src) if os.path.exists(meta_src) else {}
        self.send_response(200)
        for k, v in meta.items():
            self.send_header(f"X-Test-{k.capitalize()}", v)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        os.rename(src, os.path.join(DONE, name))
        if os.path.exists(meta_src):
            os.rename(meta_src, os.path.join(DONE, f"{name}.meta"))

    # --- POST / artefact ---

    def _artefact(self, path):
        tail = path.rsplit("/", 1)[-1]
        digest, url_ext = os.path.splitext(tail)
        if not SAFE_NAME_RE.match(digest):
            self.send_response(400)
            self.end_headers()
            return
        ext = url_ext or ".txt"
        if not SAFE_NAME_RE.match(ext.lstrip(".")):
            self.send_response(400)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        with open(os.path.join(OUTPUTS, f"{digest}{ext}"), "wb") as f:
            f.write(body)
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
        ctype = "application/json" if ext == ".txt" else "application/x-tar"
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
        queued_path = os.path.join(INPUTS, f"{digest}.plan")
        meta_path = queued_path + ".meta"
        canceled_queued = False
        for p in (queued_path, meta_path):
            try:
                os.unlink(p)
                canceled_queued = True
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
        """Remove DONE/<digest>.plan files whose digests have no
        OUTPUTS. These are the "running forever" entries -- the
        poller picked the job up at some point but no artefact is
        on the server (either the agent already DELETE'd it after
        fetching, or the poller crashed / restarted before posting).
        Currently-truly-running jobs have no OUTPUTS yet and would
        also match; the brief blip of disappearing from the listing
        until the artefact arrives is harmless -- the poller doesn't
        reference DONE/.plan after pickup.
        """
        have_output = set()
        try:
            for n in os.listdir(OUTPUTS):
                d, _ = os.path.splitext(n)
                if SAFE_DIGEST_RE.match(d):
                    have_output.add(d)
        except FileNotFoundError:
            pass
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
                try:
                    os.unlink(os.path.join(DONE, n))
                    removed += 1
                except FileNotFoundError:
                    pass
                try:
                    os.unlink(os.path.join(DONE, f"{digest}.plan.meta"))
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
        return self._send_json(
            json.dumps({"status": "ok", "removed": removed}).encode())

    def _pull_cancels(self):
        """Return the current set of cancel markers and remove them.
        The poller calls this on its main loop tick; the markers are
        consumed atomically so duplicate signals don't fire.
        """
        try:
            names = [n for n in os.listdir(CANCEL) if SAFE_DIGEST_RE.match(n)]
        except FileNotFoundError:
            names = []
        for n in names:
            try:
                os.unlink(os.path.join(CANCEL, n))
            except FileNotFoundError:
                pass
        return self._send_json(json.dumps(names).encode())

    # --- examples ---

    def _list_examples(self):
        try:
            entries = sorted(
                e for e in os.listdir(EXAMPLES) if e.endswith(".plan")
            )
        except FileNotFoundError:
            entries = []
        return self._send_json(json.dumps(entries).encode())

    def _scope_signals(self):
        """Expose scope.signals from config.json so agents know which
        channel carries which bench signal and what active threshold
        applies. Served straight from the config file at request time
        so operator edits show up without server restart.
        """
        # Read config.json inline: server.py has no config.py import
        # chain to avoid binding to the poller process.
        cfg_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.json")
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
        except FileNotFoundError:
            cfg = {}
        except Exception:
            return self._send_json(b"{}")
        signals = (cfg.get("scope") or {}).get("signals") or {}
        return self._send_json(json.dumps(signals, indent=2).encode())

    def _fetch_example(self, name):
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

    # --- util ---

    def _send_json(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    for d in (INPUTS, OUTPUTS, DONE, STATUS, RELEASE, SWEEP, CANCEL):
        os.makedirs(d, mode=0o700, exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
