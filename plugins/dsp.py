# SPDX-License-Identifier: MIT
# dsp.py --- SHARC DSP over FT4222 (QSPI boot + QSPI link tests + UART)
# Copyright (c) 2026 Jakob Kastelic

import threading
import time
import traceback

import config
from plugin import DevicePlugin, Op, BusyError
from ._prbs import prbs_xorshift32
from ._text import decode_escapes
from . import _usb


def _lazy_ft4222():
    import ft4222
    return ft4222


def _lazy_serial():
    import serial
    return serial


class DspHandle:
    """Everything dsp ops need. Exposes the FT4222 descriptor, the serial
    port name, and a UART reader lifecycle.
    """
    def __init__(self, serial_port, baud, ft4222_desc):
        self.serial_port = serial_port
        self.baud = baud
        self.ft4222_desc = ft4222_desc
        self._ser = None
        self._reader_thread = None
        self._reader_stop = None
        self._stream = None

    # --- UART ---

    def uart_open(self, session):
        if self._ser is not None:
            return
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
        self._stream = session.stream("dsp.uart")
        self._reader_stop = threading.Event()
        self._reader_thread = threading.Thread(
            target=self._reader_fn, daemon=True)
        self._reader_thread.start()

    def uart_close(self):
        if self._ser is None:
            return
        self._reader_stop.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)
        try:
            self._ser.close()
        except Exception:
            pass
        self._ser = None
        self._reader_thread = None

    def uart_write(self, data, inter_byte_s=0.05):
        if self._ser is None:
            raise RuntimeError("dsp uart not open (call dsp:uart_open first)")
        for ch in data:
            self._ser.write(bytes([ch]))
            self._ser.flush()
            if inter_byte_s > 0:
                time.sleep(inter_byte_s)

    def _reader_fn(self):
        try:
            while not self._reader_stop.is_set():
                data = self._ser.read(1024)
                if data:
                    self._stream.append(data)
            tail = self._ser.read(4096)
            if tail:
                self._stream.append(tail)
        except Exception:
            pass


# ---- Expander (I2C on FT4222 A) ----

class _Expander:
    def __init__(self, ft4222_desc):
        self.desc = ft4222_desc

    def _wr(self, dev, writes):
        ft = _lazy_ft4222()
        for a, b, c in writes:
            dev.i2cMaster_WriteEx(
                a, ft.I2CMaster.Flag.START_AND_STOP, bytes([b, c]))

    def init_and_reset(self):
        ft = _lazy_ft4222()
        dev = ft.openByDescription(self.desc)
        dev.i2cMaster_Init(100)
        try:
            self._wr(dev, [(0x30, 0x1D, 0x00)])
            self._wr(dev, [(0x30, 0x1E, 0x00)])
            self._wr(dev, [(0x30, 0x1F, 0x00)])
            self._wr(dev, [(0x30, 0x18, 0b1001_1111)])
            self._wr(dev, [(0x30, 0x19, 0b0000_0011)])
            self._wr(dev, [(0x30, 0x24, 0xff)])
            self._wr(dev, [(0x30, 0x25, 0xff)])
            self._wr(dev, [(0x30, 0x18, 0b1001_1011)])   # QSPI enabled
        finally:
            dev.close()

    def pulse_reset(self):
        """DS8 is repurposed as eval-board reset; toggle it briefly."""
        ft = _lazy_ft4222()
        dev = ft.openByDescription(self.desc)
        dev.i2cMaster_Init(100)
        try:
            self._wr(dev, [(0x30, 0x18, 0b0001_1011)])
            time.sleep(0.25)
            self._wr(dev, [(0x30, 0x18, 0b1001_1111)])
        finally:
            dev.close()


# ---- QSPI primitives ----

CLK_DIVS = (2, 4, 8, 16, 32, 64, 128, 256, 512)


def _ft_clock_div(n):
    ft = _lazy_ft4222()
    m = {
        2: ft.SPIMaster.Clock.DIV_2,
        4: ft.SPIMaster.Clock.DIV_4,
        8: ft.SPIMaster.Clock.DIV_8,
        16: ft.SPIMaster.Clock.DIV_16,
        32: ft.SPIMaster.Clock.DIV_32,
        64: ft.SPIMaster.Clock.DIV_64,
        128: ft.SPIMaster.Clock.DIV_128,
        256: ft.SPIMaster.Clock.DIV_256,
        512: ft.SPIMaster.Clock.DIV_512,
    }
    return m[n]


