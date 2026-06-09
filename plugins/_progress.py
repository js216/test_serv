# SPDX-License-Identifier: MIT
# _progress.py --- console dotted-progress shim for plugin transfers
# Copyright (c) 2026 Jakob Kastelic


class _NoProgress:
    """Stand-in when poller.py isn't importable (unit tests, tooling)."""

    bytes_done = 0

    def advance(self, n):
        pass

    def advance_to(self, done_bytes):
        pass

    def done(self):
        pass


def progress_line(label, total):
    """Return a poller.ProgressLine when running inside the poller
    process, else a no-op stand-in.  Imported lazily so plugin modules
    stay importable without poller.py, mirroring the
    register_subprocess shim pattern.  The real ProgressLine self-gates
    on size: anything under poller.PROGRESS_THRESHOLD renders nothing.
    """
    try:
        from poller import ProgressLine
    except Exception:
        return _NoProgress()
    return ProgressLine(label, total)
