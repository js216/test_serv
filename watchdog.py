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
CHECK_INTERVAL_S = 300.0
STALE_AFTER_S = 120.0
PKILL_PATTERN = "python3.*poller.py"


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
    subprocess.run(
        ["pkill", "-9", "-f", PKILL_PATTERN],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    while True:
        age = heartbeat_age_s()
        if age is None or age > STALE_AFTER_S:
            detail = "missing" if age is None else f"age={age:.0f}s"
            print(f"{datetime.now().isoformat(timespec='seconds')} "
                  f"heartbeat stale ({detail}); killing poller",
                  flush=True)
            kill_poller()
        time.sleep(CHECK_INTERVAL_S)


if __name__ == "__main__":
    main()
