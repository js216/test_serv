# SPDX-License-Identifier: MIT
# fpga.py --- iCEstick FPGA: flash-program via FT2232H MPSSE + UART drain
# Copyright (c) 2026 Jakob Kastelic

import sys
import json
import os
import subprocess
import tempfile
import threading
import time

import config
from plugin import BusyError, DevicePlugin, Op
from . import _usb
from ._text import decode_escapes, expect_timeout_msg


FT2232H_DESC_A_DEFAULT = "Dual RS232-HS A"    # MPSSE channel
FT2232H_DESC_B_DEFAULT = "Dual RS232-HS B"    # UART channel
FPGA_BAUD_DEFAULT = 115200
FPGA_PROGRAM_GRACE_S = 5.0
# open()-time enumeration settle-retry. After heavy program/cancel
# cycling the D2XX view can transiently report the FT2232H with BLANK
# serial+description (a handle somewhere is still closing) or omit it
# entirely; observed in the field to recover on its own within tens of
# seconds with no physical access (bug report 2026-06-11, agent1).
# Mirrors dsp.py's ENUM_RETRY_DELAYS_S. Kept short enough that even
# with every ftd2xx_devices() helper call hitting its 10s cap the
# whole open() stays under the registry's 60s OPEN_TIMEOUT_S --
# otherwise a slow-but-recovering driver would get the key
# quarantined instead of retried.
ENUM_RETRY_DELAYS_S = (1.0, 3.0, 5.0)


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
        if session is not None:
            session.bail_if_canceled(f"fpga:erase @ {addr}/{nbytes}B")
        _cmd(dev, [0x06])
        _cmd(dev, [0xD8,
                   (addr >> 16) & 0xff,
                   (addr >> 8) & 0xff,
                   addr & 0xff])
        _wait_wip(dev, session=session)


def _write(dev, buf, session=None):
    for off in range(0, len(buf), PAGE):
        if session is not None:
            session.bail_if_canceled(f"fpga:write @ {off}/{len(buf)}B")
        chunk = bytes(buf[off:off + PAGE])
        _cmd(dev, [0x06])
        _cmd(dev, [0x02,
                   (off >> 16) & 0xff,
                   (off >> 8) & 0xff,
                   off & 0xff] + list(chunk))
        _wait_wip(dev, session=session)


def _verify(dev, buf, session=None):
    if session is not None:
        session.bail_if_canceled("fpga:verify (read-back)")
    got = _xfer(dev, [0x03, 0, 0, 0], len(buf), session=session)
    if got == buf:
        return
    for i, (a, b) in enumerate(zip(buf, got)):
        if a != b:
            raise RuntimeError(
                f"verify mismatch at 0x{i:06x}: wrote 0x{a:02x}, got 0x{b:02x}"
            )


def _program_flash(bitstream, ft2232h_desc, ft2232h_serial, session=None):
    ftd2xx = _lazy_ftd2xx()
    # Pinned-serial path is the safe one: opening by the FTDI iSerial
    # picks exactly one device even when several FT2232H boards on
    # the same bench all advertise "Dual RS232-HS A". flag=1 ==
    # OPEN_BY_SERIAL_NUMBER, flag=2 == OPEN_BY_DESCRIPTION.
    if ft2232h_serial:
        dev = ftd2xx.openEx(
            ft2232h_serial.encode()
            if isinstance(ft2232h_serial, str) else ft2232h_serial, 1)
    else:
        dev = ftd2xx.openEx(
            ft2232h_desc.encode()
            if isinstance(ft2232h_desc, str) else ft2232h_desc, 2)
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


