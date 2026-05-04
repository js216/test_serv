# SPDX-License-Identifier: MIT
# config.py --- Single-source device config for plugins
# Copyright (c) 2026 Jakob Kastelic

import json
import os

import paths


DEFAULT_PATHS = [
    os.environ.get("TEST_SERV_CONFIG"),
    os.path.join(paths.state_dir(), "config.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
]


# Cached config snapshot. Cleared at the top of each registry refresh
# so plugin probes within one tick see a single coherent view of
# config.json, without re-reading the file (which can fail mid-edit
# if the operator's editor isn't atomic). load_invalidate() drops
# the cache before the next read.
_cached = None
_cached_lock = __import__("threading").Lock()


def load_invalidate():
    """Drop any cached config snapshot. Called from the poller's
    refresh tick + SIGHUP path so subsequent ``load()`` calls re-read
    the file from disk.
    """
    global _cached
    with _cached_lock:
        _cached = None


def load():
    """Return the merged config dict.

    Searches, in order: ``$TEST_SERV_CONFIG``, ``$TEST_SERV_DIR/config.json``,
    repo-root ``config.json``. First found wins -- the bench operator can
    fully override shipped defaults by placing a file in the state dir.
    Missing file -> empty dict.

    Supports a top-level ``"include": ["path1.json", ...]`` array so a
    multi-bench setup can split per-bench specs into separate files
    rather than stamping ~80 lines of duplicated instance dicts into
    one giant config. Includes are loaded relative to the including
    file's directory; later entries shallow-merge over earlier ones,
    and the including file's keys win over included ones.
    """
    global _cached
    with _cached_lock:
        if _cached is not None:
            return _cached
    for p in DEFAULT_PATHS:
        if not p:
            continue
        try:
            data = _load_with_includes(p)
        except FileNotFoundError:
            continue
        except Exception as e:
            raise RuntimeError(f"bad config {p!r}: {e}")
        with _cached_lock:
            _cached = data
        return data
    with _cached_lock:
        _cached = {}
    return {}


def _load_with_includes(path):
    with open(path) as f:
        data = json.load(f)
    includes = data.pop("include", None) or []
    if not includes:
        return data
    base_dir = os.path.dirname(os.path.abspath(path))
    merged = {}
    # Includes first (so they form the base), then overlay the
    # current file's keys (so the operator's top-level entries win).
    for inc in includes:
        inc_path = (inc if os.path.isabs(inc)
                    else os.path.join(base_dir, inc))
        try:
            sub = _load_with_includes(inc_path)
        except FileNotFoundError as e:
            raise RuntimeError(
                f"config {path!r} includes missing file {inc!r}: {e}")
        _shallow_merge(merged, sub)
    _shallow_merge(merged, data)
    return merged


def _shallow_merge(dst, src):
    """Dict update with one level of nesting: per-plugin sections
    merge their instances + scalar settings rather than wholesale
    replacing each other.
    """
    for k, v in src.items():
        if (isinstance(v, dict) and isinstance(dst.get(k), dict)):
            for k2, v2 in v.items():
                dst[k][k2] = v2
        else:
            dst[k] = v


def section(name):
    """Return the named plugin section (or empty dict)."""
    return load().get(name) or {}


def instances(plugin_name):
    """Return ``section[plugin_name]["instances"]`` as-is.

    Plugins iterate this, run per-instance autodetect (where requested),
    and return the resolved spec dicts from probe().
    """
    return section(plugin_name).get("instances", []) or []


def as_int(val):
    """Config ints may appear as decimal or 0x-prefixed strings."""
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        return int(val, 0)
    raise TypeError(f"expected int or str, got {type(val).__name__}")
