# SPDX-License-Identifier: MIT
# submit.py --- Submit a job and wait for its result artefacts
# Copyright (c) 2026 Jakob Kastelic

import argparse
import glob
import hashlib
import os
import shutil
import sys
import time


GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

STATE_DIR = os.environ.get(
    "TEST_SERV_DIR",
    f"/tmp/test_serv-{os.getenv('USER', 'anon')}",
)
INPUTS = os.path.join(STATE_DIR, "inputs")
OUTPUTS = os.path.join(STATE_DIR, "outputs")


class StaleOutputsError(Exception):
    pass


def output_paths(digest):
    paths = sorted(glob.glob(os.path.join(OUTPUTS, f"{digest}.*")))
    paths.sort(key=lambda p: p.endswith(".txt"))
    return paths


def submit(src_path, meta):
    with open(src_path, "rb") as f:
        data = f.read()
    digest = hashlib.sha256(data).hexdigest()
    ext = os.path.splitext(src_path)[1].lstrip(".")
    if not ext:
        raise ValueError(f"{src_path}: cannot infer extension (kind)")
    if output_paths(digest):
        raise StaleOutputsError(digest)
    dst = os.path.join(INPUTS, f"{digest}.{ext}")
    if os.path.exists(dst):
        raise FileExistsError(digest)
    meta_path = f"{dst}.meta"
    if meta:
        with open(meta_path, "w") as f:
            f.write("".join(f"{k}={v}\n" for k, v in meta.items()))
            f.flush()
            os.fsync(f.fileno())
    tmp = f"{dst}.inprogress"
    shutil.copyfile(src_path, tmp)
    os.rename(tmp, dst)
    return digest


def wait_for_result(digest, timeout):
    sentinel = os.path.join(OUTPUTS, f"{digest}.txt")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(sentinel):
            return output_paths(digest)
        time.sleep(0.05)
    return None


def dump_and_cleanup(paths):
    txt_bytes = None
    for p in paths:
        with open(p, "rb") as f:
            data = f.read()
        name = os.path.basename(p)
        sys.stdout.buffer.write(f"=== {name} ===\n".encode())
        sys.stdout.buffer.write(data)
        if not data.endswith(b"\n"):
            sys.stdout.buffer.write(b"\n")
        if p.endswith(".txt"):
            txt_bytes = data
    sys.stdout.buffer.flush()
    for p in paths:
        os.remove(p)
    return txt_bytes


def compare(txt_bytes, expected_path):
    with open(expected_path, "rb") as f:
        expected = f.read()
    if txt_bytes == expected:
        print(f"{GREEN}SUCCESS{RESET}")
        return 0
    print(f"{RED}FAIL{RESET}")
    return 1


def parse_meta_kv(pairs):
    meta = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        if not k or not v:
            raise ValueError(f"--meta expects key=value, got: {p}")
        meta[k] = v
    return meta


def fetch(digest, expected_path):
    paths = output_paths(digest)
    if not paths:
        print(f"no outputs for digest {digest}", file=sys.stderr)
        return 1
    txt_bytes = dump_and_cleanup(paths)
    if expected_path is not None:
        return compare(txt_bytes, expected_path)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job", nargs="?",
                    help="file to submit (extension is the job kind)")
    ap.add_argument("--fetch", metavar="DIGEST",
                    help="dump and clean up outputs for a previously-submitted "
                         "digest; use this after a fire-and-forget submit")
    ap.add_argument("--wait", type=float)
    ap.add_argument("--expected")
    ap.add_argument("--runtime", type=float,
                    help="capture duration in seconds (sent as X-Test-Runtime)")
    ap.add_argument("--meta", action="append", metavar="KEY=VAL",
                    help="extra sidecar key=value (repeatable)")
    args = ap.parse_args()

    if args.fetch and args.job:
        ap.error("--fetch is mutually exclusive with a job file")
    if not args.fetch and not args.job:
        ap.error("either a job file or --fetch DIGEST is required")

    if args.fetch:
        return fetch(args.fetch, args.expected)

    meta = parse_meta_kv(args.meta)
    if args.runtime is not None:
        meta["runtime"] = str(args.runtime)

    try:
        digest = submit(args.job, meta)
    except StaleOutputsError as e:
        print(f"stale outputs for {e}; run submit.py --fetch {e} first",
              file=sys.stderr)
        return 2
    except FileExistsError as e:
        print(f"duplicate job: {e}", file=sys.stderr)
        return 2

    if args.wait is None:
        print(digest)
        return 0

    paths = wait_for_result(digest, args.wait)
    if paths is None:
        return 1

    txt_bytes = dump_and_cleanup(paths)

    if args.expected is not None:
        return compare(txt_bytes, args.expected)
    return 0


if __name__ == "__main__":
    sys.exit(main())
