# SPDX-License-Identifier: MIT
# submit.py --- Submit a job and wait for its test result
# Copyright (c) 2026 Jakob Kastelic

import argparse
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


def submit(src_path, meta):
    with open(src_path, "rb") as f:
        data = f.read()
    digest = hashlib.sha256(data).hexdigest()
    ext = os.path.splitext(src_path)[1].lstrip(".")
    if not ext:
        raise ValueError(f"{src_path}: cannot infer extension (kind)")
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
    out = os.path.join(OUTPUTS, f"{digest}.txt")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(out):
            with open(out, "rb") as f:
                result = f.read()
            os.remove(out)
            return result
        time.sleep(0.05)
    return None


def compare(result, expected_path):
    with open(expected_path, "rb") as f:
        expected = f.read()
    if result == expected:
        print(f"{GREEN}SUCCESS{RESET}")
        return 0
    if result is not None:
        sys.stdout.buffer.write(result)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job", help="file to submit (extension is the job kind)")
    ap.add_argument("--wait", type=float)
    ap.add_argument("--expected")
    ap.add_argument("--runtime", type=float,
                    help="capture duration in seconds (sent as X-Test-Runtime)")
    ap.add_argument("--meta", action="append", metavar="KEY=VAL",
                    help="extra sidecar key=value (repeatable)")
    args = ap.parse_args()

    meta = parse_meta_kv(args.meta)
    if args.runtime is not None:
        meta["runtime"] = str(args.runtime)

    try:
        digest = submit(args.job, meta)
    except FileExistsError as e:
        print(f"duplicate job: {e}", file=sys.stderr)
        return 2

    if args.wait is None:
        print(digest)
        return 0

    result = wait_for_result(digest, args.wait)

    if args.expected is not None:
        return compare(result, args.expected)

    if result is None:
        return 1
    sys.stdout.buffer.write(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
