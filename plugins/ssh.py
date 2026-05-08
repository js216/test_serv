# SPDX-License-Identifier: MIT
# ssh.py --- Restricted SSH to embedded Linux target (key-based, root-only)
# Copyright (c) 2026 Jakob Kastelic

import os
import re
import shutil
import subprocess
import tempfile
import time

import config
from plugin import DevicePlugin, Op


def _track_subproc(proc, session=None):
    try:
        from poller import register_subprocess
        register_subprocess(proc, session=session)
    except Exception:
        pass


def _untrack_subproc(proc):
    try:
        from poller import unregister_subprocess
        unregister_subprocess(proc)
    except Exception:
        pass


SSH_TIMEOUT_S = 60
# `open()`'s identity probe runs `ssh ... uname -a` synchronously --
# session.canceled is unreachable from there. Cap that probe at a
# short wall-clock so cancel observed at the next op's open is within
# this window. uname is sub-second on a reachable target; ssh's own
# ConnectTimeout=5 in _ssh_argv handles unreachable.
IDENTITY_TIMEOUT_S = 8
_REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/+-]+$")


def _ssh_argv(ip, user, key, known_hosts):
    return [
        "ssh",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "IdentitiesOnly=yes",
        "-o", "PubkeyAuthentication=yes",
        "-o", "PasswordAuthentication=no",
        "-i", key,
        f"{user}@{ip}",
    ]


def _ssh_timeout_s(args):
    timeout_ms = args.get("timeout_ms")
    if timeout_ms is None:
        return SSH_TIMEOUT_S
    if timeout_ms <= 0:
        raise ValueError("ssh timeout_ms must be > 0")
    return timeout_ms / 1000.0


def _run_child(session, proc, op_name, timeout_s):
    _track_subproc(proc, session=session)
    deadline = time.monotonic() + timeout_s
    stdout = stderr = b""
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if session.canceled:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
                    raise RuntimeError(
                        f"{op_name} canceled via DELETE /jobs/<digest>")
                if time.monotonic() > deadline:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise TimeoutError(
                        f"{op_name} timed out ({timeout_s:g}s)")
    finally:
        _untrack_subproc(proc)
    return stdout, stderr


def _op_exec(session, h, args):
    cmd = args["command"]
    argv = _ssh_argv(h.ip, h.user, h.key, h.known_hosts) + [cmd]
    session.log_event("SSH", "ssh:exec", cmd)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    timeout_s = _ssh_timeout_s(args)
    stdout, stderr = _run_child(session, proc, "ssh:exec", timeout_s)
    if stdout:
        session.stream("ssh.exec").append(stdout)
    if stderr:
        session.stream("ssh.exec.stderr").append(stderr)
    session.log_event("SSH", "ssh:exec",
                      f"exit={proc.returncode} "
                      f"stdout={len(stdout)}B "
                      f"stderr={len(stderr)}B")
    if proc.returncode != 0:
        raise RuntimeError(f"ssh:exec exit={proc.returncode}")


def _scp_argv(ip, user, key, known_hosts, src_path, dest_path):
    return [
        "scp",
        "-O",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "IdentitiesOnly=yes",
        "-o", "PubkeyAuthentication=yes",
        "-o", "PasswordAuthentication=no",
        "-i", key,
        src_path,
        f"{user}@{ip}:{dest_path}",
    ]


def _op_put(session, h, args):
    data = args["data"]
    dest_path = args["path"]
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("ssh:put data must be a plan blob")
    if not _REMOTE_PATH_RE.fullmatch(dest_path):
        raise ValueError(
            "ssh:put path must be absolute and contain only "
            "A-Z a-z 0-9 . _ / + -")

    timeout_s = _ssh_timeout_s(args)
    with tempfile.NamedTemporaryFile(prefix="test-serv-ssh-put-") as f:
        f.write(bytes(data))
        f.flush()
        argv = _scp_argv(h.ip, h.user, h.key, h.known_hosts,
                         f.name, dest_path)
        session.log_event("SSH", "ssh:put",
                          f"{len(data)}B -> {dest_path}")
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        stdout, stderr = _run_child(session, proc, "ssh:put", timeout_s)

    if stdout:
        session.stream("ssh.put").append(stdout)
    if stderr:
        session.stream("ssh.put.stderr").append(stderr)
    session.log_event("SSH", "ssh:put",
                      f"exit={proc.returncode} "
                      f"stdout={len(stdout)}B "
                      f"stderr={len(stderr)}B")
    if proc.returncode != 0:
        raise RuntimeError(f"ssh:put exit={proc.returncode}")


