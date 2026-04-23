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
    ``ARG_TYPES``. ``run`` is called as ``run(session, device_handle, args)``
    where ``args`` are already-decoded python values (int, str, bool, or
    raw bytes for blobs).
    """
    args: Dict[str, str]
    doc: str
    run: Callable = None
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


def decode_args(op_schema, raw_args, blobs):
    """Materialize parser Values into plain Python per the op schema.

    ``raw_args`` is ``{name: plan.Value}``; ``blobs`` is ``{name: bytes}``.
    Missing required args, unknown args, or wrong-typed args all raise
    ValueError.
    """
    out = {}
    for k, v in raw_args.items():
        if k not in op_schema.args:
            raise ValueError(f"unknown arg {k!r}")
        want = op_schema.args[k]
        if want == "int":
            out[k] = v.as_int()
        elif want == "str":
            out[k] = v.as_str()
        elif want == "bool":
            out[k] = v.as_bool()
        elif want == "ident":
            if v.kind != "ident":
                raise ValueError(f"arg {k!r}: expected ident, got {v.kind}")
            out[k] = v.raw
        elif want == "blob":
            name = v.as_blob_name()
            if name not in blobs:
                raise ValueError(f"blob @{name} not provided")
            out[k] = blobs[name]
        elif want == "any":
            out[k] = v.raw
        else:
            raise ValueError(f"arg {k!r}: unknown schema type {want!r}")
    for name in op_schema.args:
        if name not in out:
            raise ValueError(f"missing arg {name!r}")
    return out
