# SPDX-License-Identifier: MIT
# plan.py --- Parse text job plans and their tar containers
# Copyright (c) 2026 Jakob Kastelic

import io
import shlex
import tarfile
from dataclasses import dataclass
from typing import Optional


MAX_PLAN_BYTES = 256 * 1024
# 512 MiB per blob is enough for a signed SD-card image used as a
# DFU flashlayout target; still bounded to keep the poller-host RAM
# cost predictable. Raise if you need full-image updates, but mind
# the tar packing happens fully in-memory on both client and poller.
MAX_BLOB_BYTES = 512 * 1024 * 1024
MAX_BLOBS = 64
MAX_OPS = 4096

CONTROL_VERBS = {
    "mark", "delay", "inventory", "description", "open", "close",
}


class PlanError(Exception):
    pass


@dataclass
class Value:
    # kind in {"int", "str", "bool", "ident", "blob"}
    kind: str
    raw: object

    def as_int(self):
        if self.kind != "int":
            raise PlanError(f"expected int, got {self.kind}")
        return int(self.raw)

    def as_str(self):
        if self.kind not in ("str", "ident"):
            raise PlanError(f"expected string, got {self.kind}")
        return str(self.raw)

    def as_bool(self):
        if self.kind != "bool":
            raise PlanError(f"expected bool, got {self.kind}")
        return bool(self.raw)

    def as_blob_name(self):
        if self.kind != "blob":
            raise PlanError(f"expected @blob ref, got {self.kind}")
        return str(self.raw)


@dataclass
class Op:
    lineno: int
    device: Optional[str]   # None for control verbs
    verb: str
    args: dict              # {name: Value}


@dataclass
class Plan:
    ops: list
    blobs: dict             # name -> bytes
    text: str = ""          # the original plan.txt, preserved verbatim


def _parse_value(tok):
    if tok.startswith("@"):
        name = tok[1:]
        if not name or not all(c.isalnum() or c in "._-" for c in name):
            raise PlanError(f"bad blob ref: {tok!r}")
        return Value("blob", name)
    if tok in ("true", "false"):
        return Value("bool", tok == "true")
    # int / hex
    try:
        if tok.startswith(("0x", "0X", "-0x", "-0X")):
            return Value("int", int(tok, 16))
        return Value("int", int(tok, 10))
    except ValueError:
        pass
    # identifier
    if tok and (tok[0].isalpha() or tok[0] == "_") \
            and all(c.isalnum() or c in "._-" for c in tok):
        return Value("ident", tok)
    # fall through: treat as opaque string (shlex already stripped quotes)
    return Value("str", tok)


def _parse_args(tokens, lineno):
    args = {}
    for t in tokens:
        k, sep, v = t.partition("=")
        if not sep:
            raise PlanError(f"line {lineno}: expected key=value, got {t!r}")
        if not k or not all(c.isalnum() or c == "_" for c in k):
            raise PlanError(f"line {lineno}: bad arg name {k!r}")
        if k in args:
            raise PlanError(f"line {lineno}: duplicate arg {k!r}")
        args[k] = _parse_value(v)
    return args


def parse_text(text):
    """Parse plan text into a flat list of Op."""
    if len(text) > MAX_PLAN_BYTES:
        raise PlanError(f"plan too large: {len(text)} > {MAX_PLAN_BYTES}")
    ops = []
    total = 0

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            toks = shlex.split(line, posix=True)
        except ValueError as e:
            raise PlanError(f"line {lineno}: tokenize: {e}")
        if not toks:
            continue

        head = toks[0]
        rest = toks[1:]

        if ":" in head:
            device, verb = head.split(":", 1)
            if not device or not verb:
                raise PlanError(f"line {lineno}: bad device:op {head!r}")
            ops.append(Op(lineno=lineno, device=device, verb=verb,
                          args=_parse_args(rest, lineno)))
        elif head == "description":
            # Special-case: rest of line is free-form text. Accept the
            # k=v form (`description text="..."`) too for grammar
            # symmetry, but the canonical and recommended form is just
            # `description "<short summary of what this plan does>"`.
            if rest and "=" in rest[0]:
                args = _parse_args(rest, lineno)
            else:
                args = {"text": Value("str", " ".join(rest))}
            ops.append(Op(lineno=lineno, device=None, verb="description",
                          args=args))
        elif head in CONTROL_VERBS:
            ops.append(Op(lineno=lineno, device=None, verb=head,
                          args=_parse_args(rest, lineno)))
        else:
            raise PlanError(
                f"line {lineno}: unknown verb {head!r} "
                f"(expected device:op or control verb)"
            )

        total += 1
        if total > MAX_OPS:
            raise PlanError(f"too many ops (> {MAX_OPS})")

    return ops


