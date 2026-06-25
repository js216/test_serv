# SPDX-License-Identifier: MIT
# poller.py --- Bench-host loop: pick up .plan jobs, run sessions, post tars
# Copyright (c) 2026 Jakob Kastelic

# Disable glibc's per-thread malloc cache before we import anything
# heavy, BUT only when run as the main script. When imported as a
# module (test_core, agent code), don't re-exec the host process.
# Rationale: when a third-party C extension (libusb / pyusb /
# pyft4222 / ftd2xx) hits internal corruption -- the failure mode
# where a USB device flaps mid-syscall -- glibc's
# tcache_thread_shutdown walks the per-thread cache at thread exit,
# finds an unaligned chunk, and SIGABRTs the entire process. With
# tcache_count=0 the cache is never populated, so the shutdown walk
# is a no-op and the corruption can't fire the abort. Re-exec
# ourselves once so glibc actually honours the env var (tunables
# are read at process startup, not at runtime mutation).
import os as _os, sys as _sys
if (__name__ == "__main__"
        and _os.name == "posix"
        and "GLIBC_TUNABLES" not in _os.environ
        and not _os.environ.get("_TEST_SERV_TCACHE_OK")):
    _os.environ["GLIBC_TUNABLES"] = "glibc.malloc.tcache_count=0"
    _os.environ["_TEST_SERV_TCACHE_OK"] = "1"
    _os.execv(_sys.executable, [_sys.executable] + _sys.argv)

import hashlib
import http.client
import io
import json
import os
import re
import signal
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import faulthandler
from datetime import datetime

import paths
import plan
import plugins
from plugin import Op
from registry import DeviceRegistry
from session import Session, bench_id, pack_artefact


STATE_DIR = paths.state_dir()
STATUS = os.path.join(STATE_DIR, "status")
RELEASE = os.path.join(STATE_DIR, "release")
SWEEP = os.path.join(STATE_DIR, "sweep")
LOG = os.path.join(STATE_DIR, "log.txt")
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
HEARTBEAT_LOG = os.path.join(REPO_DIR, "heartbeat.log")
STACK_DUMP_LOG = os.path.join(REPO_DIR, "stackdump.log")
HEARTBEAT_INTERVAL_S = 30.0
# Artefacts produced by run_all are spooled here before the POST to
# the server. If the POST fails (server restart, SSH-tunnel hiccup),
# the file stays on bench-side disk and the next main-loop tick
# retries -- so a transient outage at the end of a 20-minute flash
# test doesn't lose the artefact.
PENDING = os.path.join(STATE_DIR, "pending_uploads")

# digest -> Session for jobs the poller has picked up and finished
# constructing. _drain_cancels signals against this map; the server
# keeps cancel markers across drain ticks (and the artefact-upload
# path unlinks them), so a cancel that arrives mid-dispatch -- before
# the Session is registered here -- is found again on the next tick
# once the Session has landed in the map.
_active_sessions = {}
_active_lock = threading.Lock()


class _Tee:
    # Errors we must swallow so a closed-stream / pipe-closed during
    # process teardown doesn't blow up the worker thread. Anything else
    # (including OSError from a full disk) surfaces -- otherwise a
    # disk-full bench would silently stop logging.
    _SAFE_TEE_ERRORS = (BrokenPipeError, ValueError)

    def __init__(self, *streams):
        # streams[0] is the original stdout/stderr; streams[1+] are
        # log files we may rotate. Stored mutably so rotate_log()
        # can swap the file under us atomically from another thread.
        self._streams = list(streams)
        self._lock = threading.Lock()
    def write(self, s):
        with self._lock:
            streams = list(self._streams)
        for st in streams:
            try:
                st.write(s)
                st.flush()
            except self._SAFE_TEE_ERRORS:
                pass
        return len(s)
    def flush(self):
        with self._lock:
            streams = list(self._streams)
        for st in streams:
            try:
                st.flush()
            except self._SAFE_TEE_ERRORS:
                pass
    def replace_log(self, old_log, new_log):
        with self._lock:
            self._streams = [new_log if s is old_log else s
                             for s in self._streams]


# log.txt rotation: if the file already has >LOG_ROTATE_BYTES at
# startup, rename it to log.txt.1 (replacing any prior .1) and start
# fresh. Bounds bench-host disk usage from a runaway plugin trace
# without forcing the operator to wire up logrotate.
LOG_ROTATE_BYTES = 64 * 1024 * 1024


def _rotate_log_if_large(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size < LOG_ROTATE_BYTES:
        return
    backup = path + ".1"
    try:
        if os.path.exists(backup):
            os.unlink(backup)
        os.rename(path, backup)
    except OSError:
        pass


# Mutable holder so the main loop's rotation helper can swap the
# log file under sys.stdout/_Tee atomically without restarting the
# poller. Set in main() right after the initial open.
_log_f = [None]
_stack_dump_f = [None]


def _register_stack_dump():
    if _stack_dump_f[0] is None:
        _stack_dump_f[0] = open(
            STACK_DUMP_LOG, "a", buffering=1,
            encoding="utf-8", errors="replace")
    dump_f = _stack_dump_f[0]
    try:
        faulthandler.enable(file=dump_f, all_threads=True)
        sigusr1 = getattr(signal, "SIGUSR1", None)
        if sigusr1 is not None:
            try:
                faulthandler.unregister(sigusr1)
            except Exception:
                pass
            faulthandler.register(
                sigusr1, file=dump_f, all_threads=True,
                chain=False)
    except Exception:
        pass

# Refused-spool retention window. Anything in PENDING/refused/ older
# than this is unlinked: the operator either resubmitted the digest
# and copied the spool back manually, or has decided the artefact
# isn't worth recovering. Without this GC the dir grows unboundedly
# in long-uptime deployments.
# 365 days picked over a tighter window because (a) refused spools
# are tiny relative to bench disk, (b) wall-clock-based GC on a
# bench whose RTC battery died and then NTP step-forwards by months
# would otherwise nuke recently-parked spools the operator was
# counting on (the very high-stakes "I came back to the bench in 6
# months and the artefact is gone" trap). Trade more disk for more
# data safety; that's the right tradeoff for an R&D bench.
REFUSED_RETENTION_S = 365 * 24 * 3600  # 365 days
# A 409 during artefact upload means "server has no matching job
# record". That can be permanent (operator wiped history), but it can
# also be transient after a server restart / stale-job sweep while the
# poller still has the only copy of the completed artefact. Keep
# retrying for a while so resubmitting the same digest or restoring the
# server state can recover without remote-shell access to the bench.
REFUSED_GRACE_S = 6 * 3600  # 6 hours
# 409 means "server has no matching job record" and is usually not
# resolved by hammering. Retry occasionally during the grace window
# so an operator can resubmit the digest, but don't spam the tunnel/log
# on every busy poll-loop turn.
REFUSED_RETRY_BACKOFF_S = 5 * 60  # 5 minutes
_refused_retry_after = {}  # spool_path -> monotonic retry time


def _gc_refused_spools(now=None):
    """Walk PENDING/refused/ and unlink files older than retention.
    Cheap (one listdir + per-file stat); called once per refresh tick.
    """
    refused = os.path.join(PENDING, "refused")
    try:
        names = os.listdir(refused)
    except FileNotFoundError:
        return
    cutoff = (now or time.time()) - REFUSED_RETENTION_S
    removed = 0
    for n in names:
        p = os.path.join(refused, n)
        try:
            if os.stat(p).st_mtime < cutoff:
                os.unlink(p)
                removed += 1
        except OSError:
            pass
    if removed:
        print(datetime.now(),
              f"GC: removed {removed} expired spool(s) from "
              f"PENDING/refused/ (older than "
              f"{REFUSED_RETENTION_S // 86400} days)")


def _rotate_log_in_loop(path):
    """Mid-loop log rotation. Called from the main loop tick. The
    startup-only _rotate_log_if_large bounds growth to one window;
    without this, a poller that runs for months/years grows log.txt
    unboundedly (the user's stated goal is "run unsupervised for
    years"). Cheap: a single getsize() per tick.
    """
    if _log_f[0] is None:
        return
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size < LOG_ROTATE_BYTES:
        return
    backup = path + ".1"
    try:
        if os.path.exists(backup):
            os.unlink(backup)
        os.rename(path, backup)
    except OSError:
        return
    try:
        new_f = open(
            path, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        return
    old_f = _log_f[0]
    # Atomically swap stdout/stderr Tee's reference to the log file
    # so any concurrent print() lands cleanly in the new file.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "replace_log"):
            stream.replace_log(old_f, new_f)
    _log_f[0] = new_f
    try:
        old_f.close()
    except Exception:
        pass
    print(datetime.now(),
          f"log rotated -> {backup} (new file: {path})")

HTTP_PORT = int(os.environ.get("TEST_SERV_PORT", "8080"))


def _compute_code_digest():
    """sha256 of every .py file under the repo root + plugins/. Stamps
    each artefact's manifest so a future reader can tell whether two
    runs ran identical code (poller upgrades, plugin edits) without
    relying on $git_rev tagging.
    """
    h = hashlib.sha256()
    repo_root = os.path.dirname(os.path.abspath(__file__))
    paths = []
    for root, _dirs, files in os.walk(repo_root):
        # Skip caches and venvs.
        rel = os.path.relpath(root, repo_root)
        if rel.startswith("__pycache__") or "/__pycache__" in rel:
            continue
        if rel.startswith(".") or "/.git" in rel:
            continue
        for f in files:
            if f.endswith(".py"):
                paths.append(os.path.join(root, f))
    for p in sorted(paths):
        try:
            with open(p, "rb") as fh:
                h.update(os.path.relpath(p, repo_root).encode())
                h.update(b"\0")
                h.update(fh.read())
                h.update(b"\0")
        except OSError:
            continue
    return h.hexdigest()


_CODE_DIGEST = _compute_code_digest()

# Wall-clock origin used by /bench's `uptime_s`. Doesn't reset
# across run_poller.sh respawn (each respawn is a fresh process).
_BENCH_START_MONO = time.monotonic()


def _count_open_fds():
    """Cheap process-fd-count via /proc/self/fd. Returns -1 if not
    available (non-Linux, sandboxed). Surfaces in bench.json so the
    operator can spot a leaked-fd trend over weeks of uptime.
    """
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


def _count_dir_files(path, suffix=""):
    """Count files in `path` whose name ends with `suffix`. Returns
    -1 if the dir doesn't exist (so the dashboard can render "?").
    Used for pending_uploads / refused_spools surfacing.
    """
    try:
        return sum(1 for n in os.listdir(path) if n.endswith(suffix))
    except OSError:
        return -1


def _self_rss_bytes():
    """RSS in bytes from /proc/self/status (VmRSS line). Returns -1
    on failure. Coupled to Linux; the rest of the bench is too, so
    not worth a portable fallback today.
    """
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return int(parts[1]) * 1024  # kB -> B
    except OSError:
        pass
    return -1


_poller_lock_fd = None


def _acquire_poller_lock():
    """fcntl.flock on STATE_DIR/poller.lock; refuse to start if a
    second poller already holds it. Without this, two pollers
    sharing one STATE_DIR (operator mistake or systemd unit dup)
    silently corrupt PENDING/ uploads + race on /pickup.
    """
    if os.name == "nt":
        return  # fcntl unavailable on Windows; skip silently
    global _poller_lock_fd
    import fcntl
    path = os.path.join(STATE_DIR, "poller.lock")
    # Open WITHOUT O_TRUNC: if another poller holds the flock, we
    # must NOT erase its pid line (the operator running `cat
    # poller.lock` to see which pid still owns it would otherwise
    # find an empty file). The lock-file contents are only
    # rewritten if we win the flock.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise SystemExit(
            f"another poller already holds {path!r}; refusing to "
            f"start. If you're sure no other poller is running, "
            f"rm the lock file.")
    # Hold the fd for the lifetime of the process. We won the lock,
    # so it's now safe to overwrite the pid line.
    _poller_lock_fd = fd
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"pid={os.getpid()}\n".encode())
    except OSError:
        pass


