# SPDX-License-Identifier: MIT
# submit.py --- Submit a .plan job and collect artefacts
# Copyright (c) 2026 Jakob Kastelic

import argparse
import io
import json
import os
import sys
import tarfile
import time
import urllib.error
import urllib.request

from plan import pack_tar


GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

DEFAULT_SERVER = os.environ.get("TEST_SERV_URL", "http://localhost:8080")


class StaleOutputsError(Exception):
    pass


def _pack_from_plan(plan_path, blob_specs):
    with open(plan_path, "r", encoding="utf-8") as f:
        text = f.read()
    blobs = {}
    for spec in blob_specs or []:
        name, _, src = spec.partition("=")
        if not name or not src:
            raise ValueError(f"--blob expects NAME=PATH, got {spec!r}")
        with open(src, "rb") as f:
            blobs[name] = f.read()
    return pack_tar(text, blobs)


def _url(base, path):
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _request(method, url, data=None, headers=None):
    req = urllib.request.Request(
        url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req) as r:
        return r.status, r.read(), dict(r.headers)


def _http_json(method, url, data=None, headers=None):
    status, body, hdrs = _request(method, url, data, headers)
    return status, json.loads(body.decode() or "{}"), hdrs


def _submit(data, meta, server):
    headers = {"Content-Type": "application/octet-stream"}
    for k, v in meta.items():
        headers[f"X-Test-{k}"] = v
    try:
        _status, body, _hdrs = _http_json(
            "POST", _url(server, "submit"), data=data, headers=headers)
    except urllib.error.HTTPError as e:
        err = json.loads(e.read().decode() or "{}")
        digest = err.get("digest", "")
        if e.code == 409 and err.get("status") == "stale_outputs":
            raise StaleOutputsError(digest)
        if e.code == 409 and err.get("status") == "duplicate":
            raise FileExistsError(digest)
        raise
    return body["digest"]


def _get_output(server, digest, ext):
    try:
        _status, body, _hdrs = _request(
            "GET", _url(server, f"outputs/{digest}.{ext}"))
        return body
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _delete_outputs(server, digest):
    try:
        _request("DELETE", _url(server, f"outputs/{digest}"))
    except urllib.error.HTTPError:
        pass


