# SPDX-License-Identifier: MIT
# msc.py --- USB Mass Storage Class block-device writer
# Copyright (c) 2026 Jakob Kastelic

import glob
import os
import time

import config
from plugin import DevicePlugin, Op
from ._prbs import XorShift32Bytes


CHUNK_BYTES = 1 << 20


def _check_min_rate(op_name, total, elapsed_s, min_rate_Bps):
    if not min_rate_Bps:
        return
    if elapsed_s <= 0:
        return
    rate = total / elapsed_s
    if rate < min_rate_Bps:
        raise TimeoutError(
            f"{op_name} too slow: {rate:.0f} B/s < {min_rate_Bps} B/s")


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
    def __init__(self, device, block_size):
        self.device = device
        self.block_size = block_size


def _op_write(session, h, args):
    data = bytes(args["data"])
    offset_lba = args.get("offset_lba") or 0
    min_rate_Bps = args.get("min_rate_Bps")
    offset = offset_lba * h.block_size
    _refuse_if_mounted(h.device)
    total = len(data)
    t0 = time.monotonic()
    fd = os.open(h.device, os.O_WRONLY)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        written = 0
        while written < total:
            session.bail_if_canceled(f"msc:write @ {written}/{total}B")
            n = os.write(fd, data[written:written + CHUNK_BYTES])
            if n <= 0:
                raise IOError(f"write stalled at {written}/{total}")
            written += n
        # Skip fsync on cancel: cancel observed AT the bottom of the
        # loop means the write completed but the agent already wants
        # out -- making them wait several seconds for the kernel to
        # flush a 100 MB buffer to USB just to abandon the result is
        # the wrong tradeoff. The kernel will flush in the background;
        # the next msc op opens the device fresh and sees current state.
        if not session.canceled:
            os.fsync(fd)
    finally:
        os.close(fd)
    session.log_event(
        "MSC", "msc:write",
        f"wrote {total}B to {h.device} @ LBA {offset_lba}")
    _check_min_rate("msc:write", total, time.monotonic() - t0,
                    min_rate_Bps)


def _write_generated(session, h, *, n, offset_lba, min_rate_Bps,
                     op_name, make_chunk):
    offset = offset_lba * h.block_size
    _refuse_if_mounted(h.device)
    total = int(n)
    if total < 0:
        raise ValueError(f"{op_name}: n must be >= 0")
    t0 = time.monotonic()
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
        if not session.canceled:
            os.fsync(fd)
    finally:
        os.close(fd)
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
    n = args["n"]
    offset_lba = args.get("offset_lba") or 0
    min_rate_Bps = args.get("min_rate_Bps")
    offset = offset_lba * h.block_size
    t0 = time.monotonic()
    fd = os.open(h.device, os.O_RDONLY)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        got = bytearray()
        while len(got) < n:
            session.bail_if_canceled(f"msc:read @ {len(got)}/{n}B")
            chunk = os.read(fd, min(CHUNK_BYTES, n - len(got)))
            if not chunk:
                raise IOError(f"short read at {len(got)}/{n}")
            got += chunk
    finally:
        os.close(fd)
    session.stream("msc.read").append(bytes(got))
    session.log_event(
        "MSC", "msc:read",
        f"read {n}B from {h.device} @ LBA {offset_lba}")
    _check_min_rate("msc:read", n, time.monotonic() - t0, min_rate_Bps)


def _op_verify(session, h, args):
    expected = bytes(args["data"])
    offset_lba = args.get("offset_lba") or 0
    offset = offset_lba * h.block_size
    total = len(expected)
    fd = os.open(h.device, os.O_RDONLY)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        got = bytearray()
        while len(got) < total:
            session.bail_if_canceled(f"msc:verify @ {len(got)}/{total}B")
            chunk = os.read(fd, min(CHUNK_BYTES, total - len(got)))
            if not chunk:
                raise IOError(f"short read at {len(got)}/{total}")
            got += chunk
    finally:
        os.close(fd)
    got = bytes(got[:total])
    if got == expected:
        session.log_event("MSC", "msc:verify",
                          f"OK {total}B @ LBA {offset_lba}")
        # Record machine-checkable pass so manifest.status doesn't
        # land as "inert" for a verify-only plan.
        session.record_check(
            "msc_verify", h.device,
            f"{total}B match at LBA {offset_lba}",
            "hit",
            {"bytes": total, "offset_lba": offset_lba})
        return
    mism = sum(1 for a, b in zip(expected, got) if a != b)
    first = next((i for i, (a, b) in enumerate(zip(expected, got)) if a != b),
                 -1)
    session.stream("msc.verify_mismatch").append(
        b"--MISMATCH--" + got[max(0, first - 64):first + 192])
    session.record_check(
        "msc_verify", h.device,
        f"{total}B match at LBA {offset_lba}",
        "miss",
        {"bytes": total, "offset_lba": offset_lba,
         "mismatched": mism, "first_diff": first})
    raise ValueError(
        f"msc verify mismatch: {mism}B differ, first at {first}")


class MscPlugin(DevicePlugin):
    name = "msc"
    doc = ("USB Mass Storage Class block-device I/O. Probes for a "
           "configured VID/PID/iSerial, resolves the backing /dev/sdX, "
           "and exposes write / read / verify ops at arbitrary LBA "
           "offsets. Refuses any write if a partition under the device "
           "is currently mounted, so a stray host-side automount can't "
           "let the bench corrupt the agent's data.")

    ops = {
        "write": Op(
            args={"data": "blob"},
            optional_args={"offset_lba": "int", "min_rate_Bps": "int"},
            doc=("Write a blob to the resolved block device. "
                 "offset_lba defaults to 0; units are block_size "
                 "(512 B for STM32MP1 baremetal MSC). Refuses if "
                 "any partition under the device is mounted. "
                 "min_rate_Bps fails the op if effective write rate "
                 "falls below the requested byte/s floor."),
            run=_op_write),
        "read": Op(
            args={"n": "int"},
            optional_args={"offset_lba": "int", "min_rate_Bps": "int"},
            doc=("Read n bytes from the resolved block device starting at "
                 "offset_lba (default 0); bytes go into stream msc.read "
                 "in the artefact tarball. min_rate_Bps fails the op if "
                 "effective read rate falls below the requested byte/s "
                 "floor."),
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
            optional_args={"offset_lba": "int"},
            doc=("Read len(data) bytes from the resolved block device "
                 "starting at offset_lba and compare byte-for-byte. "
                 "Streams a window around the first mismatch into "
                 "msc.verify_mismatch on failure."),
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
        h = MscHandle(device=device, block_size=spec["block_size"])
        h._identity_verified = True
        return h

    def close(self, handle):
        pass
