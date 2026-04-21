# SPDX-License-Identifier: MIT
# submit.py --- Submit a job and wait for its result artefacts
# Copyright (c) 2026 Jakob Kastelic

import argparse
import csv
import glob
import hashlib
import io
import os
import shutil
import sys
import time


GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

# Scope CSV column -> (signal name, "active" predicate on the column
# value as it appears in the CSV). Edit this table to change thresholds
# or rename signals.
SCOPE_SIGNALS = {
    "C1": ("DSP_LED",   lambda v: v < 68),
    "C2": ("DSP_FAULT", lambda v: v < 150),
    "C3": ("DSP_SIG_1", lambda v: v < 150),
    "C4": ("DSP_SIG_2", lambda v: v < 150),
}

# Sentinel stapling qspi TLV onto an LDR.  Must match
# poller.DspHandler.QSPI_ATTACH_SEP verbatim.
QSPI_ATTACH_SEP = b"\n!QSPI-ATTACH!\n"

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


def submit(src_path, meta, qspi_path=None):
    with open(src_path, "rb") as f:
        data = f.read()
    ext = os.path.splitext(src_path)[1].lstrip(".")
    if qspi_path is not None:
        if ext != "ldr":
            raise ValueError(f"--qspi requires an .ldr primary, got .{ext}")
        with open(qspi_path, "rb") as f:
            qspi_data = f.read()
        if QSPI_ATTACH_SEP in data or QSPI_ATTACH_SEP in qspi_data:
            raise ValueError("payload collides with QSPI_ATTACH_SEP sentinel")
        data = data + QSPI_ATTACH_SEP + qspi_data
    digest = hashlib.sha256(data).hexdigest()
    if not ext:
        raise ValueError(f"{src_path}: cannot infer extension (kind)")
    if output_paths(digest):
        raise StaleOutputsError(digest)
    dst = os.path.join(INPUTS, f"{digest}.{ext}")
    if os.path.exists(dst):
        raise FileExistsError(digest)
    meta_path = f"{dst}.meta"
    # Drop any stale .meta left by a previously-interrupted submit so it
    # can't be paired with the fresh binary landing below.
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


def wait_for_result(digest, timeout):
    sentinel = os.path.join(OUTPUTS, f"{digest}.txt")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(sentinel):
            return output_paths(digest)
        time.sleep(0.05)
    return None


def summarize_scope_csv(data):
    """Reduce a scope CSV to one line per known channel listing how many
    times the signal crossed into its active state and back out."""
    r = csv.reader(io.StringIO(data.decode(errors="replace")))
    try:
        header = next(r)
    except StopIteration:
        return "(empty csv)\n"

    cols = [(i, h) for i, h in enumerate(header) if h in SCOPE_SIGNALS]
    prev = {i: None for i, _ in cols}
    enter = {i: 0 for i, _ in cols}
    leave = {i: 0 for i, _ in cols}
    n_active = {i: 0 for i, _ in cols}
    n_total = {i: 0 for i, _ in cols}

    for row in r:
        for i, _ in cols:
            try:
                active = SCOPE_SIGNALS[header[i]][1](float(row[i]))
            except (ValueError, IndexError):
                continue
            if prev[i] is not None and active != prev[i]:
                (enter if active else leave)[i] += 1
            prev[i] = active
            n_total[i] += 1
            if active:
                n_active[i] += 1

    name_w = max(len(n) for n, _ in SCOPE_SIGNALS.values())
    lines = []
    for i, ch in cols:
        name = SCOPE_SIGNALS[ch][0]
        pct = (100.0 * n_active[i] / n_total[i]) if n_total[i] else 0.0
        lines.append(
            f"{ch} {name:<{name_w}}  "
            f"went_active={enter[i]} went_inactive={leave[i]}"
            f"  duty={pct:5.1f}%"
        )
    return "\n".join(lines) + "\n"


def summarize_bin(data):
    h = hashlib.sha256(data).hexdigest()
    head = data[:32].hex()
    tail = data[-32:].hex() if len(data) > 32 else ""
    return (f"{len(data)} bytes  sha256={h}\n"
            f"head[0:32]={head}\n"
            + (f"tail[-32:]={tail}\n" if tail else ""))


def dump_and_cleanup(paths, raw_scope=False, out_dir=None):
    txt_bytes = None
    for p in paths:
        with open(p, "rb") as f:
            data = f.read()
        name = os.path.basename(p)
        sys.stdout.buffer.write(f"=== {name} ===\n".encode())
        if p.endswith(".csv") and not raw_scope:
            sys.stdout.buffer.write(summarize_scope_csv(data).encode())
        elif p.endswith(".bin"):
            sys.stdout.buffer.write(summarize_bin(data).encode())
            if out_dir is not None:
                dst = os.path.join(out_dir, name)
                sys.stdout.buffer.write(f"saved to {dst}\n".encode())
        else:
            sys.stdout.buffer.write(data)
            if not data.endswith(b"\n"):
                sys.stdout.buffer.write(b"\n")
        if p.endswith(".txt"):
            txt_bytes = data
    sys.stdout.buffer.flush()
    for p in paths:
        name = os.path.basename(p)
        if out_dir is not None and not p.endswith(".txt"):
            os.rename(p, os.path.join(out_dir, name))
        else:
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


def fetch(digest, expected_path, raw_scope=False, out_dir=None):
    paths = output_paths(digest)
    if not paths:
        print(f"no outputs for digest {digest}", file=sys.stderr)
        return 1
    txt_bytes = dump_and_cleanup(paths, raw_scope=raw_scope, out_dir=out_dir)
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
    ap.add_argument("--qspi", metavar="FILE",
                    help="staple a .qspi TLV stream onto the .ldr job; the "
                         "hardware harness executes the qspi ops in the same "
                         "UART+scope session, between boot and the trailing "
                         "capture window")
    ap.add_argument("--meta", action="append", metavar="KEY=VAL",
                    help="extra sidecar key=value (repeatable)")
    ap.add_argument("--raw-scope", action="store_true",
                    help="dump the full scope CSV instead of the summary")
    ap.add_argument("--out", metavar="DIR",
                    help="move non-.txt artefacts (.bin, .csv, ...) into DIR "
                         "instead of deleting them after summarizing")
    args = ap.parse_args()

    if args.out is not None:
        os.makedirs(args.out, exist_ok=True)

    if args.fetch and args.job:
        ap.error("--fetch is mutually exclusive with a job file")
    if not args.fetch and not args.job:
        ap.error("either a job file or --fetch DIGEST is required")

    if args.fetch:
        return fetch(args.fetch, args.expected,
                     raw_scope=args.raw_scope, out_dir=args.out)

    meta = parse_meta_kv(args.meta)
    if args.runtime is not None:
        meta["runtime"] = str(args.runtime)

    try:
        digest = submit(args.job, meta, qspi_path=args.qspi)
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

    paths = wait_for_result(digest, args.wait)
    if paths is None:
        return 1

    txt_bytes = dump_and_cleanup(paths, raw_scope=args.raw_scope,
                                 out_dir=args.out)

    if args.expected is not None:
        return compare(txt_bytes, args.expected)
    return 0


if __name__ == "__main__":
    sys.exit(main())
