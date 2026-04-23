# SPDX-License-Identifier: MIT
# plugin.py --- Device plugin base classes and op schema
# Copyright (c) 2026 Jakob Kastelic

from dataclasses import dataclass
from typing import Callable, Dict


# Argument type names (same vocabulary the parser produces).
ARG_TYPES = ("int", "str", "bool", "ident", "blob", "any")


@dataclass
class Op:
    """Declarative op schema.

    ``args`` is ``{name: type_name}`` where type_name is one of
    ``ARG_TYPES``. ``optional_args`` has the same shape but each entry
    may be omitted from the plan (missing -> ``None`` passed to
    ``run``).  ``run`` is called as
    ``run(session, device_handle, args)`` where ``args`` are
    already-decoded python values.
    """
    args: Dict[str, str]
    doc: str
    run: Callable = None
    optional_args: Dict[str, str] = None
    # Some ops (open/close) are executor-synthesized and have no run func.


class DevicePlugin:
    """Base class. Subclasses set class attributes ``name``, ``doc``,
    and ``ops`` and override ``probe()``/``open()``/``close()``.

    Kept as a plain class on purpose -- making it a dataclass would
    overwrite the subclass's ``ops = {...}`` class attribute during
    ``__init__``.
    """
    name = ""
    doc = ""
    ops = {}     # {op_name: Op}

    # probe/open/close are overridden by concrete plugins.

    def probe(self):
        """Return list of instance spec dicts present right now.

        Each spec is an arbitrary dict the plugin itself understands. A
        unique ``id`` key identifies the instance within this plugin's
        namespace (e.g. ``"A"``, ``"0"``). Non-exclusive enumeration only
        -- must not open the device or hold any USB/serial handle.
        """
        return []

    def open(self, spec):
        """Acquire an exclusive handle to the given instance spec.

        Return an opaque handle object the plugin's op ``run`` funcs
        understand. May raise ``BusyError`` if the OS refuses (another
        process holds the port).
        """
        raise NotImplementedError

    def close(self, handle):
        """Release a previously-opened handle."""
        raise NotImplementedError


class BusyError(RuntimeError):
    pass


class DeviceLostError(RuntimeError):
    pass


def _decode_one(name, want, v, blobs):
    if want == "int":
        return v.as_int()
    if want == "str":
        return v.as_str()
    if want == "bool":
        return v.as_bool()
    if want == "ident":
        if v.kind != "ident":
            raise ValueError(f"arg {name!r}: expected ident, got {v.kind}")
        return v.raw
    if want == "blob":
        bn = v.as_blob_name()
        if bn not in blobs:
            raise ValueError(f"blob @{bn} not provided")
        return blobs[bn]
    if want == "any":
        return v.raw
    raise ValueError(f"arg {name!r}: unknown schema type {want!r}")


def decode_args(op_schema, raw_args, blobs):
    """Materialize parser Values into plain Python per the op schema.

    ``raw_args`` is ``{name: plan.Value}``; ``blobs`` is ``{name: bytes}``.
    Missing required args, unknown args, or wrong-typed args all raise
    ValueError. Optional args (``op_schema.optional_args``) may be
    omitted; they land in the output dict as ``None``.
    """
    out = {}
    optional = op_schema.optional_args or {}
    combined = {**op_schema.args, **optional}
    for k, v in raw_args.items():
        if k not in combined:
            raise ValueError(f"unknown arg {k!r}")
        out[k] = _decode_one(k, combined[k], v, blobs)
    for name in op_schema.args:
        if name not in out:
            raise ValueError(f"missing arg {name!r}")
    for name in optional:
        out.setdefault(name, None)
    return out