def _ft_mode(n):
    ft = _lazy_ft4222()
    m = {1: ft.SPIMaster.Mode.SINGLE,
         2: ft.SPIMaster.Mode.DUAL,
         4: ft.SPIMaster.Mode.QUAD}
    return m[n]


def _ft_cpol_cpha(flags):
    ft = _lazy_ft4222()
    cpol = (ft.SPI.Cpol.IDLE_HIGH if (flags & 0x1) else ft.SPI.Cpol.IDLE_LOW)
    cpha = (ft.SPI.Cpha.CLK_LEADING if (flags & 0x2)
            else ft.SPI.Cpha.CLK_TRAILING)
    return cpol, cpha


def _open_master(desc, clk_div=8, mode=1, flags=0):
    ft = _lazy_ft4222()
    dev = ft.openByDescription(desc)
    dev.setClock(ft.SysClock.CLK_80)
    cpol, cpha = _ft_cpol_cpha(flags)
    dev.spiMaster_Init(
        _ft_mode(mode), _ft_clock_div(clk_div), cpol, cpha,
        ft.SPIMaster.SlaveSelect.SS0,
    )
    return dev


# ---- op implementations ----

def _op_reset(session, h, args):
    _Expander(h.ft4222_desc).pulse_reset()
    _Expander(h.ft4222_desc).init_and_reset()


