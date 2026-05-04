# SPDX-License-Identifier: MIT
# fpga.py --- iCEstick FPGA: flash-program via FT2232H MPSSE + UART drain
# Copyright (c) 2026 Jakob Kastelic

import sys
import threading
import time

import config
from plugin import DevicePlugin, Op
from . import _usb
from ._text import decode_escapes


FT2232H_DESC_A_DEFAULT = "Dual RS232-HS A"    # MPSSE channel
FT2232H_DESC_B_DEFAULT = "Dual RS232-HS B"    # UART channel
FPGA_BAUD_DEFAULT = 115200


def _lazy_ftd2xx():
    import ftd2xx
    return ftd2xx


def _lazy_serial():
    import serial
    import serial.tools.list_ports
    return serial


# --- iCEstick flash programmer (Micron N25Q032) ---

CLOCK_DIVISOR = 29          # 60 MHz / ((1+29)*2) = 1 MHz SPI
DIR_LOW = 0x93
IDLE_RESET = 0x10
IDLE_RUN = 0x90
ACTIVE_RESET = 0x00
PAGE = 256
SECTOR = 64 * 1024


def _mpsse_init(dev):
    # Read timeout 500 ms instead of 3 s -- MPSSE setup replies are
    # immediate (<1 ms) when the chip is healthy; if it doesn't
    # respond within 500 ms the wiring is wrong anyway. Caps the
    # blocking time of the dev.read(2) sync probe so a cancel
    # request observes within 500 ms.
    dev.setTimeouts(500, 3000)
    dev.setLatencyTimer(1)
    dev.resetDevice()
    dev.purge(3)
    dev.setBitMode(0, 0x00)
    time.sleep(0.02)
    dev.setBitMode(0, 0x02)
    time.sleep(0.02)
    dev.purge(3)
    dev.write(bytes([0xAA]))
    time.sleep(0.05)
    resp = dev.read(2)
    if resp != b"\xFA\xAA":
        raise RuntimeError(f"MPSSE sync failed: got {resp.hex()!r}")
    dev.write(bytes([
        0x85, 0x8A, 0x97, 0x8D,
        0x86, CLOCK_DIVISOR & 0xff, (CLOCK_DIVISOR >> 8) & 0xff,
    ]))
    time.sleep(0.01)


def _set_low(dev, value):
    dev.write(bytes([0x80, value, DIR_LOW]))


def _cmd(dev, out):
    n = len(out) - 1
    dev.write(
        bytes([0x80, ACTIVE_RESET, DIR_LOW,
               0x11, n & 0xff, (n >> 8) & 0xff])
        + bytes(out)
        + bytes([0x80, IDLE_RESET, DIR_LOW])
    )


MPSSE_READ_CHUNK = 1 << 16   # 65536, max MPSSE 0x20 payload per command


def _xfer(dev, out, rdlen, session=None):
    """SPI write `out`, then read `rdlen` bytes back, with CS held low
    across the whole exchange. Used by the SPI-NOR flash sequences
    (read 0x03, JEDEC 0x9F, status 0x05, etc.).

    Reads larger than 64 KiB are split into multiple 0x20 commands
    inside the same CS window -- the SPI flash keeps streaming as long
    as we keep clocking, so back-to-back read commands appear seamless
    to the chip. Each per-command read is also looped over libftd2xx
    `dev.read()` since FT_Read returns early on the chip's
    read-timeout with whatever it's buffered, not the full requested
    count. Without either of these, large reads short-read silently
    -- HX8K bitstreams (~135 KB) tripped both at once.
    """
    nw = len(out) - 1
    cmd = bytearray()
    cmd.extend([0x80, ACTIVE_RESET, DIR_LOW,
                0x11, nw & 0xff, (nw >> 8) & 0xff])
    cmd.extend(out)
    chunks = []
    remaining = rdlen
    while remaining > 0:
        n = min(remaining, MPSSE_READ_CHUNK)
        nr = n - 1
        cmd.extend([0x20, nr & 0xff, (nr >> 8) & 0xff])
        chunks.append(n)
        remaining -= n
    cmd.extend([0x80, IDLE_RESET, DIR_LOW, 0x87])
    dev.write(bytes(cmd))

    got = bytearray()
    for n in chunks:
        partial = bytearray()
        while len(partial) < n:
            if session is not None:
                session.bail_if_canceled(
                    f"fpga:_xfer @ {len(got)+len(partial)}/{rdlen}B")
            piece = dev.read(n - len(partial))
            if not piece:
                raise RuntimeError(
                    f"short read: {len(got) + len(partial)}/{rdlen}")
            partial.extend(piece)
        got.extend(partial)
    return bytes(got)