def load_tar(data):
    """Load a .plan tar: one 'plan.txt' member + arbitrary blob members."""
    if len(data) > MAX_PLAN_BYTES + MAX_BLOB_BYTES * MAX_BLOBS + 65536:
        raise PlanError("tar too large")
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    except tarfile.TarError as e:
        raise PlanError(f"bad tar: {e}")

    plan_text = None
    blobs = {}
    n_blobs = 0
    for m in tf.getmembers():
        if not m.isfile():
            continue
        name = m.name
        if "/" in name or ".." in name or name.startswith("."):
            raise PlanError(f"unsafe member name: {name!r}")
        f = tf.extractfile(m)
        if f is None:
            continue
        body = f.read()
        if name == "plan.txt":
            if len(body) > MAX_PLAN_BYTES:
                raise PlanError("plan.txt too large")
            plan_text = body.decode("utf-8", errors="strict")
        else:
            if n_blobs >= MAX_BLOBS:
                raise PlanError("too many blobs")
            if len(body) > MAX_BLOB_BYTES:
                raise PlanError(f"blob {name!r} too large")
            blobs[name] = body
            n_blobs += 1

    if plan_text is None:
        raise PlanError("tar missing plan.txt")

    ops = parse_text(plan_text)
    _check_blob_refs(ops, set(blobs))
    return Plan(ops=ops, blobs=blobs, text=plan_text)


def looks_like_tar(data):
    # ustar/POSIX magic at offset 257; many writers omit it. Fall back
    # to: a real tar starts with a printable filename in offsets 0..99,
    # padded with NUL. Plan text starts with non-NUL printable too --
    # but the size field at offset 124 is 11 octal digits + space, not
    # arbitrary text. Sniffing magic first and the size field next
    # catches both PAX and old-ustar tars without false-positiving on
    # plan.txt.
    if len(data) < 512:
        return False
    if data[257:262] == b"ustar":
        return True
    try:
        int(data[124:135].rstrip(b" \x00") or b"0", 8)
        return data[100:108].rstrip(b" \x00").isdigit() or False
    except (ValueError, AttributeError):
        return False


def extract_description(data):
    """Return the first top-level ``description "<text>"`` value from a
    plan, or ``None`` if absent / unparseable. ``data`` is the raw
    submitted body -- bytes for a packed tar, bytes or str for a plain
    plan.txt; the function sniffs the format, decodes, and parses.

    Any ``PlanError`` / tar error is swallowed -- callers ask for the
    description as a *labeling* convenience and don't want a malformed
    plan to fail their request. Single source of truth for "what is
    this plan called?".
    """
    if isinstance(data, str):
        text = data
    elif looks_like_tar(data):
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tf:
                f = tf.extractfile("plan.txt")
                if f is None:
                    return None
                text = f.read().decode("utf-8", errors="replace")
        except (tarfile.TarError, OSError, KeyError):
            return None
    else:
        text = data.decode("utf-8", errors="replace")
    try:
        ops = parse_text(text)
    except PlanError:
        return None
    for op in ops:
        if op.verb == "description":
            v = op.args.get("text")
            if v is not None:
                return v.raw if hasattr(v, "raw") else v
    return None


def _check_blob_refs(ops, available):
    for op in ops:
        for v in op.args.values():
            if v.kind == "blob" and v.raw not in available:
                raise PlanError(
                    f"line {op.lineno}: @{v.raw} not in tar"
                )


def split_device_ref(device):
    """Decode ``op.device`` into ``(plugin_name, spec_id_or_None)``.

    Plans can write either the bare plugin name (``mp135:uart_open``)
    when there's a unique instance, or the fully-qualified ``plugin.id``
    form (``mp135.evb:uart_open``) when there are several. Returns
    ``(None, None)`` for control-verb ops that have ``device=None``.
    """
    if device is None:
        return None, None
    if "." not in device:
        return device, None
    plugin_name, _, spec_id = device.partition(".")
    return plugin_name, spec_id or None


def required_devices(plan):
    """Return the set of plugin names referenced by any op in the plan.
    Used by the poller for parallelization: jobs whose device sets are
    disjoint can run concurrently.

    Strips the ``.spec_id`` suffix off ``plugin.id:op`` references --
    the session's locking and refresh paths key on plugin names, while
    spec_id resolution happens later in ``registry.resolve``.

    ``lease:claim device=foo.bar duration_s=...`` also pulls ``foo``
    into the set so the session pre-locks it; the lease op needs to
    hold that device's per_dev_lock when it adds the entry to the
    registry's lease table.
    """
    out = set()
    for op in plan.ops:
        plugin_name, _ = split_device_ref(op.device)
        if plugin_name is not None:
            out.add(plugin_name)
        if op.device == "lease" and op.verb == "claim":
            d = op.args.get("device")
            if d is not None:
                raw = d.raw if hasattr(d, "raw") else str(d)
                if "." in raw:
                    out.add(raw.split(".", 1)[0])
    return out


def pack_tar(plan_text, blobs):
    """Build a .plan tar bytes: plan.txt + blobs dict {name: bytes}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        body = plan_text.encode("utf-8")
        ti = tarfile.TarInfo(name="plan.txt")
        ti.size = len(body)
        tf.addfile(ti, io.BytesIO(body))
        for name, data in blobs.items():
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()