def _program_flash_subprocess(bitstream, ft2232h_desc, ft2232h_serial,
                              session):
    """Run the unsafe ftd2xx flash path in a child process.

    ftd2xx can wedge inside a C call while holding the GIL. If that
    happens in the poller process, Ctrl-C, watchdogs, and the main
    loop all stop. A child process gives the parent a real kill
    boundary.
    """
    timeout_s = min(session.runtime_s or 600.0, 3600.0) + FPGA_PROGRAM_GRACE_S
    with tempfile.NamedTemporaryFile(prefix="test-serv-fpga-",
                                     suffix=".bin", delete=False) as f:
        f.write(bitstream)
        bit_path = f.name
    payload = json.dumps({
        "bit_path": bit_path,
        "ft2232h_desc": ft2232h_desc,
        "ft2232h_serial": ft2232h_serial,
    })
    code = (
        "import json, sys\n"
        "from plugins.fpga import _program_flash\n"
        "cfg = json.loads(sys.argv[1])\n"
        "with open(cfg['bit_path'], 'rb') as f:\n"
        "    bitstream = f.read()\n"
        "_program_flash(bitstream, cfg['ft2232h_desc'], "
        "cfg.get('ft2232h_serial'), session=None)\n"
    )
    p = subprocess.Popen(
        [sys.executable, "-c", code, payload],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            rc = p.poll()
            if rc is not None:
                out, err = p.communicate(timeout=1.0)
                if rc == 0:
                    return
                tail = (err or out or "").strip().splitlines()
                msg = tail[-1] if tail else f"exit status {rc}"
                raise RuntimeError(f"fpga program helper failed: {msg}")
            if session.canceled:
                p.kill()
                p.wait(timeout=2.0)
                raise RuntimeError("fpga program canceled")
            if time.monotonic() > deadline:
                p.kill()
                p.wait(timeout=2.0)
                raise RuntimeError(
                    f"fpga program helper timed out after {timeout_s:.0f}s")
            time.sleep(0.05)
    finally:
        try:
            os.unlink(bit_path)
        except OSError:
            pass


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
    def __init__(self, serial_port, baud, ft2232h_desc, ft2232h_serial=None):
        self.serial_port = serial_port
        self.baud = baud
        self.ft2232h_desc = ft2232h_desc
        # When set, _program_flash opens the MPSSE channel by FTDI
        # iSerial (channel-A serial: e.g. "ICESTICK_42A") rather than
        # by the shared "Dual RS232-HS A" description -- mandatory if
        # you have more than one FT2232H board on the bench.
        self.ft2232h_serial = ft2232h_serial
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
        # Pre-empt a wedged blocking read (see mp135.uart_close for
        # rationale).
        try:
            self._ser.cancel_read()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                print(f"fpga uart_close: drain thread did not exit; "
                      f"leaked")
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
        # See mp135._drain: snapshot self._ser to avoid AttributeError
        # if uart_close races to set self._ser = None mid-read.
        ser = self._ser
        try:
            while not self._stop.is_set():
                data = ser.read(1024)
                if data:
                    self._stream.append(data)
            tail = ser.read(4096)
            if tail:
                self._stream.append(tail)
        except Exception:
            pass


# --- ops ---

def _op_program(session, h, args):
    _program_flash_subprocess(
        bytes(args["bin"]), h.ft2232h_desc, h.ft2232h_serial, session)


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
    claim = f"fpga.uart contains {sentinel!r} within {timeout_ms} ms"
    cursor = {}
    while time.monotonic() < deadline:
        if session.canceled:
            return
        if stream.contains_bytes(sentinel, cursor):
            session.log_event("EXPECT", "fpga:uart_expect",
                              f"HIT {sentinel!r}")
            session.record_check(
                "uart_expect", "fpga.uart", claim, "hit",
                {"sentinel": sentinel.decode("utf-8", "replace")})
            if end_session:
                session.signal_early_done(f"fpga.uart saw {sentinel!r}")
            return
        session.cancel_event.wait(0.01)
    session.record_check(
        "uart_expect", "fpga.uart", claim, "timeout",
        {"sentinel": sentinel.decode("utf-8", "replace")})
    raise TimeoutError(expect_timeout_msg(
        "fpga.uart", sentinel, timeout_ms, stream.snapshot_bytes()))


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
        devs = _usb.ftd2xx_devices()
        if devs is None:
            return []
        out = []
        for inst in config.instances(self.name):
            ft_desc = inst.get("ft2232h_desc") or FT2232H_DESC_A_DEFAULT
            want_serial = inst.get("ft2232h_serial") or None
            # Find the MPSSE channel-A device that matches both the
            # description and (if pinned) the iSerial. FTDI auto-
            # appends "A"/"B" to the per-channel serials, so a config
            # value of "ICESTICK_42" matches an enumerated serial of
            # "ICESTICK_42A". Accept either form so operators can
            # write the bare base serial in config.
            match = None
            for d in devs:
                if d["description"] != ft_desc:
                    continue
                if want_serial:
                    s = d["serial"]
                    if s != want_serial and s != f"{want_serial}A":
                        continue
                match = d
                break
            if match is None:
                continue
            uart_desc = inst.get("ft2232h_uart_desc") or FT2232H_DESC_B_DEFAULT
            auto = inst.get("uart_autodetect") or {}
            uart_port = (inst.get("serial_port")
                         or _find_icestick_uart(uart_desc, auto))
            out.append({
                "id": inst.get("id", "0"),
                "ft2232h_desc": ft_desc,
                # Always carry the *enumerated* serial forward (e.g.
                # "ICESTICK_42A") so open() can openEx by serial.
                # Empty string when the chip has no programmed iSerial
                # and the operator chose not to pin -- in that case
                # we fall back to opening by description.
                "ft2232h_serial": match["serial"] if want_serial else "",
                "serial_port": uart_port,
                "baudrate": int(inst.get("baudrate", FPGA_BAUD_DEFAULT)),
                "expected_uart_vid": inst.get("expected_uart_vid"),
                "expected_uart_pid": inst.get("expected_uart_pid"),
                "expected_uart_serial": inst.get("expected_uart_serial"),
                "expected_uart_interface": inst.get("expected_uart_interface"),
                "description": inst.get("description"),
            })
        return out

    def open(self, spec):
        # MPSSE-channel identity: re-walk the enumerated FTDI device
        # list and confirm the iSerial we got from probe is still
        # there. probe ran some seconds (or 15s) ago; the real check
        # has to happen at open time. We don't actually open the
        # MPSSE channel here -- _program_flash does that lazily --
        # but failing fast on a missing chip beats a confusing
        # error inside the flash sequence.
        want_serial = spec.get("ft2232h_serial") or None
        ft_desc = spec["ft2232h_desc"]
        if want_serial:
            seen = []
            saw_blank = False
            for delay in (0.0,) + ENUM_RETRY_DELAYS_S:
                if delay:
                    time.sleep(delay)
                devs = _usb.ftd2xx_devices() or []
                if any(d["description"] == ft_desc
                       and d["serial"] == want_serial for d in devs):
                    break
                seen = sorted(d["serial"] for d in devs)
                # D2XX reports blank serial AND description for a
                # device some process currently holds open -- a blank
                # row means the chip is busy/settling, not unplugged.
                saw_blank = saw_blank or any(
                    not d["serial"] and not d["description"] for d in devs)
            else:
                retries = (f"after {len(ENUM_RETRY_DELAYS_S)} retries "
                           f"over {sum(ENUM_RETRY_DELAYS_S):.0f}s")
                if saw_blank:
                    raise BusyError(
                        f"fpga: FT2232H {want_serial!r} ({ft_desc!r}) "
                        f"did not enumerate {retries}, and an FTDI "
                        f"entry reports blank strings: the chip is "
                        f"most likely still held open (killed flash "
                        f"helper settling, another process) -- not "
                        f"unplugged. If it persists, POST /devices/"
                        f"<key>/release and re-probe.")
                raise RuntimeError(
                    f"fpga: FT2232H {want_serial!r} ({ft_desc!r}) not "
                    f"enumerated {retries}; saw {seen or '(none)'}")
        # UART side: pin VID/PID/iSerial if config gave them.
        # Catches a Windows re-enumeration that lands the configured
        # COM port on a sibling FTDI chip's channel B. _identity_-
        # verified is set so verify_sweep reports the truth.
        port = spec.get("serial_port")
        verified = False
        if port:
            verified = _usb.verify_com_identity(
                port, label="fpga.uart",
                exp_vid=spec.get("expected_uart_vid"),
                exp_pid=spec.get("expected_uart_pid"),
                exp_serial=spec.get("expected_uart_serial"),
                exp_interface=spec.get("expected_uart_interface"))
        h = FpgaHandle(serial_port=spec.get("serial_port"),
                       baud=spec["baudrate"],
                       ft2232h_desc=spec["ft2232h_desc"],
                       ft2232h_serial=spec.get("ft2232h_serial") or None)
        # If either MPSSE serial or UART identity was pinned + verified,
        # mark the handle as identity-verified so verify_sweep doesn't
        # report "open ok (no handshake)" misleadingly.
        if want_serial or verified:
            h._identity_verified = True
        return h

    def close(self, handle):
        # Flush UART buffers before close in case an interrupted
        # uart_write or partial expect left stale bytes.
        ser = getattr(handle, "_ser", None)
        if ser is not None:
            try: ser.reset_input_buffer()
            except Exception: pass
            try: ser.reset_output_buffer()
            except Exception: pass
        handle.uart_close()