# Subprocess registry: every plugin's Popen child is tracked here so
# (a) the SIGINT shutdown path can SIGKILL anything still alive after
# the graceful grace window, and (b) signal_cancel can SIGKILL a
# session's own subprocesses immediately rather than depending on
# each op's poll loop to notice session.canceled. Without (b), a
# DELETE /jobs/<digest> against a session that's mid-cubeprog-flash
# took up to the cubeprog timeout to actually stop -- "delivered
# but not honored" from the operator's perspective.
_active_subprocs = {}  # proc -> session_or_None
_subprocs_lock = threading.Lock()


def register_subprocess(proc, session=None):
    """Plugins call this immediately after Popen so the shutdown +
    cancel paths can find the child. Pass `session` whenever it's
    available so signal_cancel knows to SIGKILL this proc."""
    with _subprocs_lock:
        _active_subprocs[proc] = session


def unregister_subprocess(proc):
    """Plugins call this in their finally / after wait()."""
    with _subprocs_lock:
        _active_subprocs.pop(proc, None)


def kill_session_subprocs(session):
    """Called from session.signal_cancel: SIGKILL every Popen
    registered to this session so a cancel issued mid-cubeprog or
    mid-ssh:exec aborts the child immediately, rather than waiting
    on each op's 200ms-poll loop to notice session.canceled.

    Idempotent (proc.kill on a dead pid is safe). Doesn't unregister
    -- the op's own finally / unregister_subprocess does that.
    """
    with _subprocs_lock:
        owned = [p for p, s in _active_subprocs.items() if s is session]
    for p in owned:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass


POLL_INTERVAL_S = 2.5
DEVICE_REFRESH_S = 15.0
DEFAULT_UPLOAD_S = 600.0
MAX_UPLOAD_S = 3600.0
# Show a dotted progress line on stdout for transfers >= this size.
# One dot per MiB is byte-based, but at the tunnel speeds this bench
# typically sees it reads as roughly one dot per second. Tee'd into
# log.txt so even headless runs keep a record of how long big
# SD-card-image moves actually took.
PROGRESS_THRESHOLD = 1 << 20         # 1 MiB
PROGRESS_BYTES_PER_DOT = 1 << 20     # 1 MiB
_HTTP_CHUNK = 64 * 1024              # send/recv granularity
# Floor bandwidth used to size the GET /plan transfer budget. A flat
# 30s timeout under-budgets a multi-MB plan (firmware/PRBS blobs) over
# a slow tunnel: it would abort mid-download and strand the job in the
# server's DONE/ as a "running" record. Budgeting cl/_PLAN_MIN_BPS
# gives a big-but-legitimate plan room to finish; a genuinely dead
# connection still aborts within the per-chunk timeout cap.
_PLAN_MIN_BPS = 256 * 1024           # 256 KiB/s


def _timeout_left(deadline, cap=5.0):
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("HTTP transfer deadline exceeded")
    return min(cap, left)


def _set_urlopen_timeout(response, deadline):
    timeout = _timeout_left(deadline)
    try:
        response.fp.raw._sock.settimeout(timeout)
    except Exception:
        pass


def _fmt_rate(nbytes, seconds):
    if seconds <= 0:
        return "?"
    mib_s = nbytes / seconds / (1024 * 1024)
    return f"{mib_s:.2f} MiB/s"


