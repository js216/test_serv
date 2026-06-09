# SPDX-License-Identifier: MIT
# _usb.py --- Shared USB enumeration helpers for plugin probe()s
# Copyright (c) 2026 Jakob Kastelic

import config
import json
import subprocess
import sys


FTD2XX_ENUM_TIMEOUT_S = 10.0


def _int(v):
    return config.as_int(v) if v is not None else None


def list_com_ports():
    """Return ``pyserial``'s list of COM ports, or ``[]`` if the module is
    unavailable. No side effects -- just enumerates what the OS sees.
    """
    try:
        import serial.tools.list_ports
    except ImportError:
        return []
    return list(serial.tools.list_ports.comports())


def find_com_by_vid_pid(vid, pid=None, pid_any=None, interface=None,
                        serial=None):
    """Return the first COM port matching the VID/PID (and optional
    interface number / iSerial), or ``None``.

    ``interface`` matches the USB interface number -- Windows encodes it
    in the hwid string as ``MI_0N``, Linux as ``:1.N`` in the location.

    ``serial`` matches ``ListPortInfo.serial_number``:
        a non-empty string -> exact-match the iSerial (use this to pin a
            specific programmed FTDI chip among siblings on the same VID/PID);
        the empty string ""  -> match only ports whose chip has NO programmed
            iSerial (the symmetric pin for the unprogrammed sibling);
        None / not given     -> no serial filter, first match wins.
    """
    vid_i = _int(vid)
    pid_i = _int(pid)
    pids_any = [_int(p) for p in (pid_any or [])]
    for p in list_com_ports():
        if vid_i is not None and p.vid != vid_i:
            continue
        if pid_i is not None and p.pid != pid_i:
            continue
        if pids_any and p.pid not in pids_any:
            continue
        if serial is not None:
            if serial == "":
                if p.serial_number:
                    continue
            elif p.serial_number != serial:
                continue
        if interface is not None:
            loc = (p.location or "") + " " + (p.hwid or "")
            n = int(interface)
            # Windows usbser.sys: MI_0N; Linux sysfs: :1.N;
            # Windows STLink VCP (some driver versions): :x.N.
            if not any(s in loc for s in
                       (f"MI_{n:02d}", f":1.{n}", f":x.{n}")):
                continue
        return p.device
    return None


def com_port_present(name):
    """Is this exact device name in the OS's current COM-port list?"""
    return any(p.device == name for p in list_com_ports())


def com_port_info(name):
    """Return the ``ListPortInfo`` for ``name``, or ``None``.

    Callers can then check ``.vid``, ``.pid``, ``.serial_number``,
    ``.manufacturer``, ``.location`` to verify the identity of the USB
    device backing a given COMxx slot.
    """
    for p in list_com_ports():
        if p.device == name:
            return p
    return None


def verify_com_identity(port, label, exp_vid=None, exp_pid=None,
                        exp_serial=None, exp_interface=None):
    """Look up the USB identity of the given COM port and assert it
    matches the expected values from config. Raises ``RuntimeError``
    on any mismatch (port not present, VID/PID/iSerial/interface wrong).
    Returns True if any expected field matched (so caller can record
    "_identity_verified"); returns False if nothing was pinned.

    Used by every UART-driven plugin so a Windows re-enumeration that
    moves a different USB-CDC device onto the configured COM number
    becomes a hard failure rather than silently driving the wrong
    chip.
    """
    info = com_port_info(port)
    if info is None:
        raise RuntimeError(f"{label}: {port} not in OS port list")
    verified = False
    if exp_vid is not None:
        v = config.as_int(exp_vid)
        if info.vid != v:
            raise RuntimeError(
                f"{label} USB VID mismatch on {port}: "
                f"expected 0x{v:04x}, got 0x{info.vid or 0:04x}")
        verified = True
    if exp_pid is not None:
        v = config.as_int(exp_pid)
        if info.pid != v:
            raise RuntimeError(
                f"{label} USB PID mismatch on {port}: "
                f"expected 0x{v:04x}, got 0x{info.pid or 0:04x}")
        verified = True
    if exp_serial is not None:
        got = info.serial_number or ""
        if exp_serial not in got:
            raise RuntimeError(
                f"{label} USB iSerial mismatch on {port}: "
                f"expected {exp_serial!r} substring, got {got!r}")
        verified = True
    if exp_interface is not None:
        loc = (info.location or "") + " " + (info.hwid or "")
        n = int(exp_interface)
        # Various driver stacks encode the USB interface number
        # differently in hwid / location strings:
        #   Windows usbser.sys:        MI_0N
        #   Linux sysfs-derived:       :1.N
        #   Some Windows STLink VCP:   :x.N   (config index is 'x')
        needles = (f"MI_{n:02d}", f":1.{n}", f":x.{n}")
        if not any(x in loc for x in needles):
            raise RuntimeError(
                f"{label} USB interface mismatch on {port}: "
                f"expected interface {n} (one of {needles}), got {loc!r}")
        verified = True
    return verified


