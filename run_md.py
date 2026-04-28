# SPDX-License-Identifier: GPL-3.0
# run_md.py --- Run README.md/TEST.md fences with bullet checks
# Copyright (c) 2026 Jakob Kastelic

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import datetime as _dt

from plan import pack_tar
from submit import StaleOutputsError, _output_paths, _submit, _wait


USAGE = ("usage: python3 run_md.py [--full] [--fresh] [--block N] "
         "[--ledger PATH --module NAME] [--log PATH]   "
         "(reads ./TEST.md if present, else ./README.md in CWD; "
         "--full keeps running after a failed block; "
         "--fresh removes stale server artefacts before resubmitting; "
         "--block N runs only the Nth fenced block, 0-indexed; "
         "--ledger appends 'datetime module passed-checks'; "
         "--log tees stdout/stderr to a timestamped append log)")
TEST_MD = "TEST.md"
README = "README.md"
HEADING_RE = re.compile(r"^###[ \t]+Automated Test[ \t]*$", re.MULTILINE)
NEXT_HEADING_RE = re.compile(r"^#{1,6}[ \t]", re.MULTILINE)
FENCE_RE = re.compile(r"```[^\n]*\n(.*?)^```[ \t]*\n?",
                      re.MULTILINE | re.DOTALL)
BLOB_REF_RE = re.compile(r"(?<![A-Za-z0-9_.@-])@([A-Za-z0-9._-]+)")
DEFAULT_WAIT = 180.0
OUT_DIR = "test_out"
VERIFY = "verify.py"

GREEN = "\033[32m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _parse_pairs(body, where):
    """Walk `body` collecting (fence_content, [bullets]) pairs."""
    pairs = []
    pos = 0
    while True:
        f = FENCE_RE.search(body, pos)
        if not f:
            break
        bullets, end = _parse_bullets(body, f.end())
        pairs.append((f.group(1), bullets))
        pos = end
    if not pairs:
        raise ValueError(f"no fenced blocks in {where}")
    return pairs


def _parse_section(md_text):
    """README.md path: locate '### Automated Test' section, slice it,
    and parse fence/bullet pairs out of the body."""
    m = HEADING_RE.search(md_text)
    if m is None:
        raise ValueError("no '### Automated Test' heading")
    body = md_text[m.end():]
    nh = NEXT_HEADING_RE.search(body)
    if nh:
        body = body[:nh.start()]
    return _parse_pairs(body, "'### Automated Test' section")


def _parse_test_md(md_text):
    """TEST.md path: whole file is the body, no heading expected."""
    return _parse_pairs(md_text, "TEST.md")


def _parse_bullets(text, start):
    """Parse `- ` bullets at text[start:]. Continuation lines must be
    indented. List ends on blank line, EOF, or non-list/non-indent line."""
    bullets = []
    cur = None
    i = start
    n = len(text)
    seen_any = False
    while i < n:
        nl = text.find("\n", i)
        if nl == -1:
            line = text[i:]
            new_i = n
        else:
            line = text[i:nl]
            new_i = nl + 1

        if not line.strip():
            if seen_any:
                if cur is not None:
                    bullets.append(_join_bullet(cur))
                    cur = None
                return bullets, new_i
            i = new_i
            continue

        if line.startswith("- "):
            if cur is not None:
                bullets.append(_join_bullet(cur))
            cur = [line[2:].strip()]
            seen_any = True
            i = new_i
        elif cur is not None and (line.startswith(" ") or line.startswith("\t")):
            cur.append(line.strip())
            i = new_i
        else:
            break

    if cur is not None:
        bullets.append(_join_bullet(cur))
    return bullets, i


def _join_bullet(parts):
    return " ".join(p for p in parts if p)


def _resolve_blob(name, md_dir):
    for cand in (os.path.join(md_dir, name),
                 os.path.join(md_dir, "build", name)):
        if os.path.isfile(cand):
            return cand
    raise FileNotFoundError(
        f"blob @{name} not found in {md_dir} or {md_dir}/build")


def _collect_blobs(plan_text, md_dir):
    blobs = {}
    for name in sorted(set(BLOB_REF_RE.findall(plan_text))):
        with open(_resolve_blob(name, md_dir), "rb") as f:
            blobs[name] = f.read()
    return blobs