def _wait(server, digest, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        txt = _get_output(server, digest, "txt")
        if txt is not None:
            tar = _get_output(server, digest, "tar")
            return {"txt": txt, "tar": tar}
        time.sleep(0.05)
    return None


def _summarize_tar(data):
    """Print the manifest and timeline from an artefact tarball."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        members = tf.getnames()
        manifest_m = tf.extractfile("manifest.json")
        if manifest_m is not None:
            sys.stdout.buffer.write(b"=== manifest.json ===\n")
            sys.stdout.buffer.write(manifest_m.read())
        tl = tf.extractfile("timeline.log")
        if tl is not None:
            sys.stdout.buffer.write(b"\n=== timeline.log ===\n")
            sys.stdout.buffer.write(tl.read())
        if "errors.log" in members:
            err = tf.extractfile("errors.log")
            if err is not None:
                sys.stdout.buffer.write(b"\n=== errors.log ===\n")
                sys.stdout.buffer.write(err.read())
        sys.stdout.buffer.write(b"\n=== tarball members ===\n")
        for n in members:
            sys.stdout.buffer.write(f"  {n}\n".encode())


def _extract(data, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        for m in tf.getmembers():
            if ".." in m.name or m.name.startswith("/"):
                raise RuntimeError(f"unsafe member {m.name!r}")
        try:
            tf.extractall(out_dir, filter="data")
        except TypeError:
            # Python < 3.12: no filter kwarg.
            tf.extractall(out_dir)


def _dump_outputs(outputs, digest, extract_to):
    txt_bytes = outputs.get("txt")
    tar_bytes = outputs.get("tar")
    if txt_bytes is not None:
        sys.stdout.buffer.write(b"=== sentinel .txt ===\n")
        sys.stdout.buffer.write(txt_bytes)
        if not txt_bytes.endswith(b"\n"):
            sys.stdout.buffer.write(b"\n")
    if tar_bytes is not None:
        _summarize_tar(tar_bytes)
        if extract_to is not None:
            _extract(tar_bytes, extract_to)
            tar_path = os.path.join(extract_to, f"{digest}.tar")
            with open(tar_path, "wb") as f:
                f.write(tar_bytes)
            sys.stdout.buffer.write(
                f"\nextracted to {extract_to}\n".encode())
    sys.stdout.buffer.flush()
    return txt_bytes


def _compare(txt_bytes, expected_path):
    with open(expected_path, "rb") as f:
        expected = f.read()
    if txt_bytes == expected:
        print(f"{GREEN}SUCCESS{RESET}")
        return 0
    print(f"{RED}FAIL{RESET}")
    return 1


def _fetch(server, digest, expected_path, extract_to):
    txt = _get_output(server, digest, "txt")
    tar = _get_output(server, digest, "tar")
    if txt is None and tar is None:
        print(f"no outputs for digest {digest}", file=sys.stderr)
        return 1
    txt = _dump_outputs({"txt": txt, "tar": tar}, digest, extract_to)
    _delete_outputs(server, digest)
    if expected_path is not None:
        return _compare(txt, expected_path)
    return 0


def _parse_meta_kv(pairs):
    meta = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        if not k or not v:
            raise ValueError(f"--meta expects key=value, got {p!r}")
        meta[k] = v
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", nargs="?",
                    help="path to a plan.txt (blobs added via --blob) or "
                         "a pre-packed .plan tarball")
    ap.add_argument("--blob", action="append", metavar="NAME=PATH",
                    help="add a blob to the job tar; reference as @NAME "
                         "in plan. Repeatable.")
    ap.add_argument("--fetch", metavar="DIGEST",
                    help="fetch artefacts for a previously-submitted digest")
    ap.add_argument("--server", default=DEFAULT_SERVER,
                    help="test_serv HTTP base URL "
                         f"(default: {DEFAULT_SERVER})")
    ap.add_argument("--wait", type=float,
                    help="block up to N seconds for artefacts")
    ap.add_argument("--expected", help="compare sentinel .txt against this")
    ap.add_argument("--extract", metavar="DIR",
                    help="extract artefact tarball into DIR (keeps the tar)")
    ap.add_argument("--meta", action="append", metavar="KEY=VAL",
                    help="sidecar metadata (X-Test-<Key>), repeatable")
    ap.add_argument("--runtime", type=float,
                    help="shortcut for --meta runtime=SEC")
    args = ap.parse_args()

    if args.fetch and args.plan:
        ap.error("--fetch is mutually exclusive with a plan file")
    if not args.fetch and not args.plan:
        ap.error("either a plan file or --fetch DIGEST is required")

    if args.fetch:
        return _fetch(args.server, args.fetch, args.expected, args.extract)

    # .plan = already-packed tar, otherwise treat as plan.txt + blobs.
    if args.plan.endswith(".plan"):
        with open(args.plan, "rb") as f:
            data = f.read()
    else:
        data = _pack_from_plan(args.plan, args.blob)

    meta = _parse_meta_kv(args.meta)
    if args.runtime is not None:
        meta["runtime"] = str(args.runtime)

    try:
        digest = _submit(data, meta, args.server)
    except StaleOutputsError as e:
        print(f"output stale; run:\n    python3 submit.py --fetch {e}",
              file=sys.stderr)
        return 2
    except FileExistsError as e:
        print(f"duplicate job: {e}", file=sys.stderr)
        return 2

    if args.wait is None:
        print(digest)
        return 0

    outputs = _wait(args.server, digest, args.wait)
    if outputs is None:
        print(f"timeout waiting for {digest}", file=sys.stderr)
        return 1

    txt = _dump_outputs(outputs, digest, args.extract)
    _delete_outputs(args.server, digest)
    if args.expected is not None:
        return _compare(txt, args.expected)
    return 0


if __name__ == "__main__":
    sys.exit(main())