def ftd2xx_descriptors():
    """Return the set of FTDI descriptor strings currently enumerated,
    or ``None`` if the ftd2xx module is unavailable (non-FTDI host).

    Raises ``RuntimeError`` on a transient enumeration failure so
    callers can retry or keep their previous device view. (Mapping
    that case to ``None``/``[]`` made a one-tick driver hiccup look
    like "all FTDI devices unplugged" and got specs evicted.)

    Note: D2XX cannot read the description string of a device some
    process currently holds open -- it reports a BLANK string instead.
    A returned set containing ``""`` is therefore evidence that a busy
    device is being hidden, not that the bench has a nameless chip.
    """
    try:
        import ftd2xx
    except ImportError:
        return None
    try:
        descs = ftd2xx.listDevices(2) or []
    except Exception as e:
        raise RuntimeError(f"ftd2xx enumeration failed: {e}") from e
    return {(d.decode(errors="replace") if isinstance(d, bytes) else d)
            for d in descs}


def ftd2xx_devices():
    """Return a list of ``{serial, description}`` dicts for every
    currently enumerated FTDI device, or ``None`` if the driver is
    unavailable. Used by plugins that need to disambiguate one of
    several physically-identical FTDI parts on the same bench --
    description alone (e.g. "Dual RS232-HS A") is shared across all
    FT2232H units, so an iSerial pin is the only way to be sure
    which one we're about to program.
    """
    code = r'''
import json
try:
    import ftd2xx
except ImportError:
    print("null")
    raise SystemExit(0)
try:
    n = ftd2xx.createDeviceInfoList()
except Exception:
    print("null")
    raise SystemExit(0)
out = []
for i in range(n):
    try:
        d = ftd2xx.getDeviceInfoDetail(i)
    except Exception:
        continue
    ser = d.get("serial")
    desc = d.get("description")
    if isinstance(ser, bytes):
        ser = ser.decode(errors="replace")
    if isinstance(desc, bytes):
        desc = desc.decode(errors="replace")
    out.append({"index": i, "serial": ser or "", "description": desc or ""})
print(json.dumps(out))
'''
    try:
        p = subprocess.run(
            [sys.executable, "-c", code],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=FTD2XX_ENUM_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except (TypeError, ValueError):
        return None


def winusb_device_present(vid, pid, serial=None):
    """Check whether a WinUSB / libusb device with the given VID/PID
    (and optional iSerial) is currently enumerated.

    Uses ``pyusb`` when available. Returns ``None`` (unknown) if the
    backend cannot enumerate -- callers should treat ``None`` as "might
    be there; let the operation try and fail fast". Returns ``True`` or
    ``False`` when enumeration succeeded.
    """
    vid_i = _int(vid)
    pid_i = _int(pid)
    try:
        import usb.core
    except ImportError:
        return None
    try:
        matches = list(usb.core.find(find_all=True,
                                     idVendor=vid_i, idProduct=pid_i))
    except Exception:
        return None
    if not matches:
        return False
    if serial is None:
        return True
    for dev in matches:
        try:
            if dev.serial_number == serial:
                return True
        except Exception:
            continue
    return False