def _deliver(paths, out_dir):
    """Move tar members + sentinel into out_dir. Drop original files."""
    os.makedirs(out_dir, exist_ok=True)
    sentinel = None
    for p in paths:
        if p.endswith(".tar"):
            with open(p, "rb") as f:
                data = f.read()
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
                for m in tf.getmembers():
                    if ".." in m.name or m.name.startswith("/"):
                        raise RuntimeError(f"unsafe member {m.name!r}")
                try:
                    tf.extractall(out_dir, filter="data")
                except TypeError:
                    tf.extractall(out_dir)
            os.remove(p)
        elif p.endswith(".txt"):
            with open(p, "rb") as f:
                sentinel = f.read()
            with open(os.path.join(out_dir, "sentinel.txt"), "wb") as f:
                f.write(sentinel)
            os.remove(p)
    return sentinel


def _remove_stale_outputs(digest):
    removed = 0
    for p in _output_paths(digest):
        try:
            os.remove(p)
            removed += 1
        except FileNotFoundError:
            pass
    return removed


def _run_block(plan_text, out_dir, fresh=False):
    """Submit a plan, wait, deliver to out_dir. Returns sentinel bytes."""
    blobs = _collect_blobs(plan_text, ".")
    data = pack_tar(plan_text, blobs)
    try:
        digest = _submit(data, {})
        paths = _wait(digest, DEFAULT_WAIT)
    except StaleOutputsError as e:
        digest = str(e)
        if not fresh:
            paths = _output_paths(digest)
        else:
            removed = _remove_stale_outputs(digest)
            print(f"fresh: removed {removed} stale output files for {digest}")
            digest = _submit(data, {})
            paths = _wait(digest, DEFAULT_WAIT)
    if paths is None:
        raise RuntimeError(f"timeout waiting for {digest}")
    return _deliver(paths, out_dir)


def _append_ledger(path, module, checks_pass):
    run_id = _dt.datetime.now().isoformat(timespec="seconds")
    row = f"{run_id} {module} {checks_pass}\n"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(row)
    print(f"ledger: {row.strip()}")


def _ts():
    return _dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


class _TeeStream:
    def __init__(self, stream, log):
        self.stream = stream
        self.log = log
        self._pending = ""

    def write(self, text):
        self.stream.write(text)
        self.stream.flush()
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self.log.write(f"{_ts()} {line}\n")
            self.log.flush()
        return len(text)

    def flush(self):
        self.stream.flush()
        if self._pending:
            self.log.write(f"{_ts()} {self._pending}\n")
            self.log.flush()
            self._pending = ""

    def isatty(self):
        return self.stream.isatty()

    @property
    def encoding(self):
        return self.stream.encoding


