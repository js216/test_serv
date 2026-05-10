#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# report.py --- Summarize per-device duty cycle from devices.log
# Copyright (c) 2026 Jakob Kastelic

import argparse
import json
import os
import sys
import time
from datetime import datetime


REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.join(REPO_DIR, "devices.log")


def load_events(path):
    events = []
    try:
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    print(f"{path}:{lineno}: skipping invalid JSON: {e}",
                          file=sys.stderr)
                    continue
                if rec.get("event") not in ("use", "release"):
                    continue
                if not rec.get("device") or not isinstance(rec.get("t"),
                                                            (int, float)):
                    continue
                if not rec.get("plan_digest"):
                    continue
                events.append(rec)
    except FileNotFoundError:
        return []
    events.sort(key=lambda r: float(r["t"]))
    return events


def compute_duty(events, now=None):
    if not events:
        return None
    if now is None:
        now = time.time()
    events = sorted(events, key=lambda r: float(r["t"]))

    first_t = min(float(e["t"]) for e in events)
    last_t = max(float(e["t"]) for e in events)
    active = {}
    stats = {}

    def device_stats(device):
        return stats.setdefault(device, {
            "busy_s": 0.0,
            "intervals": 0,
            "active": [],
            "stale_sessions": [],
        })

    for rec in events:
        t = float(rec["t"])
        device = rec["device"]
        session = rec.get("session") or ""
        st = device_stats(device)
        if rec["event"] == "use":
            active_for_device = active.setdefault(device, {})
            if session in active_for_device:
                continue
            for stale_session, start in list(active_for_device.items()):
                st["busy_s"] += max(0.0, t - start)
                st["intervals"] += 1
                st["stale_sessions"].append(stale_session or "(unknown)")
                del active_for_device[stale_session]
            active_for_device[session] = t
        elif device in active and session in active[device]:
            start = active[device].pop(session)
            st["busy_s"] += max(0.0, t - start)
            st["intervals"] += 1
            if not active[device]:
                del active[device]

    if active:
        window_end = max(now, last_t)
        for device, sessions in active.items():
            st = device_stats(device)
            for session, start in sessions.items():
                st["busy_s"] += max(0.0, window_end - start)
                st["active"].append({"session": session, "since": start})
    else:
        window_end = last_t

    window_s = max(0.0, window_end - first_t)
    return {
        "start": first_t,
        "end": window_end,
        "window_s": window_s,
        "stats": stats,
    }


def _fmt_ts(t):
    return datetime.fromtimestamp(t).isoformat(timespec="seconds")


def print_report(summary):
    if summary is None:
        print("no device usage records")
        return
    window_s = summary["window_s"]
    print(f"window: {_fmt_ts(summary['start'])} .. "
          f"{_fmt_ts(summary['end'])} ({window_s:.1f}s)")
    print("device                 busy_s  duty_%  intervals  active")
    rows = []
    for device, st in summary["stats"].items():
        duty = (100.0 * st["busy_s"] / window_s) if window_s else 0.0
        rows.append((duty, device, st))
    for duty, device, st in sorted(rows, key=lambda r: (-r[0], r[1])):
        active = ""
        if st["active"]:
            oldest = min(r["since"] for r in st["active"])
            active = f"since {_fmt_ts(oldest)}"
        elif st["stale_sessions"]:
            active = f"stale {len(st['stale_sessions'])}"
        print(f"{device:<20} {st['busy_s']:7.1f} {duty:7.2f} "
              f"{st['intervals']:9d}  {active}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Report per-device duty cycle from devices.log")
    ap.add_argument("--log", default=DEFAULT_LOG,
                    help=f"device log path (default: {DEFAULT_LOG})")
    args = ap.parse_args(argv)
    print_report(compute_duty(load_events(args.log)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
