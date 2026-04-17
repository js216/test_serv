# SPDX-License-Identifier: MIT
# server.py --- Generic test-job dispatcher (multi-kind, routed by extension)
# Copyright (c) 2026 Jakob Kastelic

import os
import random
from http.server import HTTPServer, BaseHTTPRequestHandler


STATE_DIR = os.environ.get(
    "TEST_SERV_DIR",
    f"/tmp/test_serv-{os.getenv('USER', 'anon')}",
)
INPUTS = os.path.join(STATE_DIR, "inputs")
OUTPUTS = os.path.join(STATE_DIR, "outputs")
DONE = os.path.join(STATE_DIR, "done")


def read_meta(path):
    meta = {}
    with open(path) as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k:
                meta[k] = v
    return meta


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ext = self.path.strip("/")
        if not ext or not ext.isalnum():
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
        meta = read_meta(meta_src) if os.path.exists(meta_src) else {}
        self.send_response(200)
        for k, v in meta.items():
            self.send_header(f"X-Test-{k.capitalize()}", v)
        self.end_headers()
        self.wfile.write(data)
        os.rename(src, os.path.join(DONE, name))
        if os.path.exists(meta_src):
            os.rename(meta_src, os.path.join(DONE, f"{name}.meta"))

    def do_POST(self):
        tail = self.path.rsplit("/", 1)[-1]
        digest, url_ext = os.path.splitext(tail)
        ext = url_ext or ".txt"
        n = int(self.headers["Content-Length"])
        with open(os.path.join(OUTPUTS, f"{digest}{ext}"), "wb") as f:
            f.write(self.rfile.read(n))
        self.send_response(200)
        self.end_headers()


for d in (INPUTS, OUTPUTS, DONE):
    os.makedirs(d, mode=0o700, exist_ok=True)
HTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
