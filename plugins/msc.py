# SPDX-License-Identifier: MIT
# msc.py --- USB Mass Storage Class block-device writer
# Copyright (c) 2026 Jakob Kastelic

import glob
import os

import config
from plugin import DevicePlugin, Op


CHUNK_BYTES = 1 << 20


def _norm_hex(v):
    return f"{config.as_int(v):04x}"


def _resolve_block_device(vid, pid, serial=None):
    """Walk /sys/bus/usb to find the /dev/sdX backing a USB device with
    the given VID/PID (and optional iSerial). Returns ``None`` if no
    matching device is currently enumerated. Hot-plug aware -- callers
    re-probe to pick up a freshly attached drive.
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
            return "/dev/" + matches[0].rsplit("/", 1)[-1]
    return None


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
    offset = offset_lba * h.block_size
    _refuse_if_mounted(h.device)
    total = len(data)
    fd = os.open(h.device, os.O_WRONLY)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        written = 0
        while written < total:
            n = os.write(fd, data[written:written + CHUNK_BYTES])
            if n <= 0:
                raise IOError(f"write stalled at {written}/{total}")
            written += n
        os.fsync(fd)
    finally:
        os.close(fd)
    session.log_event(
        "MSC", "msc:write",
        f"wrote {total}B to {h.device} @ LBA {offset_lba}")


def _op_read(session, h, args):
    n = args["n"]
    offset_lba = args.get("offset_lba") or 0
    offset = offset_lba * h.block_size
    fd = os.open(h.device, os.O_RDONLY)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        got = bytearray()
        while len(got) < n:
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
        return
    mism = sum(1 for a, b in zip(expected, got) if a != b)
    first = next((i for i, (a, b) in enumerate(zip(expected, got)) if a != b),
                 -1)
    session.stream("msc.verify_mismatch").append(
        b"--MISMATCH--" + got[max(0, first - 64):first + 192])
    raise ValueError(
        f"msc verify mismatch: {mism}B differ, first at {first}")


class MscPlugin(DevicePlugin):
    name = "msc"
    doc = ("USB Mass Storage Class block-device writer. Probes for a "
           "configured VID/PID/iSerial, resolves the backing /dev/sdX, "
           "and writes or verifies blobs at LBA offsets.")

    ops = {
        "write": Op(
            args={"data": "blob"},
            optional_args={"offset_lba": "int"},
            doc=("Write a blob to the resolved block device. "
                 "offset_lba defaults to 0; units are block_size "
                 "(512 B for STM32MP1 baremetal MSC). Refuses if "
                 "any partition under the device is mounted."),
            run=_op_write),
        "read": Op(
            args={"n": "int"},
            optional_args={"offset_lba": "int"},
            doc=("Read n bytes from the resolved block device starting at "
                 "offset_lba (default 0); bytes go into stream msc.read "
                 "in the artefact tarball."),
            run=_op_read),
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
            device = _resolve_block_device(usb_vid, usb_pid, usb_serial)
            if device is None:
                continue
            out.append({
                "id": inst.get("id", "0"),
                "device": device,
                "block_size": int(inst.get("block_size", 512)),
                "usb_vid": usb_vid,
                "usb_pid": usb_pid,
                "usb_serial": usb_serial,
                "description": inst.get("description"),
            })
        return out

    def open(self, spec):
        device = spec["device"]
        # Re-resolve from sysfs: catches the case where the bootloader
        # was reset between probe and open and the kernel reassigned
        # /dev/sdX to a different physical device.
        actual = _resolve_block_device(
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
