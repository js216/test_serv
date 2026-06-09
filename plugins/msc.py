# SPDX-License-Identifier: MIT
# msc.py --- USB Mass Storage Class block-device writer
# Copyright (c) 2026 Jakob Kastelic
#
# Why the bare write/read/verify ops shell out to dd(1) (and cmp(1)):
#
# A previous all-Python implementation used os.read / os.write loops
# with bytearray accumulation. On a USB-MSC link that does ~7 MB/s with
# plain `dd`, the plugin crawled at ~0.3 MB/s. Two compounding causes:
#
#   1. The plugin materialized the entire image in Python memory more
#      than once -- bytearray growth during read, `bytes(got)` copy,
#      plus an msc.read stream record copy. For multi-GB SD card
#      images on a bench host with limited RAM this swap-thrashed and
#      cratered USB writeback.
#   2. Page-cache-only writes without fsync measured page-cache-fill
#      rate, not wire rate. Once an fsync was added to the timed
#      window, the reported rate dropped to actual wire rate -- which
#      *would* have matched `dd ... conv=fsync` if not for (1).
#
# The fix: stage to / from a local-disk temp file and let dd do the
# block-device I/O with a fixed C-level buffer. Memory footprint stays
# bounded regardless of image size, throughput tracks the link, and we
# get dd's own rate stats for free (parsed out of stderr into the
# msc.dd artefact stream and fed to min_rate_Bps).
#
# For verify we hand the byte compare to cmp(1) instead of zipping
# bytes in Python -- same reasoning, plus cmp short-circuits on the
# first differing byte and gives us the offset directly.
#
# The *_zeroes and *_prbs variants stay in-Python because they generate
# their pattern on the fly (no source blob to stage) and their
# bottleneck was the pattern generator / comparator, not memory
# pressure. Do NOT collapse the bare write/read/verify back into pure
# Python loops "for consistency" with those variants -- the swap-thrash
# regression returns immediately on any image larger than ~free RAM.

import glob
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

import config
from plugin import DevicePlugin, Op
from ._prbs import XorShift32Bytes
from ._progress import progress_line


CHUNK_BYTES = 1 << 20
DD_TIMEOUT_S = 6 * 60 * 60   # 6h cap so a wedged dd can't camp on the lock


def _check_min_rate(op_name, total, elapsed_s, min_rate_Bps):
    if not min_rate_Bps:
        return
    if elapsed_s <= 0:
        return
    rate = total / elapsed_s
    if rate < min_rate_Bps:
        raise TimeoutError(
            f"{op_name} too slow: {rate:.0f} B/s < {min_rate_Bps} B/s")


def _track_subproc(proc, session=None):
    """Mirror plugins/dfu.py: register a Popen so the poller's SIGINT
    AND session.signal_cancel can kill dd promptly.  Imported at first
    call so this module stays importable without poller.py.
    """
    try:
        from poller import register_subprocess
        register_subprocess(proc, session=session)
    except Exception:
        pass


def _untrack_subproc(proc):
    try:
        from poller import unregister_subprocess
        unregister_subprocess(proc)
    except Exception:
        pass


_DD_STATS_RE = re.compile(
    rb"(\d+)\s+bytes\b.*?copied,\s+([\d.eE+\-]+)\s*s,\s+"
    rb"([\d.eE+\-]+)\s*([kMGT]?)B/s")
_DD_UNIT_MULT = {b"": 1, b"k": 10**3, b"M": 10**6, b"G": 10**9, b"T": 10**12}


def _parse_dd_stats(stderr_bytes):
    """Extract ``(bytes_copied, rate_Bps)`` from dd's final stderr line.

    dd's last status line is unambiguous, e.g.::
        1048576000 bytes (1.0 GB, 1000 MiB) copied, 145.7 s, 7.2 MB/s
    We scan for the LAST occurrence so any intermediate progress-bar
    lines from ``status=progress`` don't shadow the final summary.
    Returns ``(None, None)`` if the line isn't present (e.g. dd was
    killed before printing).
    """
    last = None
    for m in _DD_STATS_RE.finditer(stderr_bytes or b""):
        last = m
    if last is None:
        return None, None
    n = int(last.group(1))
    rate_val = float(last.group(3))
    mult = _DD_UNIT_MULT.get(last.group(4), 1)
    return n, rate_val * mult


