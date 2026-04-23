# SPDX-License-Identifier: MIT
# server.py --- Job broker + introspection endpoints
# Copyright (c) 2026 Jakob Kastelic

import argparse
import json
import os
import random
import re
from http.server import HTTPServer, BaseHTTPRequestHandler


import paths

STATE_DIR = paths.state_dir()
INPUTS = os.path.join(STATE_DIR, "inputs")
OUTPUTS = os.path.join(STATE_DIR, "outputs")
DONE = os.path.join(STATE_DIR, "done")
STATUS = os.path.join(STATE_DIR, "status")
RELEASE = os.path.join(STATE_DIR, "release")
SWEEP = os.path.join(STATE_DIR, "sweep")

# Examples dir is relative to this file -- bench-operator-owned starter plans.
EXAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples")


SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_EXT_RE = re.compile(r"^[A-Za-z0-9]+$")


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
        if path == "examples":
            return self._list_examples()
        if path.startswith("examples/"):
            return self._fetch_example(path[len("examples/"):])
        # job pickup: GET /<ext>
        return self._pickup(path)

    def do_POST(self):
        path = self.path.lstrip("/")
        # /devices/<id>/release
        m = re.match(r"^devices/([A-Za-z0-9._-]+)/release$", path)
        if m:
            return self._mark_release(m.group(1))
        # /sweep -- re-probe + re-verify all devices on the poller
        if path == "sweep":
            return self._mark_sweep()
        # artefact upload: /<digest>[.ext]
        return self._artefact(path)

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

    # --- examples ---

    def _list_examples(self):
        try:
            entries = sorted(
                e for e in os.listdir(EXAMPLES) if e.endswith(".plan")
            )
        except FileNotFoundError:
            entries = []
        return self._send_json(json.dumps(entries).encode())

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

    def _send_json(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    for d in (INPUTS, OUTPUTS, DONE, STATUS, RELEASE, SWEEP):
        os.makedirs(d, mode=0o700, exist_ok=True)
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
