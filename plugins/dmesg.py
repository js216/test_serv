# SPDX-License-Identifier: MIT
# dmesg.py --- Kernel log tail pseudo-device
# Copyright (c) 2026 Jakob Kastelic

import subprocess

from plugin import DevicePlugin, Op


MAX_LINES = 1000
DEFAULT_LINES = 50
DEFAULT_TIMEOUT_MS = 2000


class DmesgHandle:
    pass


def _op_tail(session, h, args):
    lines = args.get("lines") or DEFAULT_LINES
    timeout_ms = args.get("timeout_ms") or DEFAULT_TIMEOUT_MS
    if lines <= 0 or lines > MAX_LINES:
        raise ValueError(f"dmesg:tail lines out of range: {lines}")
    if timeout_ms <= 0:
        raise ValueError("dmesg:tail timeout_ms must be > 0")
    session.bail_if_canceled("dmesg:tail")
    try:
        res = subprocess.run(
            ["dmesg", "-T"], capture_output=True, check=False,
            timeout=timeout_ms / 1000.0)
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(
            f"dmesg:tail timed out after {timeout_ms} ms") from e
    if res.returncode != 0:
        err = (res.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(
            "dmesg:tail failed"
            + (f": {err}" if err else f" exit={res.returncode}"))
    body = (res.stdout or b"").decode("utf-8", "replace").splitlines()
    tail = "\n".join(body[-lines:])
    if tail:
        tail += "\n"
    session.stream("dmesg.tail").append(tail.encode())
    session.log_event("DMESG", "dmesg:tail", f"{min(lines, len(body))} lines")


class DmesgPlugin(DevicePlugin):
    name = "dmesg"
    doc = (
        "Read-only kernel log tail pseudo-device. dmesg.any:tail captures "
        "the last N lines into stream dmesg.tail using plain `dmesg -T`; "
        "the bench host must allow unprivileged dmesg reads "
        "(kernel.dmesg_restrict=0).")

    ops = {
        "tail": Op(
            args={},
            optional_args={"lines": "int", "timeout_ms": "int"},
            doc=("Capture the last lines=N kernel log lines into "
                 "dmesg.tail. Defaults: lines=50, timeout_ms=2000. "
                 f"lines must be 1..{MAX_LINES}. Requires host "
                 "kernel.dmesg_restrict=0 or equivalent permissions."),
            run=_op_tail),
    }

    def probe(self):
        return [{
            "id": "any",
            "description": (
                "kernel log tail pseudo-device; requires "
                "kernel.dmesg_restrict=0"),
        }]

    def open(self, spec):
        return DmesgHandle()

    def close(self, handle):
        pass