def _op_boot(session, h, args):
    ldr = args["ldr"]
    # LDR is ASCII hex, one byte per line. Prefix 0x03 = BDMA boot.
    buf = bytearray([0x03])
    for line in ldr.decode("ascii", errors="strict").split():
        buf.append(int(line.strip(), 16))
    # pad to 1024
    CHUNK = 1024
    n = len(buf)
    padded = ((n + CHUNK - 1) // CHUNK) * CHUNK
    if padded != n:
        buf += bytes(padded - n)
    dev = _open_master(h.ft4222_desc, clk_div=8, mode=1, flags=0)
    try:
        for i in range(0, padded, CHUNK):
            last = (i + CHUNK) >= padded
            dev.spiMaster_SingleWrite(bytes(buf[i:i+CHUNK]), last)
    finally:
        dev.close()


def _op_uart_open(session, h, args):
    h.uart_open(session)


def _op_uart_close(session, h, args):
    h.uart_close()


def _op_uart_write(session, h, args):
    h.uart_write(decode_escapes(args["data"]))


def _op_uart_expect(session, h, args):
    sentinel = decode_escapes(args["sentinel"])
    timeout_ms = args["timeout_ms"]
    stream = session.stream("dsp.uart")
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if sentinel in stream.snapshot_bytes():
            session.signal_early_done(
                f"dsp.uart saw {sentinel!r}")
            return
        time.sleep(0.01)
    raise TimeoutError(
        f"dsp.uart did not contain {sentinel!r} within {timeout_ms} ms")


CHUNK = 16384

# FT4222 MultiReadWrite is atomic per call: one CS-low window, then
# CS goes high. Unlike SingleWrite it has no "keep CS asserted" flag,
# so chunking a multi-lane transfer across several calls makes the
# slave see several independent transactions rather than one stream.
# PRBS sequences lose alignment at every chunk boundary in that case,
# so we refuse to chunk in multi-lane mode and raise instead. Empirical
# pyft4222 cap is around 64 KiB per call; bench firmware that needs
# more must split the test into several ops.
MULTI_MAX_BYTES = 65536


def _master_write(dev, data, mode):
    """Write ``data`` over FT4222 master; dispatches by init mode.

    mode=1 (SINGLE): stream over MOSI via SingleWrite; CS held low
    across chunks via the ``last`` flag so the slave sees one
    continuous transaction.

    mode=2/4 (DUAL/QUAD): one MultiReadWrite call carries the whole
    buffer; no chunking (see MULTI_MAX_BYTES note). ``single_buf`` is
    empty -- this op emits no cmd/addr phase, so the slave test
    firmware must be written to accept pure data on the multi-IO
    lanes. Flash-protocol tests that need a cmd prefix should grow a
    separate op with an explicit ``cmd`` arg.
    """
    raw = bytes(data)
    if mode == 1:
        for off in range(0, len(raw), CHUNK):
            last = (off + CHUNK) >= len(raw)
            dev.spiMaster_SingleWrite(raw[off:off+CHUNK], last)
    else:
        if len(raw) > MULTI_MAX_BYTES:
            raise ValueError(
                f"multi-lane write of {len(raw)} B exceeds "
                f"{MULTI_MAX_BYTES} B per-call cap; CS deasserts "
                f"between calls, so splitting would break a "
                f"continuous-stream test. Lower n, or rework the "
                f"slave test firmware to accept multiple "
                f"transactions.")
        dev.spiMaster_MultiReadWrite(b"", raw, 0)


def _master_read(dev, n, mode):
    """Read ``n`` bytes; dispatches by init mode.

    Same CS-continuity caveat as _master_write applies -- multi-lane
    reads larger than MULTI_MAX_BYTES would require multiple
    transactions to the slave.
    """
    if mode == 1:
        return bytes(dev.spiMaster_SingleRead(n, True))
    if n > MULTI_MAX_BYTES:
        raise ValueError(
            f"multi-lane read of {n} B exceeds {MULTI_MAX_BYTES} B "
            f"per-call cap")
    return bytes(dev.spiMaster_MultiReadWrite(b"", b"", n))


def _op_qspi_write(session, h, args):
    data = args["data"]
    mode = args["mode"]
    dev = _open_master(h.ft4222_desc,
                       clk_div=args["clk_div"], mode=mode, flags=0)
    try:
        _master_write(dev, data, mode)
    finally:
        dev.close()


def _op_qspi_read(session, h, args):
    n = args["n"]
    mode = args["mode"]
    dev = _open_master(h.ft4222_desc,
                       clk_div=args["clk_div"], mode=mode, flags=0)
    try:
        got = _master_read(dev, n, mode)
    finally:
        dev.close()
    session.stream("dsp.qspi_read").append(got)


def _op_qspi_write_prbs(session, h, args):
    seed = args["seed"]
    n = args["n"]
    mode = args["mode"]
    buf = prbs_xorshift32(seed, n)
    dev = _open_master(h.ft4222_desc,
                       clk_div=args["clk_div"], mode=mode, flags=0)
    try:
        _master_write(dev, buf, mode)
    finally:
        dev.close()


def _op_qspi_read_verify_prbs(session, h, args):
    seed = args["seed"]
    n = args["n"]
    mode = args["mode"]
    expected = prbs_xorshift32(seed, n)
    dev = _open_master(h.ft4222_desc,
                       clk_div=args["clk_div"], mode=mode, flags=0)
    try:
        got = _master_read(dev, n, mode)
    finally:
        dev.close()
    if got == expected:
        session.log_event("VERIFY", "dsp:qspi_read_verify_prbs",
                          f"OK {n}B seed=0x{seed:08x}")
        return
    mism = sum(1 for a, b in zip(got, expected) if a != b)
    first = next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b),
                 -1)
    session.log_event(
        "VERIFY", "dsp:qspi_read_verify_prbs",
        f"FAIL mismatches={mism} first_at={first} seed=0x{seed:08x}")
    start = max(0, first - 64)
    session.stream("dsp.qspi_mismatch").append(
        b"--MISMATCH--" + got[start:start+256])
    raise ValueError(
        f"PRBS verify failed: {mism} mismatches, first at {first}")


def _op_qspi_xfer_prbs(session, h, args):
    seed = args["seed"]
    n = args["n"]
    mode = args["mode"]
    # SingleReadWrite is only implemented for SINGLE-lane on FT4222;
    # full-duplex xfer in dual/quad is not a valid QSPI concept
    # (the lanes are unidirectional during a multi-IO phase).
    if mode != 1:
        raise ValueError(
            "dsp:qspi_xfer_prbs requires mode=1; use "
            "qspi_write_prbs + qspi_read_verify_prbs for dual/quad")
    buf = prbs_xorshift32(seed, n)
    dev = _open_master(h.ft4222_desc,
                       clk_div=args["clk_div"], mode=mode, flags=0)
    try:
        got = bytes(dev.spiMaster_SingleReadWrite(buf, True))
    finally:
        dev.close()
    session.stream("dsp.qspi_xfer").append(got)


# ---- plugin class ----

