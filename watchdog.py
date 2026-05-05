#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# watchdog.py --- Restart wedged poller when heartbeat stops
# Copyright (c) 2026 Jakob Kastelic

import os
import subprocess
import time
from datetime import datetime


REPO_DIR = os.path.dirname(os.path.abspath(__file__))
HEARTBEAT = os.path.join(REPO_DIR, "heartbeat.log")
WATCHDOG_LOG = os.path.join(REPO_DIR, "watchdog.log")
CHECK_INTERVAL_S = 300.0
STALE_AFTER_S = 120.0
KILL_SETTLE_S = 2.0
PKILL_PATTERN = "python3.*poller.py"
PS_CMD = ["ps", "-eo", "pid,ppid,stat,etime,cmd"]


def heartbeat_age_s(path=HEARTBEAT, now=None):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return (now or time.time()) - mtime


def heartbeat_current(path=HEARTBEAT, now=None):
    age = heartbeat_age_s(path, now=now)
    return age is not None and age <= STALE_AFTER_S


def kill_poller():
    return subprocess.run(
        ["pkill", "-9", "-f", PKILL_PATTERN],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _read_tail(path, max_bytes=4096):
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-max_bytes, os.SEEK_END)
            except OSError:
                f.seek(0)
            return f.read().decode("utf-8", "replace")
    except OSError as e:
        return f"<unavailable: {e}>"


def _poller_processes():
    try:
        out = subprocess.check_output(
            PS_CMD, text=True, stderr=subprocess.STDOUT)
    except (OSError, subprocess.CalledProcessError) as e:
        return f"<ps failed: {e}>"
    lines = [ln for ln in out.splitlines()
             if "poller.py" in ln or "timeout -s 9 3600" in ln]
    return "\n".join(lines) if lines else "<none>"


def _settled_poller_processes(timeout_s=KILL_SETTLE_S):
    deadline = time.monotonic() + timeout_s
    last = _poller_processes()
    while time.monotonic() < deadline:
        time.sleep(0.1)
        last = _poller_processes()
    return last, max(0.0, timeout_s - max(0.0, deadline - time.monotonic()))


def _append_watchdog_log(*, age, detail, pkill_result,
                         processes_before, processes_after,
                         processes_settled, settle_elapsed_s):
    stamp = datetime.now().isoformat(timespec="seconds")
    hb_stat = "<missing>"
    try:
        st = os.stat(HEARTBEAT)
        hb_stat = (f"mtime={datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')} "
                   f"size={st.st_size} mode={oct(st.st_mode & 0o777)}")
    except OSError as e:
        hb_stat = f"<stat failed: {e}>"
    entry = (
        f"=== {stamp} watchdog kill ===\n"
        f"reason: heartbeat stale ({detail})\n"
        f"age_s: {age if age is not None else 'missing'}\n"
        f"heartbeat_path: {HEARTBEAT}\n"
        f"heartbeat_stat: {hb_stat}\n"
        f"heartbeat_tail:\n{_read_tail(HEARTBEAT)}\n"
        f"processes_before_kill:\n{processes_before}\n"
        f"processes_after_kill_immediate:\n{processes_after}\n"
        f"processes_after_kill_settled "
        f"({settle_elapsed_s:.1f}s):\n{processes_settled}\n"
        f"pkill_cmd: pkill -9 -f {PKILL_PATTERN!r}\n"
        f"pkill_rc: {pkill_result.returncode}\n\n"
    )
    with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
        f.write(entry)
        f.flush()
        os.fsync(f.fileno())


def main():
    while True:
        age = heartbeat_age_s()
        if age is None or age > STALE_AFTER_S:
            detail = "missing" if age is None else f"age={age:.0f}s"
            print(f"{datetime.now().isoformat(timespec='seconds')} "
                  f"heartbeat stale ({detail}); "
                  f"running pkill -9 -f {PKILL_PATTERN!r}",
                  flush=True)
            before = _poller_processes()
            result = kill_poller()
            after = _poller_processes()
            settled, settle_elapsed = _settled_poller_processes()
            _append_watchdog_log(age=age, detail=detail,
                                 pkill_result=result,
                                 processes_before=before,
                                 processes_after=after,
                                 processes_settled=settled,
                                 settle_elapsed_s=settle_elapsed)
            print(f"{datetime.now().isoformat(timespec='seconds')} "
                  f"pkill finished rc={result.returncode}",
                  flush=True)
        time.sleep(CHECK_INTERVAL_S)


if __name__ == "__main__":
    main()
