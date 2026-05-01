# SPDX-License-Identifier: MIT
# _usb.py --- Shared USB enumeration helpers for plugin probe()s
# Copyright (c) 2026 Jakob Kastelic

import config


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


def ftd2xx_descriptors():
    """Return the set of FTDI descriptor strings currently enumerated,
    or ``None`` if the driver is unavailable (non-FTDI host, etc.).
    """
    try:
        import ftd2xx
    except ImportError:
        return None
    try:
        descs = ftd2xx.listDevices(2) or []
    except Exception:
        return None
    return {(d.decode(errors="replace") if isinstance(d, bytes) else d)
            for d in descs}


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
