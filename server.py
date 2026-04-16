# SPDX-License-Identifier: MIT
# server.py --- Minimal REST server for SHARC+ remote testing
# Copyright (c) 2026 Jakob Kastelic

import os
import random
from http.server import HTTPServer, BaseHTTPRequestHandler


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        names = os.listdir("inputs")
        if not names:
            self.send_response(204)
            self.end_headers()
            return
        name = random.choice(names)
        src = os.path.join("inputs", name)
        data = open(src, "rb").read()
        os.rename(src, os.path.join("done", name))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        tail = self.path.rsplit("/", 1)[-1]
        digest, url_ext = os.path.splitext(tail)
        ext = url_ext or ".txt"
        n = int(self.headers["Content-Length"])
        with open(os.path.join("outputs", f"{digest}{ext}"), "wb") as f:
            f.write(self.rfile.read(n))
        self.send_response(200)
        self.end_headers()


for d in ("inputs", "outputs", "done"):
    os.makedirs(d, exist_ok=True)
HTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