class ProgressLine:
    """Single-line dotted progress for big transfers::

        2026-06-09 16:38:31 GET http://... 44530 KiB ... done in 9.1s @ 4.77 MiB/s

    Inactive (every method a no-op) when ``total`` is None or below
    PROGRESS_THRESHOLD, so callers don't need their own size gate.
    ``advance(n)`` prints one dot per PROGRESS_BYTES_PER_DOT bytes and
    flushes after each so SSH-tunnelled tail -f stays live; ``done()``
    terminates the line with elapsed time and rate.  Also used (via
    plugins/_progress.py) by the msc and dfu plugins so their
    subprocess-driven transfers render the same way as HTTP ones.
    """

    def __init__(self, label, total):
        self.active = total is not None and total >= PROGRESS_THRESHOLD
        self.bytes_done = 0
        self._tally = 0
        self._started = time.monotonic()
        self._ended = False
        if self.active:
            sys.stdout.write(
                f"\n{datetime.now()} {label} {total >> 10} KiB ")
            sys.stdout.flush()

    def advance(self, n):
        if not self.active or n <= 0:
            return
        self.bytes_done += n
        self._tally += n
        if self._tally >= PROGRESS_BYTES_PER_DOT:
            sys.stdout.write("." * (self._tally // PROGRESS_BYTES_PER_DOT))
            sys.stdout.flush()
            self._tally %= PROGRESS_BYTES_PER_DOT

    def advance_to(self, done_bytes):
        """Move to an absolute cumulative byte count; never backward.
        Suits sources that report totals (dd stats lines, percentages)
        rather than per-chunk deltas, and makes repeated reports of the
        same count idempotent.
        """
        self.advance(done_bytes - self.bytes_done)

    def done(self):
        if not self.active or self._ended:
            return
        self._ended = True
        elapsed = max(0.0, time.monotonic() - self._started)
        if self.bytes_done > 0:
            sys.stdout.write(
                f" done in {elapsed:.1f}s @ "
                f"{_fmt_rate(self.bytes_done, elapsed)}\n")
        else:
            sys.stdout.write(f" done in {elapsed:.1f}s\n")
        sys.stdout.flush()


def _get(url, timeout=30.0, min_bps=None):
    start = time.monotonic()
    deadline = start + timeout
    with urllib.request.urlopen(url, timeout=_timeout_left(deadline)) as r:
        cl = r.getheader("Content-Length")
        try:
            cl_int = int(cl) if cl is not None else None
        except ValueError:
            cl_int = None
        # Extend the transfer budget for a large body so a slow-but-
        # progressing download isn't aborted just for being big. Only
        # the total deadline grows; the per-chunk timeout still trips
        # on a real stall.
        if min_bps and cl_int:
            deadline = max(deadline, start + cl_int / min_bps)
        buf = bytearray()
        if cl_int is not None:
            progress = ProgressLine(f"GET {url}", cl_int)
            while len(buf) < cl_int:
                # urlopen's timeout is per socket operation, not a
                # transfer deadline. Re-arm a small timeout per chunk
                # and enforce the caller's total budget ourselves.
                _set_urlopen_timeout(r, deadline)
                chunk = r.read(min(_HTTP_CHUNK, cl_int - len(buf)))
                if not chunk:
                    break
                buf += chunk
                progress.advance(len(chunk))
            progress.done()
        else:
            while True:
                _set_urlopen_timeout(r, deadline)
                chunk = r.read(_HTTP_CHUNK)
                if not chunk:
                    break
                buf += chunk
        body = bytes(buf)
        return r.status, body, dict(r.headers)


def _post(url, data, timeout=DEFAULT_UPLOAD_S):
    if len(data) >= PROGRESS_THRESHOLD:
        return _post_streamed(url, data, timeout)
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def _post_streamed(url, data, timeout):
    """POST using http.client so we can send the body in chunks and
    print a progress line. urllib's ``urlopen(data=bytes)`` writes the
    whole body in one syscall with no hook for progress.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http":
        # Fall back rather than reimplement TLS; we only ever talk to
        # localhost via the SSH tunnel today.
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    host = parsed.hostname or "localhost"
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    deadline = time.monotonic() + timeout
    conn = http.client.HTTPConnection(
        host, port, timeout=_timeout_left(deadline))
    try:
        conn.connect()
        conn.sock.settimeout(_timeout_left(deadline))
        conn.putrequest("POST", path)
        conn.putheader("Content-Length", str(len(data)))
        conn.putheader("Content-Type", "application/octet-stream")
        conn.sock.settimeout(_timeout_left(deadline))
        conn.endheaders()
        sent = 0
        progress = ProgressLine(f"POST {url}", len(data))
        mv = memoryview(data)
        while sent < len(data):
            conn.sock.settimeout(_timeout_left(deadline))
            n = min(_HTTP_CHUNK, len(data) - sent)
            conn.send(mv[sent:sent + n])
            sent += n
            progress.advance(n)
        progress.done()
        conn.sock.settimeout(_timeout_left(deadline))
        resp = conn.getresponse()
        # Drain so the connection can be reused / closed cleanly.
        if conn.sock is not None:
            conn.sock.settimeout(_timeout_left(deadline))
        body = resp.read()
        # Mirror urllib.urlopen semantics: raise on non-2xx so the
        # spool-aware retry path in _post_spooled can distinguish 409
        # (park under refused/) from 5xx (retry next tick) and from
        # other 4xx (terminal). Without this, _post_streamed silently
        # returns the int status and the caller's try/except never
        # fires, which is a data-loss path for any artefact >= 1 MiB.
        if resp.status >= 400:
            raise urllib.error.HTTPError(
                url, resp.status, resp.reason or "",
                dict(resp.getheaders()), io.BytesIO(body))
        return resp.status
    finally:
        conn.close()


def _meta_float(headers, key, default, hard_max):
    """Pull X-Test-<Key> from response headers (case-insensitive),
    parse as a float, clamp to ``[1.0, hard_max]``. Falls back to
    ``default`` on missing/garbage values.
    """
    needle = f"x-test-{key.lower()}"
    val = None
    for k, v in headers.items():
        if k.lower() == needle:
            val = v
            break
    if val is None:
        return default
    try:
        n = float(val)
    except (TypeError, ValueError):
        return default
    # Reject NaN / inf explicitly. Today the clamp happens to handle
    # NaN safely (max(1.0, NaN) returns 1.0 in CPython's first-arg
    # tiebreaker), but that's a python-impl accident -- a future
    # refactor that reorders min/max would silently expose a bypass.
    import math
    if not math.isfinite(n):
        return default
    return max(1.0, min(n, hard_max))


_write_atomic = paths.write_atomic


def _write_heartbeat(path=HEARTBEAT_LOG, now=None):
    ts = datetime.fromtimestamp(now or time.time()).isoformat(
        timespec="seconds")
    _write_atomic(path, f"{ts}\n".encode())
    _notify_supervisor_heartbeat()


def _notify_supervisor_heartbeat():
    fd_s = os.environ.get(_HEARTBEAT_FD_ENV)
    if not fd_s:
        return
    try:
        os.write(int(fd_s), b".")
    except BlockingIOError:
        pass
    except OSError:
        os.environ.pop(_HEARTBEAT_FD_ENV, None)
    except ValueError:
        os.environ.pop(_HEARTBEAT_FD_ENV, None)


_PUSH_CIRCUIT_OPEN_S = 30.0
_push_circuit_until = 0.0  # monotonic; pushes skipped while > now


def _push_status(name, body):
    """Push a status snapshot to the server. Best-effort -- the bench
    keeps running if the server is unreachable; the local copy under
    STATE_DIR/status/ is still authoritative for tail/inspection.
    Circuit-breaker: a push that hits a connection error / timeout
    opens the circuit for _PUSH_CIRCUIT_OPEN_S so subsequent pushes
    short-circuit instead of each waiting the full 10s for the same
    failure. Without this a wedged SSH tunnel slowed the main poll
    loop from 2.5s/tick to 12.5s+, delaying cancel responsiveness.

    Returns True iff the push actually landed. Callers that key state
    on whether the server saw the snapshot (e.g. the busy->idle edge
    in _publish_inflight) need to distinguish landed vs short-circuit
    -- otherwise a tunnel blip during the transition leaves the
    server's snapshot stale forever.
    """
    global _push_circuit_until
    if time.monotonic() < _push_circuit_until:
        return False
    base = f"http://localhost:{HTTP_PORT}"
    try:
        _post(f"{base}/status/{name}", body, timeout=10.0)
        return True
    except Exception:
        # Don't traceback-spam the log on every refresh tick if the
        # server is offline; one line is enough.
        _push_circuit_until = time.monotonic() + _PUSH_CIRCUIT_OPEN_S
        print(datetime.now(),
              f"status/{name} push failed (server unreachable?); "
              f"backing off {_PUSH_CIRCUIT_OPEN_S:.0f}s")
        return False


def _snapshot_inflight():
    """Snapshot the in-flight sessions for the dashboard's live tail.

    Bounded per session: last 32 events, last 2KiB per stream, max
    8 streams. Operator gets a "what's actually happening RIGHT NOW"
    view without the artefact-fetch cycle. Read-only over session
    state, no mutations.
    """
    INFLIGHT_EVENTS = 32
    INFLIGHT_STREAM_BYTES = 2048
    INFLIGHT_MAX_STREAMS = 8
    out = []
    with _active_lock:
        sessions = list(_active_sessions.items())
    for digest, sess in sessions:
        try:
            with sess.lock:
                events = list(sess.events[-INFLIGHT_EVENTS:])
                stream_names = list(sess.streams.keys())
            stream_tails = {}
            for name in stream_names[:INFLIGHT_MAX_STREAMS]:
                s = sess.streams.get(name)
                if s is None:
                    continue
                raw = s.tail_bytes(INFLIGHT_STREAM_BYTES)
                stream_tails[name] = raw.decode(
                    "utf-8", errors="replace")
            out.append({
                "digest": digest,
                "elapsed_s": time.monotonic() - sess.t0,
                "n_ops": len(sess.ops_log),
                "n_errors": len(sess.errors),
                "events": [
                    {"t": e["t"], "kind": e["kind"],
                     "source": e["source"], "msg": e["msg"]}
                    for e in events],
                "stream_tails": stream_tails,
            })
        except Exception:
            # Don't let a single half-built Session break the whole
            # snapshot.
            traceback.print_exc()
    return out


_was_active_inflight = False


def _publish_inflight():
    """Publish only the inflight snapshot. Cheap; called on every
    poll-loop tick (2.5s) so the dashboard's live tail tracks the
    bench in near-real-time. Skips the network push when the bench
    has been idle so an idle bench doesn't churn the tunnel -- but
    forces one push on the busy->idle transition so the server's
    inflight.json doesn't carry the just-finished session's tail
    forever (the dashboard would attach stale "live" metadata to a
    completed job's row).
    """
    global _was_active_inflight
    with _active_lock:
        any_active = bool(_active_sessions)
    inflight = json.dumps(_snapshot_inflight()).encode()
    os.makedirs(STATUS, mode=0o700, exist_ok=True)
    _write_atomic(os.path.join(STATUS, "inflight.json"), inflight)
    pushed = True
    if any_active or _was_active_inflight:
        pushed = _push_status("inflight.json", inflight)
    # Only retire the busy edge if the push actually landed. If the
    # tunnel was open earlier (last tick had any_active=True so we
    # were marked busy) and goes flaky right at the busy->idle
    # transition, the next tick must retry the empty snapshot --
    # otherwise the server's inflight feed keeps the just-finished
    # session's tail forever.
    if pushed:
        _was_active_inflight = any_active


def _publish_status(registry, plugins_by_name):
    os.makedirs(STATUS, mode=0o700, exist_ok=True)
    devices = json.dumps(registry.list_devices(), indent=2).encode()
    _write_atomic(os.path.join(STATUS, "devices.json"), devices)
    _push_status("devices.json", devices)

    # bench.json: small file the dashboard reads to render the bench
    # identity in the header + a few process-health metrics so the
    # operator can spot slow-grow problems (leaked drain threads,
    # leaked file descriptors, runaway memory) BEFORE they cause an
    # OOM kill on a bench that's been up for months. Also includes
    # PENDING/ + PENDING/refused/ counts so a stuck-tunnel backlog
    # is visible at a glance instead of requiring an ssh + ls.
    pending_n = _count_dir_files(PENDING, ".tar")
    refused_n = _count_dir_files(
        os.path.join(PENDING, "refused"), ".tar")
    bench = json.dumps({
        "bench_id": bench_id(),
        "code_digest": _CODE_DIGEST,
        "uptime_s": time.monotonic() - _BENCH_START_MONO,
        "thread_count": threading.active_count(),
        "open_fd_count": _count_open_fds(),
        "rss_bytes": _self_rss_bytes(),
        "pending_uploads": pending_n,
        "refused_spools": refused_n,
    }).encode()
    _write_atomic(os.path.join(STATUS, "bench.json"), bench)
    _push_status("bench.json", bench)

    _publish_inflight()

    ops_map = {}
    for name, pl in plugins_by_name.items():
        ops_map[name] = {
            "doc": pl.doc,
            "ops": {op_name: {"args": op.args,
                              "optional_args": op.optional_args or {},
                              "doc": op.doc}
                    for op_name, op in pl.ops.items()},
        }
    ops = json.dumps(ops_map, indent=2).encode()
    _write_atomic(os.path.join(STATUS, "ops.json"), ops)
    _push_status("ops.json", ops)


_signaled_cancels = set()  # digests this poller has already signalled
_signaled_lock = threading.Lock()


def _drain_cancels():
    """Pull cancel markers and signal the matching active sessions.

    The server now keeps cancel markers across reads so a marker that
    arrives mid-dispatch is found again on the next tick once the
    Session is registered. To avoid re-signalling (and re-logging)
    the same cancel every 2.5s for the duration of pack+spool+upload,
    track which digests we've already signalled in this process.
    Entries are evicted when the session leaves _active_sessions.
    """
    base = f"http://localhost:{HTTP_PORT}"
    try:
        status, body, _ = _get(f"{base}/cancels", timeout=10.0)
    except Exception:
        return
    if status != 200 or not body:
        return
    try:
        digests = json.loads(body.decode())
    except Exception:
        return
    # Garbage-collect _signaled_cancels for jobs we no longer track.
    with _signaled_lock, _active_lock:
        _signaled_cancels.intersection_update(_active_sessions)
    for d in digests:
        with _signaled_lock:
            if d in _signaled_cancels:
                continue
        with _active_lock:
            sess = _active_sessions.get(d)
        if sess is None:
            # The current poller has no such live session. Resolve the
            # server-side DONE/CANCEL record now; otherwise the UI can
            # show "cancel pending" forever for a stale running row
            # left behind by a crashed/restarted worker.
            _resolve_stale_cancel(d)
            continue
        sess.signal_cancel()
        with _signaled_lock:
            _signaled_cancels.add(d)
        print(datetime.now(), f"[{d[:8]}] cancel signaled")


def _resolve_stale_cancel(digest):
    base = f"http://localhost:{HTTP_PORT}"
    try:
        _post(f"{base}/cancels/{digest}/stale", b"", timeout=5.0)
        print(datetime.now(), f"[{digest[:8]}] stale cancel resolved")
    except Exception:
        pass


def _drain_release_markers(registry):
    try:
        names = os.listdir(RELEASE)
    except FileNotFoundError:
        return
    import re as _re
    safe = _re.compile(r"^[A-Za-z0-9._-]+$")
    for n in names:
        path = os.path.join(RELEASE, n)
        # Defence in depth: server-side validation already gates the
        # marker filename, but if RELEASE is ever populated by another
        # path (dropped file, future feature) we don't want to call
        # release_now("..").
        if not safe.match(n):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        try:
            ok = registry.release_now(n)
            # Also clear any quarantine flag on the key so an operator
            # who replugged a wedged device can recover without
            # restarting the poller (the prior behaviour required a
            # full restart to clear quarantined keys, which conflicts
            # with the "run unsupervised for years" goal).
            cleared_q = False
            with registry.lock:
                if n in registry.quarantined:
                    registry.quarantined.discard(n)
                    cleared_q = True
            os.remove(path)
            tail = " (quarantine cleared)" if cleared_q else ""
            print(datetime.now(), "release",
                  n, "ok" if ok else "(was not cached / in use)",
                  tail)
        except Exception:
            traceback.print_exc()


_sweep_deferred_logged = False


def _drain_sweep_markers(registry, plugins_by_name):
    """Honour a REST-triggered re-sweep: re-probe + verify + republish.

    Defers when any session is active. verify_sweep's per-device open
    would otherwise race the session's eager dev_lock: the sweep
    thread blocks for VERIFY_OPEN_TIMEOUT_S, then quarantines the
    very key the session is currently using, and the session crashes
    on its next op with "device hung; replug + restart poller". So
    we leave the marker in place; the next tick (or the one after
    the session ends) will re-honour it. Same rationale as the
    SIGHUP handler.
    """
    try:
        names = os.listdir(SWEEP)
    except FileNotFoundError:
        return
    if not names:
        return
    with _active_lock:
        active = len(_active_sessions)
    if active:
        # Log once per deferral so the operator who clicked "Run
        # Inventory" on the dashboard at least sees that the click
        # was received and is waiting for the bench to idle. Without
        # this, the marker silently sits in SWEEP/ until the queue
        # drains and the operator thinks nothing happened.
        global _sweep_deferred_logged
        if not _sweep_deferred_logged:
            print(datetime.now(),
                  f"sweep deferred: {active} session(s) still active "
                  f"(will run when idle)")
            _sweep_deferred_logged = True
        return
    _sweep_deferred_logged = False
    print(datetime.now(), "sweep requested via REST")
    registry.refresh()
    _print_device_table(registry.verify_sweep(), registry)
    _publish_status(registry, plugins_by_name)
    for n in names:
        try:
            os.remove(os.path.join(SWEEP, n))
        except OSError:
            pass


_SPEC_LOCATOR_KEYS = (
    "serial_port", "resource", "ft4222_desc", "ft2232h_desc",
    "ip", "usb_serial",
)


def _describe_spec(spec):
    """Render the spec's most user-visible identifier (COM port, VISA
    resource, FTDI descriptor, IP, ...).  Empty string if none found.
    """
    for k in _SPEC_LOCATOR_KEYS:
        v = spec.get(k)
        if v:
            return str(v)
    return ""


def _print_device_table(verify_map, registry):
    """Pretty-print: plugin.id   location   latency   status/identity."""
    rows = registry.list_devices()
    if not rows:
        print("  (no devices present)")
        return
    w_id = max(len(r["id"]) for r in rows)
    locs = [_describe_spec(r["spec"]) for r in rows]
    w_loc = max([len(x) for x in locs] + [0])
    for r, loc in zip(rows, locs):
        v = r.get("verify") or {}
        ok = v.get("ok")
        mark = "OK   " if ok else ("FAIL " if ok is False else "?    ")
        lat = f"{v.get('latency_ms', 0):7.1f} ms" if v else "       --"
        if v and v.get("err"):
            tail = v["err"]
        elif v and v.get("ok"):
            tail = ("identity verified" if v.get("verified")
                    else "open ok (plugin has no identity handshake)")
        else:
            tail = "(not yet verified)"
        print(f"  [{mark}] {r['id']:<{w_id}}  {loc:<{w_loc}}  "
              f"{lat}  {tail}")


def _dispatch(payload, headers, registry, plugins_by_name):
    job_id = hashlib.sha256(payload).hexdigest()
    tag = f"[{job_id[:8]}]"
    from session import DEFAULT_SESSION_S, MAX_SESSION_S
    runtime_s = _meta_float(headers, "Runtime",
                            DEFAULT_SESSION_S, MAX_SESSION_S)
    upload_s = _meta_float(headers, "Upload-Timeout",
                           DEFAULT_UPLOAD_S, MAX_UPLOAD_S)
    try:
        parsed = plan.load_tar(payload)
    except plan.PlanError as e:
        print(datetime.now(), tag,
              f"pickup {len(payload):>8} B  ?  parse failed: {e}")
        tar = _failure_artefact(job_id, f"plan parse failed: {e}")
        _post_artefact(job_id, tar, upload_s, try_now=False)
        return

    needed = sorted(plan.required_device_refs(parsed))
    devs = ",".join(needed) if needed else "(none)"
    print(datetime.now(), tag,
          f"pickup {len(payload):>8} B  {devs}")

    try:
        _validate_against_plugins(parsed, plugins_by_name, registry)
    except Exception as e:
        tar = _failure_artefact(job_id, f"validation: {e}")
        _post_artefact(job_id, tar, upload_s, try_now=False)
        return

    session = Session(registry, parsed, runtime_s=runtime_s)
    # Stamp identity for the manifest. plan_digest = sha256 of the
    # exact tar bytes the agent submitted (job_id is that already).
    # code_digest is captured once at poller startup and shared
    # across sessions.
    session.plan_digest = job_id
    session.code_digest = _CODE_DIGEST
    with _active_lock:
        _active_sessions[job_id] = session
    try:
        session.run_all(plugins_by_name)
        tar, _manifest_text = pack_artefact(session)
    finally:
        with _active_lock:
            _active_sessions.pop(job_id, None)
    _post_artefact(job_id, tar, upload_s, try_now=False)


def _validate_against_plugins(parsed, plugins_by_name, registry):
    """Reject jobs with unknown plugins or op names before running.

    Device *instance* presence is NOT checked here: the registry's
    view of which devices are plugged in refreshes only every
    DEVICE_REFRESH_S seconds, so a plan whose earlier ops switch the
    board into a new mode (e.g. bench_mcu:send data="r" puts MP135 in
    DFU) would unfairly fail validation if instance presence were
    required up front. Missing instances surface as op-time errors
    via session._run_device_op, which does a targeted re-probe of the
    relevant plugin before giving up.
    """
    for op in parsed.ops:
        if op.device is None:
            continue
        plugin_name, _ = plan.split_device_ref(op.device)
        if plugin_name not in plugins_by_name:
            known = ", ".join(sorted(plugins_by_name)) or "(none)"
            raise ValueError(
                f"line {op.lineno}: unknown device {op.device!r}; "
                f"known plugins: {known}")
        pl = plugins_by_name[plugin_name]
        if op.verb in ("open", "close"):
            continue
        if op.verb not in pl.ops:
            ops_known = ", ".join(sorted(pl.ops)) or "(none)"
            raise ValueError(
                f"line {op.lineno}: {op.device!r} has no op "
                f"{op.verb!r}; known ops: {ops_known}")


def _failure_artefact(job_id, message):
    import io
    import tarfile
    from session import make_manifest
    buf = io.BytesIO()
    # Same shape as session.pack_artefact's manifest (one schema, one
    # builder) so anything reading manifest.json sees consistent keys
    # whether the run succeeded, errored, or never started. status is
    # the discriminator. Identity fields (run_id, plan_digest,
    # code_digest) MUST be populated even on failure so an aggregator
    # can group failed runs against their plan + bench code state.
    manifest = make_manifest(
        status="failed",
        t0_monotonic=time.monotonic(),
        t0_wall=time.time(),
        runtime_s=0.0,
        streams=[],
        n_ops=0,
        n_errors=1,
        required_devices=[],
        expectations=[],
        message=message,
        run_id=f"sess-{uuid.uuid4().hex[:12]}",
        plan_digest=job_id,
        code_digest=_CODE_DIGEST,
        blob_digests={},
    )
    manifest["job_id"] = job_id
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo("manifest.json")
        ti.size = len(manifest_bytes)
        tf.addfile(ti, io.BytesIO(manifest_bytes))
        err = message.encode() + b"\n"
        ti = tarfile.TarInfo("errors.log")
        ti.size = len(err)
        tf.addfile(ti, io.BytesIO(err))
    return buf.getvalue()


_SPOOL_NAME_RE = re.compile(r"^([0-9a-f]{64})\.[0-9a-f]+\.tar$")


def _spool_artefact(job_id, tar_bytes):
    """Write the artefact atomically to ``PENDING/<digest>.<run>.tar``.

    The trailing run-id is uuid-derived so each spool attempt has a
    unique filename. Without it, a re-submit of the same digest while
    an old upload was retrying would create two callers racing on
    the same path: ``_spool_artefact`` would ``os.replace`` the file
    inode out from under the old caller's already-open fd, and the
    OLD caller's later ``os.unlink`` would silently delete the NEW
    caller's tar. Net result: the agent fetches the previous run's
    bytes labelled as the new digest. The unique-name scheme makes
    each spool file owned by exactly one in-flight upload; the digest
    survives in the prefix so _drain_pending_uploads can post each
    one to ``/<digest>.tar``.
    """
    os.makedirs(PENDING, mode=0o700, exist_ok=True)
    run = uuid.uuid4().hex[:12]
    path = os.path.join(PENDING, f"{job_id}.{run}.tar")
    paths.write_atomic(path, tar_bytes)
    return path


def _post_spooled(spool_path, timeout_s=DEFAULT_UPLOAD_S):
    """Try POSTing one spooled artefact. On 2xx or 4xx (permanent
    refusal), unlink it. On 5xx / connection errors leave the file
    and log; the next main-loop tick will retry.
    """
    name = os.path.basename(spool_path)
    m = _SPOOL_NAME_RE.match(name)
    if not m:
        # Stray file, not one of ours. Leave it; a later operator
        # cleanup is safer than blindly deleting.
        return False
    digest = m.group(1)
    base = f"http://localhost:{HTTP_PORT}"
    try:
        with open(spool_path, "rb") as f:
            body = f.read()
    except FileNotFoundError:
        return False
    try:
        _post(f"{base}/{digest}.tar", body, timeout=timeout_s)
    except urllib.error.HTTPError as e:
        if e.code == 409:
            # No matching job record on the server (e.g. the operator
            # clicked "delete all" between session-end and our retry).
            # Don't unlink: the spool is the only on-bench evidence
            # the run happened. Also don't park it immediately: after a
            # server crash/restart or stale-job sweep, the operator may
            # resubmit the same digest and the next retry will be valid.
            try:
                age_s = time.time() - os.path.getmtime(spool_path)
            except OSError:
                age_s = 0.0
            if age_s < REFUSED_GRACE_S:
                print(datetime.now(),
                      f"POST {name} refused 409; keeping in "
                      f"{PENDING} for retry for up to "
                      f"{REFUSED_GRACE_S:.0f}s "
                      f"(next retry in {REFUSED_RETRY_BACKOFF_S:.0f}s)")
                _refused_retry_after[spool_path] = (
                    time.monotonic() + REFUSED_RETRY_BACKOFF_S)
                return False
            # Grace expired. Park under refused/ so the operator can
            # re-POST it manually after re-queueing.
            refused = os.path.join(PENDING, "refused")
            os.makedirs(refused, mode=0o700, exist_ok=True)
            try:
                os.replace(spool_path, os.path.join(refused, name))
                print(datetime.now(),
                      f"POST {name} refused 409 after retry grace; "
                      f"parked under "
                      f"{refused} (resubmit the digest, then move "
                      f"the file back into {PENDING} to retry).")
            except FileNotFoundError:
                # A concurrent _post_spooled call (most likely the
                # main-loop drain racing the session's own _post_
                # artefact) already handled the same spool first --
                # either POSTed it 200 + unlinked, or parked it
                # under refused/ before we got here. Either way the
                # artefact is accounted for; nothing to do.
                pass
            except OSError as move_err:
                print(datetime.now(),
                      f"POST {name} refused 409 + park failed: "
                      f"{move_err} (leaving spool in place)")
            return False
        if 400 <= e.code < 500:
            print(datetime.now(),
                  f"POST {name} refused with {e.code} (giving up): {e}")
            try:
                os.unlink(spool_path)
            except FileNotFoundError:
                pass
            return False
        print(datetime.now(),
              f"POST {name} failed with {e.code} (will retry): {e}")
        return False
    except Exception:
        # Connection-class failure (URLError, socket.timeout, etc.).
        # Open the push circuit so the next _drain_pending_uploads
        # tick skips entirely instead of waiting the full timeout
        # again on every spool against a wedged tunnel.
        global _push_circuit_until
        _push_circuit_until = time.monotonic() + _PUSH_CIRCUIT_OPEN_S
        print(datetime.now(),
              f"POST {name} failed (will retry):",
              traceback.format_exc().splitlines()[-1])
        return False
    try:
        os.unlink(spool_path)
    except FileNotFoundError:
        pass
    return True


DRAIN_PER_SPOOL_TIMEOUT_S = 30.0
# Heuristic for "healthy slow tunnel" budget: assume the tunnel can
# move at least this many bytes per second. A 100 MiB artefact
# therefore gets ~100s; a 5 MiB artefact still uses the 30s floor.
# Scaled cap stays well under DEFAULT_UPLOAD_S=600s, so the bench
# stays responsive even under sustained slow-tunnel conditions.
DRAIN_BYTES_PER_S_FLOOR = 1_000_000


def _drain_per_spool_timeout(spool_path):
    """Per-spool upload cap for the main-loop drain. Floor is the
    constant 30s (covers a wedged tunnel quickly); scales up with
    file size so a healthy-but-slow tunnel can complete a 100 MiB
    artefact instead of getting stuck retrying it forever (each
    attempt failing identically with no progress).
    """
    try:
        size = os.path.getsize(spool_path)
    except OSError:
        return DRAIN_PER_SPOOL_TIMEOUT_S
    scaled = DRAIN_PER_SPOOL_TIMEOUT_S + size / DRAIN_BYTES_PER_S_FLOOR
    return min(DEFAULT_UPLOAD_S, scaled)


def _drain_pending_uploads(timeout_s=None):
    """Try to POST every spooled artefact. Called on each main-loop
    tick so a transient server outage doesn't lose work.

    Honours the same circuit-breaker that _push_status uses: when
    the SSH tunnel is wedged (TCP black-hole, not RST), each upload
    can take the full timeout before giving up. The circuit-breaker
    skips next tick on the first failure, but the FIRST failure
    still has to actually time out -- so the per-spool timeout is
    DRAIN_PER_SPOOL_TIMEOUT_S (30s) plus a size-scaled allowance
    instead of the foreground DEFAULT_UPLOAD_S (600s). The long
    timeout is for the foreground _post_artefact path where a
    worker thread is waiting on its own upload; the main-loop
    drain just retries next tick, so a tight floor keeps the loop
    responsive (cancels still get drained, /plan still gets pulled)
    while the size scaling lets large healthy uploads actually
    complete on slow tunnels.
    """
    if time.monotonic() < _push_circuit_until:
        return
    try:
        names = sorted(os.listdir(PENDING))
    except FileNotFoundError:
        return
    for n in names:
        if not n.endswith(".tar"):
            continue
        path = os.path.join(PENDING, n)
        if time.monotonic() < _refused_retry_after.get(path, 0.0):
            continue
        per_spool = (timeout_s
                     if timeout_s is not None
                     else _drain_per_spool_timeout(path))
        _post_spooled(path, timeout_s=per_spool)


_pending_drain_lock = threading.Lock()


def _kick_pending_upload_drain(timeout_s=None):
    """Start one background pending-upload drain if none is active.

    Upload retries are tunnel-facing and can take their timeout budget
    under packet loss. They must not run inline on the poll loop,
    because stale heartbeat means "poll loop stopped".
    """
    if time.monotonic() < _push_circuit_until:
        return False
    if not _pending_drain_lock.acquire(blocking=False):
        return False

    def _run():
        try:
            _drain_pending_uploads(timeout_s=timeout_s)
        finally:
            _pending_drain_lock.release()

    threading.Thread(
        target=_run, daemon=True, name="pending-upload-drain").start()
    return True


def _post_artefact(job_id, tar_bytes, timeout_s=DEFAULT_UPLOAD_S,
                   try_now=True):
    """Spool the artefact to disk first, then try to upload. If the
    upload fails, the file stays in PENDING and the next main-loop
    tick's _drain_pending_uploads picks it up.
    """
    spool = _spool_artefact(job_id, tar_bytes)
    if try_now:
        _post_spooled(spool, timeout_s=timeout_s)


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _resolve_orphan_running_jobs():
    """Startup-only: fail any job the server still lists as 'running'.

    This poller just started, and the STATE_DIR advisory lock
    guarantees no second poller shares this state -- so a 'running'
    row can have no live session behind it. It is a leftover from a
    crash or a watchdog os._exit mid-run. Without this, the row shows
    'running' forever: the submitting agent polls a job that can never
    finish, and its device locks look held by a job that no longer
    exists. Jobs whose artefact was spooled before the crash are
    skipped -- the PENDING upload resolves them as done.
    """
    base = f"http://localhost:{HTTP_PORT}"
    try:
        _status, body, _headers = _get(f"{base}/jobs", timeout=10.0)
        jobs = json.loads(body)
    except Exception:
        return
    try:
        spooled = {n.split(".", 1)[0] for n in os.listdir(PENDING)}
    except FileNotFoundError:
        spooled = set()
    for j in jobs if isinstance(jobs, list) else []:
        if j.get("status") != "running":
            continue
        digest = j.get("digest") or ""
        if not _DIGEST_RE.match(digest) or digest in spooled:
            continue
        print(datetime.now(),
              f"[{digest[:8]}] orphaned 'running' job from a previous "
              f"poller incarnation; posting failure artefact")
        tar = _failure_artefact(
            digest,
            "poller restarted while this job was in flight (crash or "
            "watchdog os._exit); the run was lost -- resubmit if "
            "still needed")
        try:
            _post_artefact(digest, tar)
        except Exception:
            traceback.print_exc()


# Supervisor knobs. Match the prior shell-supervisor's behaviour so
# the move from run_poller.sh to in-process supervision is invisible
# to the operator.
SUPERVISOR_COOLDOWN_S = 5.0
SUPERVISOR_RUN_BUDGET_S = 10.0
SUPERVISOR_MAX_FAST_FAILS = 5
SUPERVISOR_HEARTBEAT_CHECK_S = 5.0
SUPERVISOR_HEARTBEAT_STALE_S = 120.0
SUPERVISOR_STACK_DUMP_GRACE_S = 1.0
# Hidden flag the supervisor passes to its worker child so the child
# knows not to recurse into another supervisor.
_WORKER_FLAG = "--_worker"
_HEARTBEAT_FD_ENV = "TEST_SERV_HEARTBEAT_FD"
_LOCK_HELD_ENV = "TEST_SERV_POLLER_LOCK_HELD"
_LOCK_FD_ENV = "TEST_SERV_POLLER_LOCK_FD"


def _heartbeat_stale(last_heartbeat_mono, now=None):
    now = now or time.monotonic()
    age = now - last_heartbeat_mono
    return age > SUPERVISOR_HEARTBEAT_STALE_S, age


def _supervise():
    """Respawn the worker child on non-zero exit so a third-party C
    extension SIGABRT (pyusb / pyft4222 / ftd2xx heap corruption when
    a USB device flaps mid-syscall) doesn't take the bench offline.

    Folded into poller.py instead of a separate shell script so the
    operator can't accidentally invoke an un-supervised poller and
    lose the bench to the next driver glitch. `python3 poller.py`
    runs the supervisor; the supervisor spawns a worker child with
    the hidden _WORKER_FLAG and respawns it on crash. Pass
    --no-supervisor to run un-supervised (debug only).

    Clean exits (rc=0), SIGINT (rc=130 / -SIGINT), and SIGTERM
    (rc=143 / -SIGTERM) all stop the supervisor; otherwise it
    respawns after SUPERVISOR_COOLDOWN_S. A worker whose heartbeat
    stops is considered wedged even if the process is still alive:
    dump Python thread stacks via SIGUSR1, then SIGKILL it so the
    next generation takes over. Five fast-fails in a row abort so an
    operator with a config error sees the failure instead of the
    supervisor spinning forever.
    """
    import subprocess
    import select

    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    # Hold the singleton lock in the supervisor, not in each transient
    # worker. This matters when poller.py itself is wrapped by an outer
    # shell loop such as `while true; do timeout 3600 python3 poller.py;
    # done`: timeout may kill/restart the supervisor while an old worker
    # is still tearing down. If only workers lock, the replacement
    # supervisor can start and fast-fail child after child on the old
    # worker's lock. With supervisor-owned locking, a second supervisor
    # refuses immediately and the outer loop naturally waits/retries.
    _acquire_poller_lock()

    # Pass through any extra args (e.g. --no-supervisor's siblings if
    # we add some later, or --port / config overrides). argv[0] is
    # this script's path; everything else is forwarded.
    extra = [a for a in sys.argv[1:] if a != _WORKER_FLAG]

    child_box = [None]
    signal_counts = {}

    def _forward(signum, _frame):
        signal_counts[signum] = signal_counts.get(signum, 0) + 1
        c = child_box[0]
        if c is None or c.poll() is not None:
            return
        try:
            if (signum == getattr(signal, "SIGINT", None)
                    and signal_counts[signum] >= 2):
                print(f"{datetime.now()} supervisor: second SIGINT; "
                      f"killing worker child", file=sys.stderr)
                c.kill()
            else:
                c.send_signal(signum)
        except Exception:
            pass

    for sig_name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _forward)
            except (ValueError, OSError):
                pass

    print(f"{datetime.now()} supervisor: starting worker child "
          f"(--no-supervisor to run un-supervised for debug)")

    fast_fails = 0
    while True:
        argv = [sys.executable, os.path.abspath(__file__),
                _WORKER_FLAG] + extra
        start = time.monotonic()
        last_heartbeat_mono = start
        hb_r = hb_w = None
        env = os.environ.copy()
        env[_LOCK_HELD_ENV] = "1"
        pass_fds = []
        if _poller_lock_fd is not None:
            try:
                os.set_inheritable(_poller_lock_fd, True)
            except OSError:
                pass
            env[_LOCK_FD_ENV] = str(_poller_lock_fd)
            pass_fds.append(_poller_lock_fd)
        try:
            hb_r, hb_w = os.pipe()
            try:
                os.set_blocking(hb_w, False)
            except (AttributeError, OSError):
                pass
            os.set_inheritable(hb_w, True)
            env[_HEARTBEAT_FD_ENV] = str(hb_w)
            pass_fds.append(hb_w)
            p = subprocess.Popen(argv, pass_fds=tuple(pass_fds), env=env)
        except OSError as e:
            for fd in (hb_r, hb_w):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            print(f"{datetime.now()} supervisor: spawn failed: {e}",
                  file=sys.stderr)
            return 1
        try:
            os.close(hb_w)
        except OSError:
            pass
        child_box[0] = p
        killed_for_stale_heartbeat = False
        while True:
            rc = p.poll()
            if rc is not None:
                break
            try:
                readable, _, _ = select.select(
                    [hb_r], [], [], SUPERVISOR_HEARTBEAT_CHECK_S)
            except (OSError, ValueError):
                readable = []
            if readable:
                try:
                    data = os.read(hb_r, 4096)
                except OSError:
                    data = b""
                if data:
                    last_heartbeat_mono = time.monotonic()
            stale, age = _heartbeat_stale(last_heartbeat_mono)
            if stale:
                killed_for_stale_heartbeat = True
                sigusr1 = getattr(signal, "SIGUSR1", None)
                dump_msg = ("dumping stacks then "
                            if sigusr1 is not None
                            else "stack dump unavailable; ")
                print(f"{datetime.now()} supervisor: worker heartbeat "
                      f"stale for {age:.0f}s; {dump_msg}"
                      f"killing worker pid={p.pid}",
                      file=sys.stderr)
                if sigusr1 is not None:
                    try:
                        p.send_signal(sigusr1)
                    except Exception:
                        pass
                    time.sleep(SUPERVISOR_STACK_DUMP_GRACE_S)
                try:
                    p.kill()
                except Exception:
                    pass
                rc = p.wait()
                break
        try:
            os.close(hb_r)
        except OSError:
            pass
        elapsed = time.monotonic() - start

        # Negative rc on POSIX = killed by signal -rc.
        sig_rc = -rc if rc < 0 else 0
        if rc == 0:
            print(f"{datetime.now()} supervisor: worker exited "
                  f"cleanly (rc=0); not respawning")
            return 0
        if rc == 130 or sig_rc == getattr(signal, "SIGINT", 2):
            print(f"{datetime.now()} supervisor: worker exited via "
                  f"SIGINT; not respawning")
            return 0
        if rc == 143 or sig_rc == getattr(signal, "SIGTERM", 15):
            print(f"{datetime.now()} supervisor: worker exited via "
                  f"SIGTERM; not respawning")
            return 0

        if killed_for_stale_heartbeat:
            fast_fails = 0
            print(f"{datetime.now()} supervisor: worker killed after "
                  f"{elapsed:.1f}s for stale heartbeat (rc={rc}); "
                  f"respawning in {SUPERVISOR_COOLDOWN_S:.0f}s",
                  file=sys.stderr)
        elif elapsed < SUPERVISOR_RUN_BUDGET_S:
            fast_fails += 1
            print(f"{datetime.now()} supervisor: worker died after "
                  f"{elapsed:.1f}s (rc={rc}, fast-fail "
                  f"{fast_fails}/{SUPERVISOR_MAX_FAST_FAILS})",
                  file=sys.stderr)
            if fast_fails >= SUPERVISOR_MAX_FAST_FAILS:
                print(f"{datetime.now()} supervisor: too many fast "
                      f"fails; giving up so the operator can "
                      f"investigate", file=sys.stderr)
                return 1
        else:
            fast_fails = 0
            print(f"{datetime.now()} supervisor: worker died after "
                  f"{elapsed:.1f}s (rc={rc}); respawning in "
                  f"{SUPERVISOR_COOLDOWN_S:.0f}s", file=sys.stderr)
        time.sleep(SUPERVISOR_COOLDOWN_S)


def main():
    # Operator-facing entry: by default we self-supervise. The hidden
    # _WORKER_FLAG marks the spawned child; --no-supervisor lets the
    # operator opt out for a debug run where they want a crash to be
    # immediately visible instead of respawning.
    if _WORKER_FLAG in sys.argv:
        sys.argv = [a for a in sys.argv if a != _WORKER_FLAG]
        return _run_poller()
    if "--no-supervisor" in sys.argv:
        sys.argv = [a for a in sys.argv if a != "--no-supervisor"]
        return _run_poller()
    return _supervise()


def _run_poller():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    os.makedirs(PENDING, mode=0o700, exist_ok=True)
    # Advisory lock on STATE_DIR so a second poller against the same
    # state dir can't race on PENDING/ uploads or duplicate-pickup
    # plans (rename gate handles the latter, but the cleaner
    # behaviour is "refuse the second poller and tell the operator").
    if os.environ.get(_LOCK_HELD_ENV) != "1":
        _acquire_poller_lock()
    _rotate_log_if_large(LOG)
    log_f = open(LOG, "a", buffering=1, encoding="utf-8", errors="replace")
    _log_f[0] = log_f
    sys.stdout = _Tee(sys.__stdout__, log_f)
    sys.stderr = _Tee(sys.__stderr__, log_f)
    _register_stack_dump()
    _write_heartbeat()
    print(datetime.now(), f"state dir: {STATE_DIR}")
    print(datetime.now(), f"logging to {LOG}")
    print(datetime.now(), f"stack dumps to {STACK_DUMP_LOG}")
    print(datetime.now(), "loading plugins...")
    plugins_by_name = plugins.load_all()
    _write_heartbeat()
    print(datetime.now(), "plugins:", sorted(plugins_by_name.keys()))

    registry = DeviceRegistry(plugins_by_name)
    registry.refresh()
    _write_heartbeat()
    print(datetime.now(), "startup verify sweep:")
    registry.verify_sweep()
    _write_heartbeat()
    _print_device_table(registry.verify_results, registry)
    _publish_status(registry, plugins_by_name)
    # Upload anything spooled by a previous incarnation FIRST so those
    # jobs resolve as done, then fail whatever 'running' rows remain --
    # they belong to no live session and would otherwise sit 'running'
    # forever after a crash / watchdog os._exit.
    _drain_pending_uploads(timeout_s=30.0)
    _resolve_orphan_running_jobs()

    def _sighup(signum, frame):
        print(datetime.now(), "SIGHUP: refresh plugins + devices")
        # Refuse the reload while sessions are running -- live plugin
        # modules with cached USB handles would be swapped under
        # them. The operator's edits land on the next idle window.
        with _active_lock:
            if _active_sessions:
                print(datetime.now(),
                      f"SIGHUP: deferring -- {len(_active_sessions)} "
                      f"session(s) still running")
                return
        try:
            new_plugins = plugins.load_all(reload=True)
            plugins_by_name.clear()
            plugins_by_name.update(new_plugins)
            registry.plugins = plugins_by_name
            registry.refresh()
            global _CODE_DIGEST
            _CODE_DIGEST = _compute_code_digest()
            _publish_status(registry, plugins_by_name)
        except Exception:
            traceback.print_exc()

    # Two-step Ctrl-C: first ^C raises KeyboardInterrupt so the
    # graceful cleanup at the bottom of main() runs (cancel sessions,
    # SIGKILL subprocs, drain uploads, close handles). A SECOND ^C
    # at any point afterwards calls os._exit() directly so the
    # operator can always escape -- without this, a Ctrl-C that lands
    # while a USB close syscall in registry.close_all() is hung in C
    # never returns to Python and the process becomes unkillable
    # short of SIGKILL from another shell.
    _sigint_count = [0]

    def _sigint_handler(_signum, _frame):
        _sigint_count[0] += 1
        if _sigint_count[0] >= 2:
            sys.stderr.write(
                "\nSIGINT (second): forcing exit, no further cleanup\n")
            os._exit(130)
        # First ^C: re-raise as KeyboardInterrupt so the existing
        # except KeyboardInterrupt cleanup path runs.
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGINT, _sigint_handler)
        signal.signal(signal.SIGHUP, _sighup)
    except (AttributeError, ValueError):
        # windows / non-main thread: skip
        pass

    last_refresh = time.monotonic()
    last_heartbeat = 0.0
    base = f"http://localhost:{HTTP_PORT}"

    # Useful concurrency is bounded by the number of distinct device
    # instances -- any two jobs competing for the same device serialize
    # on the registry's per-device lock anyway. Two jobs touching
    # different *instances* of the same plugin (mp135.evb vs
    # mp135.custom, dfu.evb vs dfu.custom, fpga.hx1k vs fpga.hx8k) can
    # run in parallel; the cap is over the spec count, not the plugin
    # count. Floor of 2 so a sparsely-populated bench still serves
    # back-to-back submissions. Jobs beyond the cap stay queued on
    # disk in inputs/.
    max_active = max(2, len(registry.specs))
    worker_slot = threading.Semaphore(max_active)
    print(datetime.now(),
          f"dispatch: at most {max_active} job(s) active "
          f"(= number of device instances)")

    def _worker(body, headers):
        try:
            _dispatch(body, headers, registry, plugins_by_name)
        finally:
            worker_slot.release()

    try:
        while True:
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                _write_heartbeat()
                last_heartbeat = now
            _drain_release_markers(registry)
            _drain_sweep_markers(registry, plugins_by_name)
            _drain_cancels()
            _kick_pending_upload_drain()
            # Refresh the live-tail snapshot every poll tick so the
            # dashboard sees in-flight events + stream tails with
            # ~2.5s latency rather than waiting for the artefact.
            _publish_inflight()
            _rotate_log_in_loop(LOG)

            if now - last_refresh > DEVICE_REFRESH_S:
                registry.refresh()
                _publish_status(registry, plugins_by_name)
                _gc_refused_spools()
                last_refresh = time.monotonic()

            # Try to claim a worker slot, but don't block: if all
            # workers are busy on long sessions, we still need to drain
            # cancels / sweeps / pending uploads / publish inflight on
            # the next tick. Blocking acquire here meant a cancel issued
            # while the bench was saturated could wait minutes before
            # being signaled.
            if not worker_slot.acquire(timeout=POLL_INTERVAL_S):
                continue
            try:
                status, body, headers = _get(f"{base}/plan",
                                             min_bps=_PLAN_MIN_BPS)
            except Exception:
                print(datetime.now(), "GET /plan failed")
                worker_slot.release()
                time.sleep(POLL_INTERVAL_S)
                continue

            if status == 204 or not body:
                worker_slot.release()
                time.sleep(POLL_INTERVAL_S)
                continue

            t = threading.Thread(target=_worker, args=(body, headers),
                                 daemon=True, name=f"job-{job_id_short(body)}")
            t.start()
    except KeyboardInterrupt:
        # Operator pressed Ctrl-C. Try to give in-flight workers a
        # graceful shot at finishing -- so cubeprog flashes etc.
        # don't get orphaned at PID 1 mid-write -- before the
        # daemon-thread reaper bites at interpreter exit.
        # The custom SIGINT handler installed at startup has already
        # promoted itself to "hard exit on any further SIGINT" so an
        # operator who mashes Ctrl-C because cleanup is taking too
        # long (USB close hung in C-level syscall, _drain_pending_
        # uploads waiting on a flaky tunnel) always has an escape.
        print(datetime.now(),
              "SIGINT: cancelling active sessions, waiting up to 30s "
              "(Ctrl-C again to force exit)")
        with _active_lock:
            for sess in list(_active_sessions.values()):
                try:
                    sess.signal_cancel()
                except Exception:
                    pass
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            with _active_lock:
                if not _active_sessions:
                    break
            time.sleep(0.1)
        with _active_lock:
            still = list(_active_sessions)
        if still:
            print(datetime.now(),
                  f"SIGINT: {len(still)} session(s) still running")
        # SIGKILL any subprocess plugins registered during the run
        # (cubeprog flash, ssh, etc.) so a Ctrl-C during a flash
        # doesn't orphan them at PID 1 still holding USB handles.
        with _subprocs_lock:
            zombies = list(_active_subprocs)
        if zombies:
            print(datetime.now(),
                  f"SIGINT: SIGKILLing {len(zombies)} child "
                  f"subprocess(es)")
            for p in zombies:
                try:
                    if p.poll() is None:
                        p.kill()
                except Exception:
                    pass
            # Wait briefly so the kernel actually reaps each child
            # before the interpreter exits -- without this, USB
            # handles held by the children can stay locked on
            # Windows (TerminateProcess + parent-exit) and the
            # children's pipe ends remain open in the parent's
            # daemon-thread .communicate() until process death.
            for p in zombies:
                try:
                    p.wait(timeout=2.0)
                except Exception:
                    pass
                # Close pipe ends so the kernel releases them.
                for stream in (p.stdout, p.stderr, p.stdin):
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:
                        pass
        # Try to drain any pending uploads one last time so a cancel
        # artefact lands on the server before we go.
        try:
            _drain_pending_uploads(timeout_s=5.0)
        except Exception:
            pass
    finally:
        registry.close_all()


def job_id_short(body):
    try:
        return hashlib.sha256(body).hexdigest()[:8]
    except Exception:
        return "????????"


if __name__ == "__main__":
    main()
