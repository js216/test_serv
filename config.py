# SPDX-License-Identifier: MIT
# config.py --- Single-source device config for plugins
# Copyright (c) 2026 Jakob Kastelic

import json
import os


DEFAULT_PATHS = [
    os.environ.get("TEST_SERV_CONFIG"),
    os.path.join(
        os.environ.get("TEST_SERV_DIR",
                       f"/tmp/test_serv-{os.getenv('USER', 'anon')}"),
        "config.json",
    ),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
]


def load():
    """Return the merged config dict.

    Searches, in order: ``$TEST_SERV_CONFIG``, ``$TEST_SERV_DIR/config.json``,
    repo-root ``config.json``. First found wins -- the bench operator can
    fully override shipped defaults by placing a file in the state dir.
    Missing file -> empty dict.
    """
    for p in DEFAULT_PATHS:
        if not p:
            continue
        try:
            with open(p) as f:
                return json.load(f)
        except FileNotFoundError:
            continue
        except Exception as e:
            raise RuntimeError(f"bad config {p!r}: {e}")
    return {}


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
