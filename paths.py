# SPDX-License-Identifier: MIT
# paths.py --- Shared state-dir resolution (cross-platform)
# Copyright (c) 2026 Jakob Kastelic

import os
import tempfile


def write_atomic(path, body):
    """Atomic file write usable from any thread/process.

    A unique tempfile is used per call so concurrent writers don't
    collide on a shared "<path>.tmp" name -- ThreadingHTTPServer in
    server.py runs status pushes in parallel. ``os.replace`` is
    atomic on POSIX and overwrites-if-exists on Windows.
    """
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(
        dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