def _tmpdir():
    return os.environ.get(
        "TEST_SERV_MSC_TMPDIR", tempfile.gettempdir())


def _stage_blob(data, instance_id):
    """Write blob bytes to a uniquely named temp file; return the path.
    Caller is responsible for unlinking.  Splitting this out of the op
    keeps the dd-spawn helper free of bytes-bound code.
    """
    fd, path = tempfile.mkstemp(
        prefix=f"msc-{instance_id}-", suffix=".bin", dir=_tmpdir())
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except Exception:
        try: os.remove(path)
        except OSError: pass
        raise
    return path


def _new_tmp(instance_id, suffix=".bin"):
    fd, path = tempfile.mkstemp(
        prefix=f"msc-{instance_id}-", suffix=suffix, dir=_tmpdir())
    os.close(fd)
    return path


def _terminate(proc):
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _run_dd(session, argv, op_name, stream_name="msc.dd",
            total_bytes=None, label=None):
    """Run dd, stream stderr to ``stream_name``, honour session cancel,
    and return ``(bytes_done, rate_Bps, elapsed_s)``.

    ``bytes_done`` / ``rate_Bps`` come from dd's own final stats line so
    we report whatever dd actually achieved -- including the partial
    count on a mid-flight cancel.  Returns ``(None, None, elapsed)`` if
    dd died before printing the summary, in which case ``op_name`` is
    raised as IOError so the caller can surface it.

    stderr is drained by a thread as dd runs (rather than collected by
    communicate() at exit) so the once-a-second ``status=progress``
    lines can drive a dotted console progress line, matching the
    poller's GET/POST output for big transfers.  ``total_bytes`` sizes
    the line's header; pass None to disable.
    """
    dd = shutil.which("dd")
    if dd is None:
        raise RuntimeError(
            f"{op_name}: dd(1) not found on PATH; install coreutils")
    full_argv = [dd] + list(argv)
    t0 = time.monotonic()
    proc = subprocess.Popen(
        full_argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _track_subproc(proc, session=session)
    stderr_buf = bytearray()
    progress = progress_line(label or op_name, total_bytes)

    def _drain_stderr():
        fd = proc.stderr.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            stderr_buf.extend(chunk)
            # status=progress lines carry a cumulative byte count;
            # scan a trailing window (lines are short; a count split
            # across chunks parses low once and heals on the next
            # line, since advance_to never moves backward).
            last = None
            for last in _DD_STATS_RE.finditer(bytes(stderr_buf[-4096:])):
                pass
            if last is not None:
                progress.advance_to(int(last.group(1)))

    reader = threading.Thread(target=_drain_stderr, daemon=True)
    reader.start()
    canceled = False
    try:
        deadline = t0 + DD_TIMEOUT_S
        while proc.poll() is None:
            if session.canceled:
                canceled = True
                _terminate(proc)
                break
            if time.monotonic() > deadline:
                _terminate(proc)
                raise TimeoutError(
                    f"{op_name}: dd exceeded {DD_TIMEOUT_S}s wall clock")
            time.sleep(0.2)
    finally:
        _untrack_subproc(proc)
        reader.join(timeout=5)
        progress.done()
    elapsed = time.monotonic() - t0
    stderr = bytes(stderr_buf)
    session.stream(stream_name).append(stderr or b"")
    if canceled:
        raise RuntimeError(
            f"{op_name}: dd canceled via DELETE /jobs/<digest>")
    if proc.returncode != 0:
        raise IOError(
            f"{op_name}: dd exit={proc.returncode}; see {stream_name} stream")
    bytes_done, rate_Bps = _parse_dd_stats(stderr or b"")
    return bytes_done, rate_Bps, elapsed


def _norm_hex(v):
    return f"{config.as_int(v):04x}"


def _resolve_block_device(vid, pid, serial=None):
    """Walk /sys/bus/usb to find the /dev/sdX backing a USB device with
    the given VID/PID (and optional iSerial). Returns ``(device, speed)``
    where ``device`` is the /dev path and ``speed`` is the host-negotiated
    rate in Mbps (12=FS, 480=HS, 5000=SS) or ``None`` if unreadable.
    Returns ``(None, None)`` if no matching device is currently
    enumerated. Hot-plug aware -- callers re-probe to pick up a freshly
    attached drive.
    """
    target_vid = _norm_hex(vid)
    target_pid = _norm_hex(pid)
    for usb_path in glob.glob("/sys/bus/usb/devices/*"):
        leaf = usb_path.rsplit("/", 1)[-1]
        # interface dirs ("1-1.1:1.0") have no idVendor; skip without
        # paying for a failed open.
        if ":" in leaf:
            continue
        try:
            with open(f"{usb_path}/idVendor") as f:
                v = f.read().strip().lower()
            with open(f"{usb_path}/idProduct") as f:
                p = f.read().strip().lower()
        except OSError:
            continue
        if v != target_vid or p != target_pid:
            continue
        if serial:
            try:
                with open(f"{usb_path}/serial") as f:
                    s = f.read().strip()
            except OSError:
                s = ""
            if s != serial:
                continue
        matches = sorted(glob.glob(
            f"{usb_path}/*:*/host*/target*/*/block/sd?"))
        if matches:
            speed = None
            try:
                with open(f"{usb_path}/speed") as f:
                    speed = int(f.read().strip())
            except (OSError, ValueError):
                pass
            return ("/dev/" + matches[0].rsplit("/", 1)[-1], speed)
    return (None, None)


def _refuse_if_mounted(device):
    """Raise if ``device`` itself or any of its partitions
    (``/dev/sdaN``) is currently mounted. Cheap O(N_mounts) lookup of
    ``/proc/mounts``; runs before every write to keep the bench tech
    from nuking a teammate's USB stick.
    """
    try:
        with open("/proc/mounts") as f:
            mounts = [line.split()[0] for line in f]
    except OSError:
        return
    for m in mounts:
        if m == device:
            raise RuntimeError(f"refusing to write {device}: it is mounted")
        if m.startswith(device) and m[len(device):].isdigit():
            raise RuntimeError(
                f"refusing to write {device}: partition {m} is mounted")


class MscHandle:
    def __init__(self, device, block_size, instance_id="0"):
        self.device = device
        self.block_size = block_size
        self.instance_id = instance_id


def _op_write(session, h, args):
    data = args["data"]
    offset_lba = args.get("offset_lba") or 0
    min_rate_Bps = args.get("min_rate_Bps")
    offset_bytes = offset_lba * h.block_size
    _refuse_if_mounted(h.device)
    total = len(data)

    # Stage the blob to local disk so dd can stream it with a fixed
    # C-level buffer instead of us materializing every chunk in Python.
    # Without this the page cache plus 2x in-process copies of a multi-
    # GB SD image swap-thrash the bench machine and crater USB
    # writeback -- we measured ~0.3 MB/s on a link that does 7+ MB/s.
    src = _stage_blob(data, h.instance_id)
    try:
        argv = [f"if={src}", f"of={h.device}",
                "bs=1M", "conv=fsync,notrunc",
                "oflag=seek_bytes", f"seek={offset_bytes}",
                "status=progress"]
        bytes_w, rate_Bps, elapsed = _run_dd(
            session, argv, "msc:write",
            total_bytes=total, label=f"msc:write {h.device}")
    finally:
        try: os.remove(src)
        except OSError: pass

    actual = bytes_w if bytes_w is not None else total
    rate_note = (f" ({rate_Bps/1e6:.2f} MB/s via dd)"
                 if rate_Bps is not None else " via dd")
    session.log_event(
        "MSC", "msc:write",
        f"wrote {actual}B to {h.device} @ LBA {offset_lba}{rate_note}")
    _check_min_rate("msc:write", actual, elapsed, min_rate_Bps)


def _write_generated(session, h, *, n, offset_lba, min_rate_Bps,
                     op_name, make_chunk):
    offset = offset_lba * h.block_size
    _refuse_if_mounted(h.device)
    total = int(n)
    if total < 0:
        raise ValueError(f"{op_name}: n must be >= 0")
    t0 = time.monotonic()
    progress = progress_line(f"{op_name} {h.device}", total)
    fd = os.open(h.device, os.O_WRONLY)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        written = 0
        while written < total:
            session.bail_if_canceled(
                f"{op_name} @ {written}/{total}B")
            want = min(CHUNK_BYTES, total - written)
            chunk = make_chunk(written, want)
            session.bail_if_canceled(
                f"{op_name} before write @ {written}/{total}B")
            pos = 0
            while pos < len(chunk):
                session.bail_if_canceled(
                    f"{op_name} partial write @ {written}/{total}B")
                nwr = os.write(fd, chunk[pos:])
                if nwr <= 0:
                    raise IOError(f"{op_name} stalled at {written}/{total}")
                pos += nwr
                written += nwr
                progress.advance(nwr)
        if not session.canceled:
            os.fsync(fd)
    finally:
        os.close(fd)
        progress.done()
    session.log_event(
        "MSC", op_name,
        f"wrote {total}B to {h.device} @ LBA {offset_lba}")
    _check_min_rate(op_name, total, time.monotonic() - t0, min_rate_Bps)


def _op_write_zeroes(session, h, args):
    n = args["n"]
    offset_lba = args.get("offset_lba") or 0
    min_rate_Bps = args.get("min_rate_Bps")
    zero = b"\0" * CHUNK_BYTES
    _write_generated(
        session, h, n=n, offset_lba=offset_lba,
        min_rate_Bps=min_rate_Bps, op_name="msc:write_zeroes",
        make_chunk=lambda _off, want: zero[:want])


def _op_write_prbs(session, h, args):
    n = args["n"]
    offset_lba = args.get("offset_lba") or 0
    min_rate_Bps = args.get("min_rate_Bps")
    _write_generated(
        session, h, n=n, offset_lba=offset_lba,
        min_rate_Bps=min_rate_Bps, op_name="msc:write_prbs",
        make_chunk=_prbs_chunker(args["seed"]))


def _prbs_chunker(seed):
    prbs = XorShift32Bytes(int(seed))
    return lambda _off, want: prbs.read(want)


def _verify_generated(session, h, *, n, offset_lba, min_rate_Bps,
                      op_name, make_chunk):
    offset = offset_lba * h.block_size
    total = int(n)
    if total < 0:
        raise ValueError(f"{op_name}: n must be >= 0")
    t0 = time.monotonic()
    mism = 0
    first = -1
    first_window = b""
    progress = progress_line(f"{op_name} {h.device}", total)
    fd = os.open(h.device, os.O_RDONLY)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        read = 0
        while read < total:
            session.bail_if_canceled(f"{op_name} @ {read}/{total}B")
            want = min(CHUNK_BYTES, total - read)
            expected = make_chunk(read, want)
            got = bytearray()
            while len(got) < want:
                session.bail_if_canceled(
                    f"{op_name} read @ {read + len(got)}/{total}B")
                chunk = os.read(fd, want - len(got))
                if not chunk:
                    raise IOError(f"short read at {read + len(got)}/{total}")
                got += chunk
                progress.advance(len(chunk))
            got = bytes(got)
            for i, (a, b) in enumerate(zip(expected, got)):
                if a == b:
                    continue
                mism += 1
                if first < 0:
                    first = read + i
                    first_window = got[max(0, i - 64):i + 192]
            read += want
    finally:
        os.close(fd)
        progress.done()
    claim = f"{total}B generated pattern match at LBA {offset_lba}"
    if mism == 0:
        session.log_event("MSC", op_name, f"OK {total}B @ LBA {offset_lba}")
        session.record_check(
            op_name.replace("msc:", "msc_"), h.device, claim, "hit",
            {"bytes": total, "offset_lba": offset_lba})
        _check_min_rate(op_name, total, time.monotonic() - t0, min_rate_Bps)
        return
    session.stream("msc.verify_mismatch").append(
        b"--MISMATCH--" + first_window)
    session.record_check(
        op_name.replace("msc:", "msc_"), h.device, claim, "miss",
        {"bytes": total, "offset_lba": offset_lba,
         "mismatched": mism, "first_diff": first})
    raise ValueError(
        f"{op_name} mismatch: {mism}B differ, first at {first}")


def _op_verify_zeroes(session, h, args):
    n = args["n"]
    offset_lba = args.get("offset_lba") or 0
    min_rate_Bps = args.get("min_rate_Bps")
    zero = b"\0" * CHUNK_BYTES
    _verify_generated(
        session, h, n=n, offset_lba=offset_lba,
        min_rate_Bps=min_rate_Bps, op_name="msc:verify_zeroes",
        make_chunk=lambda _off, want: zero[:want])


def _op_verify_prbs(session, h, args):
    n = args["n"]
    offset_lba = args.get("offset_lba") or 0
    min_rate_Bps = args.get("min_rate_Bps")
    _verify_generated(
        session, h, n=n, offset_lba=offset_lba,
        min_rate_Bps=min_rate_Bps, op_name="msc:verify_prbs",
        make_chunk=_prbs_chunker(args["seed"]))


def _op_read(session, h, args):
    n = int(args["n"])
    offset_lba = args.get("offset_lba") or 0
    min_rate_Bps = args.get("min_rate_Bps")
    offset_bytes = offset_lba * h.block_size

    dst = _new_tmp(h.instance_id)
    try:
        argv = [f"if={h.device}", f"of={dst}",
                "bs=1M",
                "iflag=skip_bytes,count_bytes",
                f"skip={offset_bytes}", f"count={n}",
                "status=progress"]
        bytes_r, rate_Bps, elapsed = _run_dd(
            session, argv, "msc:read",
            total_bytes=n, label=f"msc:read {h.device}")

        # Stream the captured bytes into the msc.read artefact stream
        # chunk-by-chunk so a multi-GB read doesn't materialize the full
        # payload in Python memory.  The stream cap (STREAM_MAX_BYTES,
        # 64 MiB) still evicts oldest records for oversized reads, but
        # the bench RAM footprint stays bounded.
        stream = session.stream("msc.read")
        with open(dst, "rb") as f:
            while True:
                session.bail_if_canceled(
                    f"msc:read drain @ {f.tell()}/{n}B")
                chunk = f.read(CHUNK_BYTES)
                if not chunk:
                    break
                stream.append(chunk)
    finally:
        try: os.remove(dst)
        except OSError: pass

    actual = bytes_r if bytes_r is not None else n
    rate_note = (f" ({rate_Bps/1e6:.2f} MB/s via dd)"
                 if rate_Bps is not None else " via dd")
    session.log_event(
        "MSC", "msc:read",
        f"read {actual}B from {h.device} @ LBA {offset_lba}{rate_note}")
    _check_min_rate("msc:read", actual, elapsed, min_rate_Bps)


def _op_verify(session, h, args):
    expected = args["data"]
    offset_lba = args.get("offset_lba") or 0
    offset_bytes = offset_lba * h.block_size
    total = len(expected)
    min_rate_Bps = args.get("min_rate_Bps")

    exp_path = _stage_blob(expected, h.instance_id)
    got_path = _new_tmp(h.instance_id)
    try:
        argv = [f"if={h.device}", f"of={got_path}",
                "bs=1M",
                "iflag=skip_bytes,count_bytes",
                f"skip={offset_bytes}", f"count={total}",
                "status=progress"]
        bytes_r, rate_Bps, elapsed = _run_dd(
            session, argv, "msc:verify",
            total_bytes=total, label=f"msc:verify {h.device}")
        _check_min_rate(
            "msc:verify", bytes_r if bytes_r is not None else total,
            elapsed, min_rate_Bps)

        # Hand the byte-for-byte compare to cmp(1): C-level memcmp,
        # streaming, never materializes either file in our process.
        # cmp exits 0 on identical, 1 on diff, 2 on error.
        cmp_bin = shutil.which("cmp")
        if cmp_bin is None:
            raise RuntimeError(
                "msc:verify: cmp(1) not found on PATH; install diffutils")
        cmp_proc = subprocess.run(
            [cmp_bin, "--", exp_path, got_path],
            capture_output=True, timeout=DD_TIMEOUT_S)
        if cmp_proc.returncode == 0:
            session.log_event(
                "MSC", "msc:verify",
                f"OK {total}B @ LBA {offset_lba} via dd+cmp"
                + (f" ({rate_Bps/1e6:.2f} MB/s)"
                   if rate_Bps is not None else ""))
            session.record_check(
                "msc_verify", h.device,
                f"{total}B match at LBA {offset_lba}",
                "hit",
                {"bytes": total, "offset_lba": offset_lba})
            return
        if cmp_proc.returncode != 1:
            raise IOError(
                f"msc:verify: cmp exit={cmp_proc.returncode}: "
                f"{(cmp_proc.stderr or b'').decode(errors='replace')!r}")

        # cmp's stdout on mismatch: "exp got differ: byte 12345, line 678"
        # 1-indexed byte position; convert to 0-indexed.
        m = re.search(rb"byte\s+(\d+)", cmp_proc.stdout or b"")
        first = int(m.group(1)) - 1 if m else -1
        with open(got_path, "rb") as f:
            if first >= 0:
                f.seek(max(0, first - 64))
            window = f.read(256)
        session.stream("msc.verify_mismatch").append(
            b"--MISMATCH--" + window)
        session.record_check(
            "msc_verify", h.device,
            f"{total}B match at LBA {offset_lba}",
            "miss",
            {"bytes": total, "offset_lba": offset_lba,
             "first_diff": first})
        raise ValueError(
            f"msc verify mismatch: first differing byte at {first}")
    finally:
        for p in (exp_path, got_path):
            try: os.remove(p)
            except OSError: pass


class MscPlugin(DevicePlugin):
    name = "msc"
    doc = ("USB Mass Storage Class block-device I/O. Probes for a "
           "configured VID/PID/iSerial, resolves the backing /dev/sdX, "
           "and exposes write / read / verify ops at arbitrary LBA "
           "offsets. Refuses any write if a partition under the device "
           "is currently mounted, so a stray host-side automount can't "
           "let the bench corrupt the agent's data.  write / read / "
           "verify shell out to dd(1) (and cmp(1) for verify) so a "
           "multi-GB SD image streams through a fixed C-level buffer "
           "instead of materializing in Python memory; dd's stats are "
           "captured in the msc.dd stream and feed min_rate_Bps.  The "
           "*_zeroes and *_prbs variants stay in-process since they "
           "generate their pattern on the fly.")

    ops = {
        "write": Op(
            args={"data": "blob"},
            optional_args={"offset_lba": "int", "min_rate_Bps": "int"},
            doc=("Write a blob to the resolved block device via dd(1) "
                 "(bs=1M conv=fsync,notrunc).  offset_lba defaults to 0; "
                 "units are block_size (512 B for STM32MP1 baremetal "
                 "MSC).  Refuses if any partition under the device is "
                 "mounted.  min_rate_Bps fails the op if dd's reported "
                 "wire rate falls below the requested byte/s floor.  "
                 "dd's raw stderr (progress + final stats line) is "
                 "captured in the msc.dd stream alongside the artefact "
                 "-- inspect it to see the actual wire rate dd "
                 "measured."),
            run=_op_write),
        "read": Op(
            args={"n": "int"},
            optional_args={"offset_lba": "int", "min_rate_Bps": "int"},
            doc=("Read n bytes from the resolved block device via dd(1) "
                 "starting at offset_lba (default 0); bytes go into "
                 "stream msc.read in the artefact tarball, streamed "
                 "chunk-by-chunk so a multi-GB read does not "
                 "materialize in plugin memory.  The msc.read stream "
                 "is capped at STREAM_MAX_BYTES; reads larger than "
                 "that get truncated to the most recent records.  "
                 "min_rate_Bps fails the op if dd's reported wire rate "
                 "falls below the requested byte/s floor.  dd's raw "
                 "stderr (progress + final stats line) is captured in "
                 "the msc.dd stream alongside the artefact -- inspect "
                 "it to see the actual wire rate dd measured."),
            run=_op_read),
        "write_zeroes": Op(
            args={"n": "int"},
            optional_args={"offset_lba": "int", "min_rate_Bps": "int"},
            doc=("Write n zero bytes to the resolved block device. "
                 "offset_lba defaults to 0; units are block_size "
                 "(512 B for STM32MP1 baremetal MSC). Refuses if any "
                 "partition under the device is mounted. min_rate_Bps "
                 "fails the op if effective write rate falls below the "
                 "requested byte/s floor."),
            run=_op_write_zeroes),
        "write_prbs": Op(
            args={"seed": "int", "n": "int"},
            optional_args={"offset_lba": "int", "min_rate_Bps": "int"},
            doc=("Write n bytes of deterministic xorshift32 PRBS to the "
                 "resolved block device. Reproduce/verify the same "
                 "pattern with verify_prbs using identical seed, n, and "
                 "offset_lba. offset_lba defaults to 0; min_rate_Bps "
                 "fails the op if effective write rate falls below the "
                 "requested byte/s floor."),
            run=_op_write_prbs),
        "verify_zeroes": Op(
            args={"n": "int"},
            optional_args={"offset_lba": "int", "min_rate_Bps": "int"},
            doc=("Read n bytes from the resolved block device and verify "
                 "they are all zero. offset_lba defaults to 0; "
                 "min_rate_Bps fails the op if effective read rate falls "
                 "below the requested byte/s floor. Records a "
                 "machine-checkable msc_verify_zeroes check."),
            run=_op_verify_zeroes),
        "verify_prbs": Op(
            args={"seed": "int", "n": "int"},
            optional_args={"offset_lba": "int", "min_rate_Bps": "int"},
            doc=("Read n bytes from the resolved block device and verify "
                 "they match the deterministic xorshift32 PRBS produced "
                 "by write_prbs with identical seed, n, and offset_lba. "
                 "offset_lba defaults to 0; min_rate_Bps fails the op if "
                 "effective read rate falls below the requested byte/s "
                 "floor. Records a machine-checkable "
                 "msc_verify_prbs check."),
            run=_op_verify_prbs),
        "verify": Op(
            args={"data": "blob"},
            optional_args={"offset_lba": "int", "min_rate_Bps": "int"},
            doc=("Read len(data) bytes from the resolved block device "
                 "via dd(1) starting at offset_lba and compare byte-"
                 "for-byte using cmp(1).  Streams a 256-byte window "
                 "around the first mismatch into msc.verify_mismatch "
                 "on failure.  min_rate_Bps applies to the dd read "
                 "portion only (cmp is local-disk and cheap).  dd's "
                 "raw stderr (progress + final stats line) is captured "
                 "in the msc.dd stream alongside the artefact -- "
                 "inspect it to see the actual wire rate dd measured."),
            run=_op_verify),
    }

    def probe(self):
        out = []
        for inst in config.instances(self.name):
            usb_vid = inst.get("usb_vid")
            usb_pid = inst.get("usb_pid")
            usb_serial = inst.get("usb_serial")
            if not (usb_vid and usb_pid):
                continue
            device, speed_mbps = _resolve_block_device(
                usb_vid, usb_pid, usb_serial)
            if device is None:
                continue
            out.append({
                "id": inst.get("id", "0"),
                "device": device,
                "block_size": int(inst.get("block_size", 512)),
                "usb_vid": usb_vid,
                "usb_pid": usb_pid,
                "usb_serial": usb_serial,
                "usb_speed_mbps": speed_mbps,
                "description": inst.get("description"),
            })
        return out

    def open(self, spec):
        device = spec["device"]
        # Re-resolve from sysfs: catches the case where the bootloader
        # was reset between probe and open and the kernel reassigned
        # /dev/sdX to a different physical device.
        actual, _ = _resolve_block_device(
            spec["usb_vid"], spec["usb_pid"], spec.get("usb_serial"))
        if actual != device:
            raise RuntimeError(
                f"msc: device path drifted ({device!r} -> {actual!r}); "
                f"replug or re-probe")
        _refuse_if_mounted(device)
        h = MscHandle(device=device, block_size=spec["block_size"],
                      instance_id=spec.get("id", "0"))
        h._identity_verified = True
        return h

    def close(self, handle):
        pass
