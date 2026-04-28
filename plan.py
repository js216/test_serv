# SPDX-License-Identifier: MIT
# plan.py --- Parse text job plans and their tar containers
# Copyright (c) 2026 Jakob Kastelic

import io
import shlex
import tarfile
from dataclasses import dataclass, field
from typing import Optional


MAX_PLAN_BYTES = 256 * 1024
# 512 MiB per blob is enough for a signed SD-card image used as a
# DFU flashlayout target; still bounded to keep the poller-host RAM
# cost predictable. Raise if you need full-image updates, but mind
# the tar packing happens fully in-memory on both client and poller.
MAX_BLOB_BYTES = 512 * 1024 * 1024
MAX_BLOBS = 64
MAX_OPS = 4096
MAX_DEPTH = 2

CONTROL_VERBS = {
    "fork", "end", "join", "barrier", "mark", "delay", "wall_time",
    "inventory",
    "open", "close",
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
    body: list = field(default_factory=list)  # populated for fork


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
    """Parse plan text into a flat list of Op (with nested body on fork)."""
    if len(text) > MAX_PLAN_BYTES:
        raise PlanError(f"plan too large: {len(text)} > {MAX_PLAN_BYTES}")
    ops_flat = []
    stack = [ops_flat]        # current insertion list
    depth = 0
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
            op = Op(lineno=lineno, device=device, verb=verb,
                    args=_parse_args(rest, lineno))
            stack[-1].append(op)
        elif head == "fork":
            if depth >= MAX_DEPTH:
                raise PlanError(f"line {lineno}: fork nesting too deep")
            args = _parse_args(rest, lineno)
            if "name" not in args or args["name"].kind != "ident":
                raise PlanError(f"line {lineno}: fork requires name=IDENT")
            fork_op = Op(lineno=lineno, device=None, verb="fork", args=args)
            stack[-1].append(fork_op)
            stack.append(fork_op.body)
            depth += 1
        elif head == "end":
            if depth == 0:
                raise PlanError(f"line {lineno}: 'end' without matching fork")
            stack.pop()
            depth -= 1
        elif head in CONTROL_VERBS:
            op = Op(lineno=lineno, device=None, verb=head,
                    args=_parse_args(rest, lineno))
            stack[-1].append(op)
        else:
            raise PlanError(
                f"line {lineno}: unknown verb {head!r} "
                f"(expected device:op or control verb)"
            )

        total += 1
        if total > MAX_OPS:
            raise PlanError(f"too many ops (> {MAX_OPS})")

    if depth != 0:
        raise PlanError("unclosed fork block (missing 'end')")

    return ops_flat


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


def _check_blob_refs(ops, available):
    def walk(op_list):
        for op in op_list:
            for v in op.args.values():
                if v.kind == "blob" and v.raw not in available:
                    raise PlanError(
                        f"line {op.lineno}: @{v.raw} not in tar"
                    )
            walk(op.body)
    walk(ops)


def required_devices(plan):
    """Return the set of plugin names referenced by any op in the plan
    (including fork bodies). Used by the poller for parallelization:
    jobs whose device sets are disjoint can run concurrently.
    """
    out = set()
    def walk(ops):
        for op in ops:
            if op.device is not None:
                out.add(op.device)
            walk(op.body)
    walk(plan.ops)
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
