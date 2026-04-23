# SPDX-License-Identifier: MIT
# paths.py --- Shared state-dir resolution (cross-platform)
# Copyright (c) 2026 Jakob Kastelic

import os
import tempfile


def default_state_dir():
    """Return a writable per-user scratch dir for inputs/outputs/status/etc.

    On Windows ``tempfile.gettempdir()`` honours ``%TEMP%`` /
    ``%LOCALAPPDATA%\\Temp``; on POSIX it's typically ``/tmp``. Hardcoding
    ``/tmp`` breaks Windows, so use the stdlib resolver.
    """
    user = os.getenv("USER") or os.getenv("USERNAME") or "anon"
    return os.path.join(tempfile.gettempdir(), f"test_serv-{user}")


def state_dir():
    return os.environ.get("TEST_SERV_DIR", default_state_dir())