class DspPlugin(DevicePlugin):
    name = "dsp"
    doc = "SHARC DSP over FT4222 QSPI master; expander reset; UART drain."

    ops = {
        "reset": Op(args={}, doc="Pulse expander reset + reinit.",
                    run=_op_reset),
        "boot": Op(args={"ldr": "blob"},
                   doc="Load LDR firmware via FT4222 QSPI boot path.",
                   run=_op_boot),
        "uart_open": Op(args={}, doc="Start UART capture into dsp.uart.",
                        run=_op_uart_open),
        "uart_close": Op(args={}, doc="Stop UART capture.",
                         run=_op_uart_close),
        "uart_write": Op(args={"data": "str"},
                         doc=("Write to DSP UART byte-at-a-time. "
                              "Python-style escapes decoded: "
                              "\\r \\n \\t \\0 \\xNN etc."),
                         run=_op_uart_write),
        "uart_expect": Op(args={"sentinel": "str", "timeout_ms": "int"},
                          doc="Block until sentinel appears in dsp.uart; "
                              "sets early_done.",
                          run=_op_uart_expect),
        "qspi_write": Op(args={"data": "blob", "clk_div": "int", "mode": "int"},
                         doc="Raw QSPI master write of a blob.",
                         run=_op_qspi_write),
        "qspi_read": Op(args={"n": "int", "clk_div": "int", "mode": "int"},
                        doc="Raw QSPI master read; result appended to "
                            "stream dsp.qspi_read.",
                        run=_op_qspi_read),
        "qspi_write_prbs": Op(
            args={"seed": "int", "n": "int",
                  "clk_div": "int", "mode": "int"},
            doc="Write n bytes of xorshift32(seed) over QSPI.",
            run=_op_qspi_write_prbs),
        "qspi_read_verify_prbs": Op(
            args={"seed": "int", "n": "int",
                  "clk_div": "int", "mode": "int"},
            doc="Read n bytes, compare to xorshift32(seed); fails op on "
                "mismatch.",
            run=_op_qspi_read_verify_prbs),
        "qspi_xfer_prbs": Op(
            args={"seed": "int", "n": "int",
                  "clk_div": "int", "mode": "int"},
            doc="Full-duplex xfer of xorshift32(seed); MISO goes into "
                "stream dsp.qspi_xfer.",
            run=_op_qspi_xfer_prbs),
    }

    def probe(self):
        descs = _usb.ftd2xx_descriptors()
        if descs is None:
            return []
        out = []
        for inst in config.instances(self.name):
            ft_desc = inst.get("ft4222_desc")
            if not ft_desc or ft_desc not in descs:
                continue
            out.append({
                "id": inst.get("id", "A"),
                "serial_port": inst.get("serial_port"),
                "baudrate": int(inst.get("baudrate", 115200)),
                "ft4222_desc": ft_desc,
                "ft4222_serial": inst.get("ft4222_serial"),
            })
        return out

    def open(self, spec):
        # Identity handshake: walk the FTDI device info list and confirm
        # an entry matches both the expected description and (if pinned)
        # serial.  Catches the case where Windows kept the same
        # "FT4222 A" label but the hardware behind it changed, which is
        # the failure mode bare-descriptor-matching can't see.
        desc = spec["ft4222_desc"]
        expected_serial = spec.get("ft4222_serial")
        try:
            import ft4222 as ft_mod
        except ImportError:
            raise RuntimeError("pyft4222 not installed")
        matched = None
        for i in range(ft_mod.createDeviceInfoList()):
            info = ft_mod.getDeviceInfoDetail(i, False)
            d = info.get("description", b"")
            s = info.get("serial", b"")
            if isinstance(d, bytes):
                d = d.decode(errors="replace")
            if isinstance(s, bytes):
                s = s.decode(errors="replace")
            if d != desc:
                continue
            if expected_serial and s != expected_serial:
                continue
            matched = (d, s, info.get("type"), info.get("id"))
            break
        if matched is None:
            raise RuntimeError(
                f"dsp: no FTDI device matches desc={desc!r} "
                f"serial={expected_serial!r}")
        handle = DspHandle(serial_port=spec["serial_port"],
                           baud=spec["baudrate"],
                           ft4222_desc=spec["ft4222_desc"])
        handle._identity_verified = True
        return handle

    def close(self, handle):
        handle.uart_close()