def _wait_wip(dev, session=None):
    while _xfer(dev, [0x05], 1, session=session)[0] & 0x01:
        if session is not None:
            if session.canceled:
                raise RuntimeError(
                    "fpga:_wait_wip canceled during flash status poll")
            session.cancel_event.wait(0.001)
        else:
            time.sleep(0.001)


def _erase(dev, nbytes, session=None):
    for addr in range(0, nbytes, SECTOR):
        session.bail_if_canceled(f"fpga:erase @ {addr}/{nbytes}B")
        _cmd(dev, [0x06])
        _cmd(dev, [0xD8,
                   (addr >> 16) & 0xff,
                   (addr >> 8) & 0xff,
                   addr & 0xff])
        _wait_wip(dev, session=session)


def _write(dev, buf, session=None):
    for off in range(0, len(buf), PAGE):
        session.bail_if_canceled(f"fpga:write @ {off}/{len(buf)}B")
        chunk = bytes(buf[off:off + PAGE])
        _cmd(dev, [0x06])
        _cmd(dev, [0x02,
                   (off >> 16) & 0xff,
                   (off >> 8) & 0xff,
                   off & 0xff] + list(chunk))
        _wait_wip(dev, session=session)


def _verify(dev, buf, session=None):
    session.bail_if_canceled("fpga:verify (read-back)")
    got = _xfer(dev, [0x03, 0, 0, 0], len(buf), session=session)
    if got == buf:
        return
    for i, (a, b) in enumerate(zip(buf, got)):
        if a != b:
            raise RuntimeError(
                f"verify mismatch at 0x{i:06x}: wrote 0x{a:02x}, got 0x{b:02x}"
            )


def _program_flash(bitstream, ft2232h_desc, session=None):
    ftd2xx = _lazy_ftd2xx()
    dev = ftd2xx.openEx(ft2232h_desc.encode() if isinstance(ft2232h_desc, str)
                        else ft2232h_desc, 2)
    try:
        _mpsse_init(dev)
        _set_low(dev, IDLE_RESET)
        time.sleep(0.01)
        _cmd(dev, [0xAB])
        time.sleep(0.001)
        ident = _xfer(dev, [0x9F], 3)
        if ident[0] != 0x20:
            raise RuntimeError(f"bad flash ID: {ident.hex()}")
        _erase(dev, len(bitstream), session=session)
        _write(dev, bitstream, session=session)
        _verify(dev, bitstream, session=session)
        _set_low(dev, IDLE_RUN)
    finally:
        dev.setBitMode(0, 0)
        dev.close()


def _find_icestick_uart(uart_desc, auto):
    """Locate the FTDI channel-B COM port the FPGA UART comes out on."""
    if sys.platform == "win32":
        try:
            ftd2xx = _lazy_ftd2xx()
            dev = ftd2xx.openEx(uart_desc.encode() if isinstance(uart_desc, str)
                                else uart_desc, 2)
        except Exception:
            return None
        try:
            num = dev.getComPortNumber()
        finally:
            dev.close()
        return f"COM{num}" if num > 0 else None
    if auto:
        return _usb.find_com_by_vid_pid(**auto)
    return None


# --- plugin handle ---

class FpgaHandle:
    def __init__(self, serial_port, baud, ft2232h_desc):
        self.serial_port = serial_port
        self.baud = baud
        self.ft2232h_desc = ft2232h_desc
        self._ser = None
        self._thread = None
        self._stop = None
        self._stream = None

    def uart_open(self, session):
        if self._ser is not None:
            return
        if self.serial_port is None:
            raise RuntimeError(
                "fpga:uart_open: no UART COM port resolved for the "
                "iCEstick.  Channel B (FT2232H 'Dual RS232-HS B') was "
                "not visible to the poller -- typically because some "
                "other process held it (PuTTY, a stale serial reader) "
                "or because uart_autodetect in config.json doesn't "
                "match the cable. Check `python3 -c \"import serial."
                "tools.list_ports as L; [print(p.device, p.vid, p.pid,"
                " p.location) for p in L.comports()]\"` and update "
                "fpga.uart_autodetect or set serial_port explicitly.")
        serial = _lazy_serial()
        self._ser = serial.Serial(
            self.serial_port, baudrate=self.baud, timeout=0.1,
            dsrdtr=False, rtscts=False, xonxoff=False,
        )
        try:
            self._ser.setDTR(False)
            self._ser.setRTS(False)
        except Exception:
            pass
        self._ser.reset_input_buffer()
        self._stream = session.stream("fpga.uart")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def uart_close(self):
        if self._ser is None:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            self._ser.close()
        except Exception:
            pass
        self._ser = None
        self._thread = None

    def uart_write(self, data):
        if self._ser is None:
            raise RuntimeError(
                "fpga uart not open (call fpga:uart_open first)")
        self._ser.write(data)
        self._ser.flush()

    def _drain(self):
        try:
            while not self._stop.is_set():
                data = self._ser.read(1024)
                if data:
                    self._stream.append(data)
            tail = self._ser.read(4096)
            if tail:
                self._stream.append(tail)
        except Exception:
            pass


