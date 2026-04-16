# SPDX-License-Identifier: MIT
# submit.py --- Submit an .ldr file and wait for its test result
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

HERE = os.path.dirname(os.path.abspath(__file__))
INPUTS = os.path.join(HERE, "inputs")
OUTPUTS = os.path.join(HERE, "outputs")


def submit(ldr_path):
    with open(ldr_path, "rb") as f:
        data = f.read()
    digest = hashlib.sha256(data).hexdigest()
    dst = os.path.join(INPUTS, f"{digest}.ldr")
    if os.path.exists(dst):
        raise FileExistsError(digest)
    shutil.copyfile(ldr_path, dst)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ldr")
    ap.add_argument("--wait", type=float)
    ap.add_argument("--expected")
    args = ap.parse_args()

    try:
        digest = submit(args.ldr)
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
