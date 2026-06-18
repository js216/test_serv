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

import plan
from plan import pack_tar


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
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.URLError as e:
        # HTTPError is a subclass of URLError; let it propagate so
        # _submit can decode the structured 4xx body. Plain URLError
        # (connection refused, DNS failure, SSH tunnel closed) is the
        # operator-runs-submit-before-server-is-up trap; surface it
        # as a one-line RuntimeError instead of an opaque traceback.
        if isinstance(e, urllib.error.HTTPError):
            raise
        reason = getattr(e, "reason", e)
        raise RuntimeError(
            f"cannot reach test_serv at {url}: {reason}") from None


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
        try:
            err = json.loads(e.read().decode() or "{}")
        except ValueError:
            err = {}
        digest = err.get("digest", "")
        if e.code == 409 and err.get("status") == "stale_outputs":
            raise StaleOutputsError(digest)
        if e.code == 409 and err.get("status") == "duplicate":
            raise FileExistsError(digest)
        # The server tags 4xx/5xx responses with a structured status
        # (too_large/queue_full/disk_full/empty/...). Surface it as a
        # one-line RuntimeError instead of an opaque urllib traceback,
        # which is what an operator running submit.py from a shell
        # actually wants to see.
        status = err.get("status")
        limit = err.get("limit")
        if status:
            tail = f" (limit={limit})" if limit is not None else ""
            raise RuntimeError(
                f"submit refused: {e.code} {status}{tail}")
        raise
    return body["digest"]


def _get_tar(server, digest):
    try:
        _status, body, _hdrs = _request(
            "GET", _url(server, f"outputs/{digest}.tar"))
        return body
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _head_tar(server, digest):
    """Cheap completion poll. Returns True iff the tar exists on the
    server. Used by --wait to avoid downloading the full artefact on
    every poll tick.
    """
    try:
        _request("HEAD", _url(server, f"outputs/{digest}.tar"))
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def _job_known(server, digest):
    """Return True while the server still advertises an accepted job.

    A canceled or stale-resolved job can disappear without publishing an
    output tar. In that case --wait should return promptly instead of
    polling HEAD /outputs/<digest>.tar until the outer timeout expires.
    """
    _status, body, _hdrs = _http_json("GET", _url(server, "jobs"))
    jobs = body if isinstance(body, list) else body.get("jobs", [])
    return any(isinstance(job, dict) and job.get("digest") == digest
               for job in jobs)


def _delete_outputs(server, digest):
    try:
        _request("DELETE", _url(server, f"outputs/{digest}"))
    except urllib.error.HTTPError:
        pass