# --- ops ---

def _op_program(session, h, args):
    _program_flash(bytes(args["bin"]), h.ft2232h_desc, session=session)


def _op_uart_open(session, h, args):
    h.uart_open(session)


def _op_uart_close(session, h, args):
    h.uart_close()


def _op_uart_write(session, h, args):
    h.uart_write(decode_escapes(args["data"]))


def _op_uart_expect(session, h, args):
    sentinel = decode_escapes(args["sentinel"])
    timeout_ms = args["timeout_ms"]
    end_session = bool(args.get("end_session"))
    stream = session.stream("fpga.uart")
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if session.canceled:
            return
        if sentinel in stream.snapshot_bytes():
            session.log_event("EXPECT", "fpga:uart_expect",
                              f"HIT {sentinel!r}")
            if end_session:
                session.signal_early_done(f"fpga.uart saw {sentinel!r}")
            return
        session.cancel_event.wait(0.01)
    raise TimeoutError(
        f"fpga.uart did not contain {sentinel!r} within {timeout_ms} ms")


class FpgaPlugin(DevicePlugin):
    name = "fpga"
    doc = "iCEstick FPGA: program SPI flash via FT2232H MPSSE + UART."

    ops = {
        "program": Op(args={"bin": "blob"},
                      doc="Erase, write, verify flash then release CRESET.",
                      run=_op_program),
        "uart_open": Op(args={}, doc="Start FPGA UART capture.",
                        run=_op_uart_open),
        "uart_close": Op(args={}, doc="Stop FPGA UART capture.",
                         run=_op_uart_close),
        "uart_write": Op(args={"data": "str"},
                         doc=("Send bytes to FPGA UART (escapes "
                              "decoded: \\r \\n \\t \\0 \\xNN). "
                              "Requires uart_open first."),
                         run=_op_uart_write),
        "uart_expect": Op(
            args={"sentinel": "str", "timeout_ms": "int"},
            optional_args={"end_session": "bool"},
            doc=("Block until sentinel in fpga.uart, then continue. "
                 "Pass end_session=true to short-circuit the plan."),
            run=_op_uart_expect),
    }

    def probe(self):
        descs = _usb.ftd2xx_descriptors()
        if descs is None:
            return []
        out = []
        for inst in config.instances(self.name):
            ft_desc = inst.get("ft2232h_desc") or FT2232H_DESC_A_DEFAULT
            if ft_desc not in descs:
                continue
            uart_desc = inst.get("ft2232h_uart_desc") or FT2232H_DESC_B_DEFAULT
            auto = inst.get("uart_autodetect") or {}
            uart_port = (inst.get("serial_port")
                         or _find_icestick_uart(uart_desc, auto))
            out.append({
                "id": inst.get("id", "0"),
                "ft2232h_desc": ft_desc,
                "serial_port": uart_port,
                "baudrate": int(inst.get("baudrate", FPGA_BAUD_DEFAULT)),
                "description": inst.get("description"),
            })
        return out

    def open(self, spec):
        return FpgaHandle(serial_port=spec.get("serial_port"),
                          baud=spec["baudrate"],
                          ft2232h_desc=spec["ft2232h_desc"])

    def close(self, handle):
        handle.uart_close()

    def cleanup_after_cancel(self, handle):
        # UART side: flush stale bytes left by an interrupted
        # uart_write or partial expect.
        ser = getattr(handle, "_ser", None)
        if ser is not None:
            try: ser.reset_input_buffer()
            except Exception: pass
            try: ser.reset_output_buffer()
            except Exception: pass
