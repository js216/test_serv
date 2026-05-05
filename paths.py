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
    tmpdir = os.path.join(d, ".tmp")
    os.makedirs(tmpdir, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=tmpdir, prefix=os.path.basename(path) + ".", suffix=".tmp")
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
    """Return a *persistent* writable per-user scratch dir for
    inputs/outputs/status/done/cancel.

    Persistence matters: if the dir lives on tmpfs (Linux /tmp) every
    queued plan, every DONE record, every artefact, and every cancel
    marker disappears on reboot -- agents who submitted minutes earlier
    silently lose their work and the dashboard shows nothing.

    POSIX:   ``$XDG_DATA_HOME/test_serv`` if set,
             else ``~/.local/share/test_serv``.
    Windows: ``%LOCALAPPDATA%\\test_serv`` if set,
             else ``~/AppData/Local/test_serv``.
    Fallback: ``<tempdir>/test_serv-<user>``.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return os.path.join(base, "test_serv")
        home = os.path.expanduser("~")
        if home and home != "~":
            return os.path.join(home, "AppData", "Local", "test_serv")
    else:
        base = os.environ.get("XDG_DATA_HOME")
        if base:
            return os.path.join(base, "test_serv")
        home = os.path.expanduser("~")
        if home and home != "~":
            return os.path.join(home, ".local", "share", "test_serv")
    user = os.getenv("USER") or os.getenv("USERNAME") or "anon"
    return os.path.join(tempfile.gettempdir(), f"test_serv-{user}")


def state_dir():
    return os.environ.get("TEST_SERV_DIR", default_state_dir())