def _wait(server, digest, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _head_tar(server, digest):
                return _get_tar(server, digest)
            if not _job_known(server, digest):
                return None
        except (RuntimeError, urllib.error.URLError, ConnectionError,
                TimeoutError) as e:
            if isinstance(e, urllib.error.HTTPError):
                raise
            # The job has already been accepted by the server. If the
            # operator restarts test_serv or the TCP connection resets
            # while a bench run is still in progress, keep polling until
            # the normal wait deadline instead of converting a brief
            # transport blip into a false mission failure.
            pass
        time.sleep(0.05)
    return None


def _summarize_tar(data):
    """Print the manifest and timeline from an artefact tarball."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        members = tf.getnames()
        if "manifest.json" in members:
            manifest_m = tf.extractfile("manifest.json")
            if manifest_m is not None:
                sys.stdout.buffer.write(b"=== manifest.json ===\n")
                sys.stdout.buffer.write(manifest_m.read())
        if "timeline.log" in members:
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


def _extract(data, out_dir, force=False):
    # Refuse to merge into an existing non-empty directory: a stale
    # streams/foo.bin from a previous run interleaved with a new
    # streams/bar.bin from this run is a recipe for "but it worked
    # last time" confusion. --force runs an rmtree first.
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        if not force:
            raise RuntimeError(
                f"--extract dir {out_dir!r} is non-empty; pass "
                f"--force to clear it before extracting, or pick "
                f"a fresh dir")
        import shutil
        shutil.rmtree(out_dir)
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


def _dump_outputs(tar_bytes, digest, extract_to, force=False):
    if tar_bytes is None:
        return
    _summarize_tar(tar_bytes)
    if extract_to is not None:
        _extract(tar_bytes, extract_to, force=force)
        tar_path = os.path.join(extract_to, f"{digest}.tar")
        with open(tar_path, "wb") as f:
            f.write(tar_bytes)
        sys.stdout.buffer.write(
            f"\nextracted to {extract_to}\n".encode())
    sys.stdout.buffer.flush()


def _fetch(server, digest, extract_to, force=False):
    tar = _get_tar(server, digest)
    if tar is None:
        print(f"no outputs for digest {digest}", file=sys.stderr)
        return 1
    _dump_outputs(tar, digest, extract_to, force=force)
    _delete_outputs(server, digest)
    return 0


def _manifest_n_errors(tar_bytes):
    """Op-level error count from the artefact's manifest.json (0 == clean).

    Returns -1 if the manifest is missing or unreadable -- a job whose
    manifest we cannot read did not demonstrably succeed. The server
    returns the artefact (HTTP 200) even when the bench session recorded
    op errors, so this count is the only success signal; main() uses it
    to exit nonzero instead of always returning 0 on a completed job.
    """
    if not tar_bytes:
        return -1
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
            m = tf.extractfile("manifest.json")
            if m is None:
                return -1
            return int(json.loads(m.read().decode()).get("n_errors", -1))
    except Exception:
        return -1


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
    ap.add_argument("--extract", metavar="DIR",
                    help="extract artefact tarball into DIR (keeps the tar)")
    ap.add_argument("--force", action="store_true",
                    help="when --extract DIR is non-empty, rmtree it "
                         "first instead of refusing")
    ap.add_argument("--runtime", type=float,
                    help="per-session deadline in seconds "
                         "(X-Test-Runtime; default 600).")
    ap.add_argument("--upload-timeout", type=float,
                    help="artefact-POST timeout in seconds "
                         "(X-Test-Upload-Timeout; default 600).")
    args = ap.parse_args()

    if args.fetch and args.plan:
        ap.error("--fetch is mutually exclusive with a plan file")
    if not args.fetch and not args.plan:
        ap.error("either a plan file or --fetch DIGEST is required")

    if args.fetch:
        return _fetch(args.server, args.fetch, args.extract,
                      force=args.force)

    # Sniff the file: a packed plan-tar starts with ustar magic at
    # offset 257 (plan.looks_like_tar). Plain plan-text gets packed
    # via _pack_from_plan with --blob attachments. The previous
    # implementation switched on the `.plan` extension, which
    # silently treated text-content `examples/*.plan` files as if
    # they were tarballs and corrupted them on submit. Sniffing the
    # bytes lets text and tar coexist under the same filename.
    with open(args.plan, "rb") as f:
        first = f.read(512)
        rest = f.read() if len(first) >= 512 else b""
    head = first + rest
    if plan.looks_like_tar(head):
        data = head
    else:
        data = _pack_from_plan(args.plan, args.blob)

    meta = {}
    if args.runtime is not None:
        meta["runtime"] = str(args.runtime)
    if args.upload_timeout is not None:
        meta["upload-timeout"] = str(args.upload_timeout)

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

    tar = _wait(args.server, digest, args.wait)
    if tar is None:
        print(f"timeout waiting for {digest}", file=sys.stderr)
        return 1

    _dump_outputs(tar, digest, args.extract, force=args.force)
    _delete_outputs(args.server, digest)
    n_errors = _manifest_n_errors(tar)
    if n_errors != 0:
        print(f"bench session for {digest} reported {n_errors} op-level "
              f"error(s) (manifest n_errors={n_errors})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
