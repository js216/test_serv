# SPDX-License-Identifier: MIT
# submit.py --- Submit a .plan job and collect artefacts
# Copyright (c) 2026 Jakob Kastelic

import argparse
import glob
import hashlib
import io
import json
import os
import sys
import tarfile
import time

from plan import pack_tar


GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

import paths

STATE_DIR = paths.state_dir()
INPUTS = os.path.join(STATE_DIR, "inputs")
OUTPUTS = os.path.join(STATE_DIR, "outputs")


class StaleOutputsError(Exception):
    pass


def _output_paths(digest):
    paths = sorted(glob.glob(os.path.join(OUTPUTS, f"{digest}.*")))
    # Put .txt last so a reader can trust "last file is sentinel".
    paths.sort(key=lambda p: p.endswith(".txt"))
    return paths


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


def _submit(data, meta):
    digest = hashlib.sha256(data).hexdigest()
    if _output_paths(digest):
        raise StaleOutputsError(digest)
    dst = os.path.join(INPUTS, f"{digest}.plan")
    if os.path.exists(dst):
        raise FileExistsError(digest)
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
    tmp = f"{dst}.inprogress"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, dst)
    return digest


def _wait(digest, timeout):
    sentinel = os.path.join(OUTPUTS, f"{digest}.txt")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(sentinel):
            return _output_paths(digest)
        time.sleep(0.05)
    return None


def _summarize_tar(tar_path):
    """Print the manifest and timeline from an artefact tarball."""
    with open(tar_path, "rb") as f:
        data = f.read()
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


def _extract(tar_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    with open(tar_path, "rb") as f:
        data = f.read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        for m in tf.getmembers():
            if ".." in m.name or m.name.startswith("/"):
                raise RuntimeError(f"unsafe member {m.name!r}")
        try:
            tf.extractall(out_dir, filter="data")
        except TypeError:
            # Python < 3.12: no filter kwarg.
            tf.extractall(out_dir)


def _dump_and_cleanup(paths, extract_to):
    tar_path = None
    txt_bytes = None
    for p in paths:
        if p.endswith(".tar"):
            tar_path = p
        elif p.endswith(".txt"):
            with open(p, "rb") as f:
                txt_bytes = f.read()
            sys.stdout.buffer.write(b"=== sentinel .txt ===\n")
            sys.stdout.buffer.write(txt_bytes)
            if not txt_bytes.endswith(b"\n"):
                sys.stdout.buffer.write(b"\n")
    if tar_path is not None:
        _summarize_tar(tar_path)
        if extract_to is not None:
            _extract(tar_path, extract_to)
            sys.stdout.buffer.write(
                f"\nextracted to {extract_to}\n".encode())
    sys.stdout.buffer.flush()
    for p in paths:
        if extract_to is not None and p.endswith(".tar"):
            os.rename(p, os.path.join(extract_to, os.path.basename(p)))
        else:
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
    return txt_bytes


def _compare(txt_bytes, expected_path):
    with open(expected_path, "rb") as f:
        expected = f.read()
    if txt_bytes == expected:
        print(f"{GREEN}SUCCESS{RESET}")
        return 0
    print(f"{RED}FAIL{RESET}")
    return 1


def _fetch(digest, expected_path, extract_to):
    paths = _output_paths(digest)
    if not paths:
        print(f"no outputs for digest {digest}", file=sys.stderr)
        return 1
    txt = _dump_and_cleanup(paths, extract_to)
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
        return _fetch(args.fetch, args.expected, args.extract)

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
        digest = _submit(data, meta)
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

    paths = _wait(digest, args.wait)
    if paths is None:
        print(f"timeout waiting for {digest}", file=sys.stderr)
        return 1

    txt = _dump_and_cleanup(paths, args.extract)
    if args.expected is not None:
        return _compare(txt, args.expected)
    return 0


if __name__ == "__main__":
    sys.exit(main())
