# SPDX-License-Identifier: MIT
# ssh.py --- Restricted SSH to embedded Linux target (key-based, root-only)
# Copyright (c) 2026 Jakob Kastelic

import os
import shutil
import subprocess

import config
from plugin import DevicePlugin, Op


SSH_TIMEOUT_S = 60


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


def _op_exec(session, h, args):
    cmd = args["command"]
    argv = _ssh_argv(h.ip, h.user, h.key, h.known_hosts) + [cmd]
    session.log_event("SSH", "ssh:exec", cmd)
    try:
        result = subprocess.run(
            argv, capture_output=True,
            timeout=SSH_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"ssh:exec timed out ({SSH_TIMEOUT_S}s)")
    if result.stdout:
        session.stream("ssh.exec").append(result.stdout)
    if result.stderr:
        session.stream("ssh.exec.stderr").append(result.stderr)
    session.log_event("SSH", "ssh:exec",
                      f"exit={result.returncode} "
                      f"stdout={len(result.stdout)}B "
                      f"stderr={len(result.stderr)}B")
    if result.returncode != 0:
        raise RuntimeError(f"ssh:exec exit={result.returncode}")


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
    # stale host keys across reflashes. -R is idempotent.
    subprocess.run(
        ["ssh-keygen", "-R", h.ip, "-f", h.known_hosts],
        capture_output=True, check=False)
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
            doc=("Run an arbitrary shell command on the target as the "
                 "configured user (typically root). Stdout/stderr "
                 "land in streams ssh.exec / ssh.exec.stderr; the "
                 "timeline event records the exit code. Non-zero "
                 "exit raises so the plan halts on failure. "
                 f"Per-call timeout: {SSH_TIMEOUT_S}s. Requires the "
                 "target's host pubkey to already be trusted -- run "
                 "ssh:trust_host_key first per image build."),
            run=_op_exec),
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