def _op_pubkey(session, h, args):
    pub_path = h.key + ".pub"
    if not os.path.exists(pub_path):
        raise FileNotFoundError(
            f"ssh: no public key at {pub_path!r}; expected to find a "
            f"sibling of the bench's configured private key. Generate "
            f"with `ssh-keygen -t ed25519 -f {h.key}` and re-probe.")
    with open(pub_path, "rb") as f:
        data = f.read()
    session.stream("ssh.pubkey").append(data)
    session.log_event("SSH", "ssh:pubkey",
                      f"emitted {len(data)}B from {pub_path}")


def _op_trust_host_key(session, h, args):
    line = args["key"].strip()
    # Validate: <algo> <base64> [comment]; reject embedded newlines so
    # the agent can't smuggle multiple entries via a single op call.
    if "\n" in line or "\r" in line:
        raise ValueError(
            "ssh:trust_host_key: key must be a single line")
    parts = line.split(None, 2)
    if len(parts) < 2 or not parts[0].startswith("ssh-") and not parts[0].startswith("ecdsa-"):
        raise ValueError(
            f"ssh:trust_host_key: invalid key line {line!r}; expected "
            f"'<algo> <base64> [comment]'")
    # Drop any existing entries for the target IP so we don't accumulate
    # stale host keys across reflashes. -R is idempotent and finishes in
    # <100 ms; we still poll and SIGTERM on cancel for completeness.
    if session.canceled:
        raise RuntimeError("ssh:trust_host_key canceled before ssh-keygen")
    proc = subprocess.Popen(
        ["ssh-keygen", "-R", h.ip, "-f", h.known_hosts],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _track_subproc(proc, session=session)
    try:
        while True:
            try:
                proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if session.canceled:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise RuntimeError(
                        "ssh:trust_host_key canceled mid-keygen")
    finally:
        _untrack_subproc(proc)
    # Append the new entry IP-prefixed; ssh-keygen leaves the file
    # without a trailing newline, so emit one before our line.
    with open(h.known_hosts, "ab") as f:
        f.write(f"{h.ip} {line}\n".encode())
    session.log_event(
        "SSH", "ssh:trust_host_key",
        f"replaced known_hosts entry for {h.ip}: {parts[0]}")


class SshHandle:
    def __init__(self, ip, user, key, known_hosts):
        self.ip = ip
        self.user = user
        self.key = key
        self.known_hosts = known_hosts


class SshPlugin(DevicePlugin):
    name = "ssh"
    doc = (
        "SSH to a fixed-IP embedded Linux target as root. Login is\n"
        "key-only: BatchMode=yes, IdentitiesOnly=yes,\n"
        "PasswordAuthentication=no, StrictHostKeyChecking=yes.\n"
        "\n"
        "Two distinct keypairs are involved -- DO NOT confuse them:\n"
        "  (A) BENCH'S AUTH KEYPAIR: the bench has the private half;\n"
        "      the agent must embed the public half in the target\n"
        "      rootfs's /root/.ssh/authorized_keys so the bench can\n"
        "      log in. Get the public half via `ssh:pubkey`.\n"
        "  (B) TARGET'S HOST KEYPAIR: the agent generates a fresh pair\n"
        "      per image build, embeds the private half in the rootfs\n"
        "      at /etc/ssh/ssh_host_ed25519_key (mode 0600), and tells\n"
        "      the bench the public half via `ssh:trust_host_key` so\n"
        "      the bench's known_hosts will trust the freshly booted\n"
        "      image.\n"
        "\n"
        "PRIVATE KEYS NEVER LEAVE THEIR ORIGINATING SIDE. Only public\n"
        "keys cross the agent<->bench boundary, both directions:\n"
        "  * bench -> agent: bench's auth public key (ssh:pubkey)\n"
        "  * agent -> bench: target's host public key\n"
        "                    (ssh:trust_host_key)\n"
        "\n"
        "End-to-end debug-image workflow:\n"
        "  1. `ssh:pubkey`                       (capture bench auth\n"
        "                                         pubkey from stream\n"
        "                                         ssh.pubkey)\n"
        "  2. agent-side: build the rootfs with\n"
        "       - that pubkey at /root/.ssh/authorized_keys, mode 0600\n"
        "       - sshd_config: PermitRootLogin prohibit-password,\n"
        "         PubkeyAuthentication yes, PasswordAuthentication no\n"
        "       - a freshly-generated target host keypair, the private\n"
        "         half at /etc/ssh/ssh_host_ed25519_key (mode 0600)\n"
        "  3. `ssh:trust_host_key key=\"<the pub half from step 2>\"`\n"
        "  4. `dfu.evb:flash_layout layout=@flash.tsv` (or whichever\n"
        "      board's `dfu.<id>` you're targeting)\n"
        "  5. `msc.evb:write data=@sdcard.img`\n"
        "  6. `delay ms=8000`  (wait for boot)\n"
        "  7. `ssh:exec command=\"...\"`  (drive the running system)\n"
        "\n"
        "Bench private key path, known_hosts path, target IP, user,\n"
        "and expected_uname live in config.json's `ssh` section.")

    ops = {
        "pubkey": Op(
            args={},
            doc=("Bench -> agent. Emits the bench's SSH PUBLIC key "
                 "(the .pub sibling of the bench's pinned identity "
                 "key) into stream ssh.pubkey. The agent must embed "
                 "this exact line in the target rootfs at "
                 "/root/.ssh/authorized_keys (mode 0600), so the "
                 "bench can authenticate as root via subsequent "
                 "ssh:exec. Public keys are public by definition; "
                 "no secret is leaked here."),
            run=_op_pubkey),
        "trust_host_key": Op(
            args={"key": "str"},
            doc=("Agent -> bench. The agent generates a target host "
                 "keypair as part of its image build and embeds the "
                 "PRIVATE half in the rootfs at "
                 "/etc/ssh/ssh_host_ed25519_key (mode 0600). It "
                 "passes the matching PUBLIC half here as the `key` "
                 "argument (single-line `<algo> <base64> [comment]`, "
                 "no IP prefix); the bench replaces any previous "
                 "host-key entry for the target IP in known_hosts so "
                 "the next ssh:exec accepts the freshly booted "
                 "image. NEVER send the private host key to the "
                 "bench -- only the public half. Run this once per "
                 "image build, before ssh:exec."),
            run=_op_trust_host_key),
        "exec": Op(
            args={"command": "str"},
            optional_args={"timeout_ms": "int"},
            doc=("Run an arbitrary shell command on the target as the "
                 "configured user (typically root). Stdout/stderr "
                 "land in streams ssh.exec / ssh.exec.stderr; the "
                 "timeline event records the exit code. Non-zero "
                 "exit raises so the plan halts on failure. "
                 f"Per-call timeout: {SSH_TIMEOUT_S}s. Requires the "
                 "target's host pubkey to already be trusted -- run "
                 "ssh:trust_host_key first per image build."),
            run=_op_exec),
        "put": Op(
            args={"data": "blob", "path": "str"},
            optional_args={"timeout_ms": "int"},
            doc=("Copy a plan blob to an absolute path on the target "
                 "using key-only scp with the same strict known_hosts "
                 "and pinned identity-key behavior as ssh:exec. "
                 "Stdout/stderr land in streams ssh.put / "
                 "ssh.put.stderr; non-zero exit raises. "
                 f"Per-call timeout: {SSH_TIMEOUT_S}s by default. "
                 "Requires ssh:trust_host_key first per image build."),
            run=_op_put),
    }

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
                "description": inst.get("description"),
            })
        return out

    def open(self, spec):
        h = SshHandle(ip=spec["ip"], user=spec["user"],
                      key=spec["key"], known_hosts=spec["known_hosts"])
        # Identity handshake: when the config pins a substring of `uname
        # -a`, refuse the handle if the target's uname doesn't match.
        # Catches the case where a freshly-flashed but wrong image is
        # running -- ssh:exec would otherwise silently drive the wrong
        # board.
        expected = spec.get("expected_uname")
        if expected:
            argv = (_ssh_argv(h.ip, h.user, h.key, h.known_hosts)
                    + ["uname -a"])
            try:
                res = subprocess.run(argv, capture_output=True,
                                     timeout=IDENTITY_TIMEOUT_S, check=False)
            except subprocess.TimeoutExpired:
                raise RuntimeError(
                    f"ssh identity check timed out against {h.ip} "
                    f"(>{IDENTITY_TIMEOUT_S}s)")
            uname = (res.stdout or b"").decode(errors="replace")
            if expected not in uname:
                raise RuntimeError(
                    f"ssh identity mismatch: expected {expected!r} in "
                    f"`uname -a`, got {uname!r}")
            h._identity_verified = True
        return h

    def close(self, handle):
        pass
