# SPDX-License-Identifier: MIT
# fpga.py --- iCEstick FPGA: flash-program via FT2232H MPSSE + UART drain
# Copyright (c) 2026 Jakob Kastelic

import sys
import threading
import time

import config
from plugin import DevicePlugin, Op
from . import _usb


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
    dev.setTimeouts(3000, 3000)
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


def _xfer(dev, out, rdlen):
    nw = len(out) - 1
    nr = rdlen - 1
    dev.write(
        bytes([0x80, ACTIVE_RESET, DIR_LOW,
               0x11, nw & 0xff, (nw >> 8) & 0xff])
        + bytes(out)
        + bytes([0x20, nr & 0xff, (nr >> 8) & 0xff,
                 0x80, IDLE_RESET, DIR_LOW,
                 0x87])
    )
    got = dev.read(rdlen)
    if len(got) != rdlen:
        raise RuntimeError(f"short read: {len(got)}/{rdlen}")
    return got


def _wait_wip(dev):
    while _xfer(dev, [0x05], 1)[0] & 0x01:
        time.sleep(0.001)


def _erase(dev, nbytes):
    for addr in range(0, nbytes, SECTOR):
        _cmd(dev, [0x06])
        _cmd(dev, [0xD8,
                   (addr >> 16) & 0xff,
                   (addr >> 8) & 0xff,
                   addr & 0xff])
        _wait_wip(dev)


def _write(dev, buf):
    for off in range(0, len(buf), PAGE):
        chunk = bytes(buf[off:off + PAGE])
        _cmd(dev, [0x06])
        _cmd(dev, [0x02,
                   (off >> 16) & 0xff,
                   (off >> 8) & 0xff,
                   off & 0xff] + list(chunk))
        _wait_wip(dev)


def _verify(dev, buf):
    got = _xfer(dev, [0x03, 0, 0, 0], len(buf))
    if got == buf:
        return
    for i, (a, b) in enumerate(zip(buf, got)):
        if a != b:
            raise RuntimeError(
                f"verify mismatch at 0x{i:06x}: wrote 0x{a:02x}, got 0x{b:02x}"
            )


def _program_flash(bitstream, ft2232h_desc):
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
        _erase(dev, len(bitstream))
        _write(dev, bitstream)
        _verify(dev, bitstream)
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
        if self._ser is None and self.serial_port is not None:
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
    _program_flash(bytes(args["bin"]), h.ft2232h_desc)


def _op_uart_open(session, h, args):
    h.uart_open(session)


def _op_uart_close(session, h, args):
    h.uart_close()


def _op_uart_expect(session, h, args):
    sentinel = args["sentinel"].encode("utf-8")
    timeout_ms = args["timeout_ms"]
    stream = session.stream("fpga.uart")
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if sentinel in stream.snapshot_bytes():
            session.signal_early_done(f"fpga.uart saw {sentinel!r}")
            return
        time.sleep(0.01)
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
        "uart_expect": Op(args={"sentinel": "str", "timeout_ms": "int"},
                          doc="Block until sentinel in fpga.uart stream.",
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
            })
        return out

    def open(self, spec):
        return FpgaHandle(serial_port=spec.get("serial_port"),
                          baud=spec["baudrate"],
                          ft2232h_desc=spec["ft2232h_desc"])

    def close(self, handle):
        handle.uart_close()