def _install_log(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    log = open(path, "a", encoding="utf-8")
    log.write(f"{_ts()} === run_md start ===\n")
    log.flush()
    sys.stdout = _TeeStream(sys.stdout, log)
    sys.stderr = _TeeStream(sys.stderr, log)
    return log


def _run_make():
    cmd = ["make", f"-j{os.cpu_count() or 1}"]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
    return proc.wait()


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except AttributeError:
            pass

    args = list(argv[1:])
    block_sel = None
    fresh = False
    ledger_path = None
    log_path = None
    module = None
    out = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--full":
            i += 1
        elif a == "--fresh":
            fresh = True
            i += 1
        elif a == "--ledger":
            if i + 1 >= len(args):
                print(USAGE, file=sys.stderr)
                return 1
            ledger_path = args[i + 1]
            i += 2
        elif a.startswith("--ledger="):
            ledger_path = a.split("=", 1)[1]
            i += 1
        elif a == "--log":
            if i + 1 >= len(args):
                print(USAGE, file=sys.stderr)
                return 1
            log_path = args[i + 1]
            i += 2
        elif a.startswith("--log="):
            log_path = a.split("=", 1)[1]
            i += 1
        elif a == "--module":
            if i + 1 >= len(args):
                print(USAGE, file=sys.stderr)
                return 1
            module = args[i + 1]
            i += 2
        elif a.startswith("--module="):
            module = a.split("=", 1)[1]
            i += 1
        elif a == "--block":
            if i + 1 >= len(args):
                print(USAGE, file=sys.stderr)
                return 1
            try:
                block_sel = int(args[i + 1])
            except ValueError:
                print(USAGE, file=sys.stderr)
                return 1
            i += 2
        elif a.startswith("--block="):
            try:
                block_sel = int(a.split("=", 1)[1])
            except ValueError:
                print(USAGE, file=sys.stderr)
                return 1
            i += 1
        else:
            out.append(a)
            i += 1
    if out:
        print(USAGE, file=sys.stderr)
        return 1
    if (ledger_path is None) != (module is None):
        print("error: --ledger and --module must be used together",
              file=sys.stderr)
        return 1
    log_handle = _install_log(log_path) if log_path is not None else None

    if os.path.isfile(TEST_MD):
        src = TEST_MD
        parser = _parse_test_md
    elif os.path.isfile(README):
        src = README
        parser = _parse_section
    else:
        print(f"error: neither {TEST_MD} nor {README} in CWD",
              file=sys.stderr)
        return 1

    try:
        with open(src, "r", encoding="utf-8") as f:
            md_text = f.read()
    except OSError as e:
        print(f"error: cannot read {src}: {e}", file=sys.stderr)
        return 1

    try:
        pairs = parser(md_text)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if block_sel is not None:
        if block_sel < 0 or block_sel >= len(pairs):
            print(f"error: --block {block_sel} out of range "
                  f"(have {len(pairs)} blocks, 0-indexed)",
                  file=sys.stderr)
            return 1
        pairs = [pairs[block_sel]]

    if os.path.isfile("Makefile"):
        print("--- make ---")
        rc = _run_make()
        if rc != 0:
            print(f"{RED}{BOLD}FAIL{RESET}: make exited {rc}",
                  file=sys.stderr)
            if ledger_path is not None:
                _append_ledger(ledger_path, module, 0)
            return 1

    verify_path = os.path.abspath(VERIFY)

    out_root = os.path.abspath(OUT_DIR)
    if os.path.isdir(out_root):
        shutil.rmtree(out_root)
    os.makedirs(out_root, exist_ok=True)

    checks_map = {}
    for plan_text, bullets in pairs:
        h = hashlib.sha256(plan_text.encode("utf-8")).hexdigest()[:16]
        checks_map[h] = bullets
    with open(os.path.join(out_root, "checks.json"), "w") as f:
        json.dump(checks_map, f, indent=2)

    block_fails = 0
    checks_pass = 0
    total_checks = sum(len(bullets) for _, bullets in pairs)
    checks_seen = 0
    for plan_text, bullets in pairs:
        h = hashlib.sha256(plan_text.encode("utf-8")).hexdigest()[:16]
        out_dir = os.path.join(out_root, h)
        print(f"\n=== block {h} ===")
        block_ok = True

        try:
            sentinel = _run_block(plan_text, out_dir, fresh=fresh)
        except (FileNotFoundError, OSError, RuntimeError) as e:
            print(f"submission error: {e}", file=sys.stderr)
            block_ok = False
            sentinel = None

        if sentinel is not None:
            sentinel_text = sentinel.decode(
                "utf-8", errors="replace").rstrip()
            print(f"--- sentinel ---\n{sentinel_text}")

        if block_ok and bullets and not os.path.isfile(verify_path):
            print(f"{VERIFY} missing", file=sys.stderr)
            block_ok = False

        if block_ok:
            if bullets:
                print()
            for item in bullets:
                result = subprocess.run(
                    [sys.executable, verify_path, item],
                    cwd=out_dir,
                    capture_output=True, text=True,
                )
                if result.stderr:
                    sys.stderr.write(result.stderr)
                info = result.stdout.strip()
                ok = result.returncode == 0
                tag = (f"{GREEN}PASS{RESET}" if ok
                       else f"{RED}FAIL{RESET}")
                suffix = f" ({info})" if info else ""
                checks_seen += 1
                print(f"[{checks_seen}/{total_checks} {tag}] {item}{suffix}")
                if not ok:
                    block_ok = False
                else:
                    checks_pass += 1

        banner = (f"{GREEN}{BOLD}SUCCESS{RESET}" if block_ok
                  else f"{RED}{BOLD}FAIL{RESET}")
        print(f"--- block {h}: {banner} ---")
        if not block_ok:
            block_fails += 1
            if "--full" not in sys.argv and block_sel is None:
                break

    total = len(pairs)
    word = "BLOCK" if total == 1 else "BLOCKS"
    summary = (f"{GREEN}{BOLD}{total} {word} PASSED{RESET}"
               if block_fails == 0
               else f"{RED}{BOLD}{block_fails}/{total} {word} FAILED{RESET}")
    print(f"\n{summary}")
    if ledger_path is not None:
        _append_ledger(ledger_path, module, checks_pass)
    if log_handle is not None:
        print(f"log: {log_path}")
        sys.stdout.flush()
        sys.stderr.flush()
        log_handle.write(f"{_ts()} === run_md end rc={0 if block_fails == 0 else 1} ===\n")
        log_handle.flush()
    return 0 if block_fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
