# SPDX-License-Identifier: MIT
# ssh.py --- Restricted SSH to embedded Linux target (fixed IP, whitelist)
# Copyright (c) 2026 Jakob Kastelic

import os
import shutil
import subprocess

import config
from plugin import DevicePlugin, Op

# Each entry: op_suffix -> (command template, {placeholder: schema_type, doc})
# The op is exposed as ssh:run_<suffix>. Grammar exactness enforced by
# per-op schema -- no variadic dispatch anywhere.
CMD_WHITELIST = {
    "uname":      ("uname -a",              {}),
    "dmesg_tail": ("dmesg | tail -n {n}",   {"n": "int"}),
    "uptime":     ("uptime",                {}),
    "df":         ("df -h",                 {}),
    "date":       ("date -Iseconds",        {}),
    "ls_log":     ("ls -la /var/log",       {}),
}

SSH_TIMEOUT_S = 15


def _ssh_argv(ip, user, key, known_hosts):
    return [
        "ssh",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "IdentitiesOnly=yes",
        "-i", key,
        f"{user}@{ip}",
    ]


def _make_runner(suffix, template, placeholders):
    def _run(session, h, args):
        fills = {k: args[k] for k in placeholders}
        cmd = template.format(**fills)
        argv = _ssh_argv(h.ip, h.user, h.key, h.known_hosts) + [cmd]
        session.log_event("SSH", f"ssh:run_{suffix}", cmd)
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                timeout=SSH_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError(
                f"ssh:run_{suffix} timed out ({SSH_TIMEOUT_S}s)")
        if result.stdout:
            session.stream(f"ssh.{suffix}").append(result.stdout)
        if result.stderr:
            session.stream(f"ssh.{suffix}.stderr").append(result.stderr)
        if result.returncode != 0:
            raise RuntimeError(
                f"ssh:run_{suffix} exit={result.returncode}")
    return _run


def _build_ops():
    ops = {}
    for suffix, (template, placeholders) in CMD_WHITELIST.items():
        ops[f"run_{suffix}"] = Op(
            args=dict(placeholders),
            doc=f"SSH target and run {template!r}",
            run=_make_runner(suffix, template, placeholders),
        )
    return ops


class SshHandle:
    def __init__(self, ip, user, key, known_hosts):
        self.ip = ip
        self.user = user
        self.key = key
        self.known_hosts = known_hosts


class SshPlugin(DevicePlugin):
    name = "ssh"
    doc = ("SSH to fixed-IP embedded Linux; per-command ops exposed as "
           "ssh:run_<name>, each with a fixed schema. Key, known_hosts, "
           "IP are bench-owned. No free-form command passing.")

    ops = _build_ops()

    def probe(self):
        if not shutil.which("ssh"):
            return []
        out = []
        for inst in config.instances(self.name):
            key = os.path.expanduser(inst.get("key", ""))
            known_hosts = os.path.expanduser(inst.get("known_hosts", ""))
            if not key or not os.path.exists(key):
                continue
            if not known_hosts or not os.path.exists(known_hosts):
                continue
            out.append({
                "id": inst.get("id", "target"),
                "ip": inst["ip"],
                "user": inst.get("user", "root"),
                "key": key,
                "known_hosts": known_hosts,
                "expected_uname": inst.get("expected_uname"),
            })
        return out

    def open(self, spec):
        h = SshHandle(ip=spec["ip"], user=spec["user"],
                      key=spec["key"], known_hosts=spec["known_hosts"])
        # Identity handshake: if the config pinned a substring of `uname -a`,
        # refuse the handle when the target's uname doesn't match. Prevents
        # running a DSP-targeted plan against a randomly-rebooted MP135 or
        # vice versa.
        expected = spec.get("expected_uname")
        if expected:
            argv = _ssh_argv(h.ip, h.user, h.key, h.known_hosts) + ["uname -a"]
            try:
                res = subprocess.run(argv, capture_output=True,
                                     timeout=SSH_TIMEOUT_S, check=False)
            except subprocess.TimeoutExpired:
                raise RuntimeError(
                    f"ssh identity check timed out against {h.ip}")
            uname = (res.stdout or b"").decode(errors="replace")
            if expected not in uname:
                raise RuntimeError(
                    f"ssh identity mismatch: expected {expected!r} in "
                    f"`uname -a`, got {uname!r}")
            h._identity_verified = True
        return h

    def close(self, handle):
        pass
