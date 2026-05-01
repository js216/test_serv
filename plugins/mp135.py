# SPDX-License-Identifier: MIT
# mp135.py --- STM32MP135 eval board serial console via STLink VCP
# Copyright (c) 2026 Jakob Kastelic

import threading
import time

import config
from plugin import DevicePlugin, Op
from . import _usb
from ._text import decode_escapes


# --- handle with background UART reader ---

class Mp135Handle:
    def __init__(self, port, baud):
        self.port = port
        self.baud = baud
        self._ser = None
        self._thread = None
        self._stop = None
        self._stream = None

    def uart_open(self, session):
        if self._ser is not None:
            return
        import serial
        self._ser = serial.Serial(
            self.port, baudrate=self.baud, timeout=0.1,
            dsrdtr=False, rtscts=False, xonxoff=False,
        )
        # DTR/RTS on open can reset the target on some STLink firmwares.
        try:
            self._ser.setDTR(False)
            self._ser.setRTS(False)
        except Exception:
            pass
        self._ser.reset_input_buffer()
        self._stream = session.stream("mp135.uart")
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
                "mp135 uart not open (call mp135:uart_open first)")
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

def _op_uart_open(session, h, args):
    h.uart_open(session)


def _op_uart_close(session, h, args):
    h.uart_close()


def _op_uart_write(session, h, args):
    # decode_escapes lets plans send CR / LF / NUL via "\r", "\n", "\0".
    # Without it shlex keeps the backslash literal and the bootloader
    # shell never sees the Enter keystroke it needs.
    h.uart_write(decode_escapes(args["data"]))


def _op_uart_expect(session, h, args):
    sentinel = decode_escapes(args["sentinel"])
    timeout_ms = args["timeout_ms"]
    end_session = bool(args.get("end_session"))
    stream = session.stream("mp135.uart")
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if sentinel in stream.snapshot_bytes():
            session.log_event("EXPECT", "mp135:uart_expect",
                              f"HIT {sentinel!r}")
            if end_session:
                session.signal_early_done(f"mp135.uart saw {sentinel!r}")
            return
        time.sleep(0.01)
    raise TimeoutError(
        f"mp135.uart did not contain {sentinel!r} within {timeout_ms} ms")


# --- plugin ---

class Mp135Plugin(DevicePlugin):
    name = "mp135"
    doc = ("STM32MP135 eval board serial console via STLink VCP "
           "(configured in config.json, usually ST VID 0483 PID 3753 "
           "interface MI_01).  Background-drained into mp135.uart stream.")

    ops = {
        "uart_open": Op(args={},
                        doc="Open console, start capture into mp135.uart.",
                        run=_op_uart_open),
        "uart_close": Op(args={}, doc="Stop capture, close port.",
                         run=_op_uart_close),
        "uart_write": Op(args={"data": "str"},
                         doc=("Write to console. Python-style escapes "
                              "decoded: \\r \\n \\t \\0 \\xNN etc."),
                         run=_op_uart_write),
        "uart_expect": Op(
            args={"sentinel": "str", "timeout_ms": "int"},
            optional_args={"end_session": "bool"},
            doc=("Block until sentinel appears in mp135.uart, then "
                 "continue.  Pass end_session=true to short-circuit "
                 "the rest of the plan (old wait_uart semantics)."),
            run=_op_uart_expect),
    }

    def probe(self):
        out = []
        for inst in config.instances(self.name):
            port = inst.get("serial_port")
            if port is None:
                auto = inst.get("autodetect") or {}
                port = _usb.find_com_by_vid_pid(**auto) if auto else None
            if port is None or not _usb.com_port_present(port):
                continue
            out.append({
                "id": inst.get("id", "0"),
                "serial_port": port,
                "baudrate": int(inst.get("baudrate", 115200)),
                "expected_usb_vid": inst.get("expected_usb_vid"),
                "expected_usb_pid": inst.get("expected_usb_pid"),
                "expected_usb_serial": inst.get("expected_usb_serial"),
                "expected_usb_interface": inst.get("expected_usb_interface"),
                "description": inst.get("description"),
            })
        return out

    def open(self, spec):
        h = Mp135Handle(port=spec["serial_port"], baud=spec["baudrate"])

        # Identity check: look up the USB properties backing this COMxx.
        # Linux boot console emits no stable prompt, so on-wire handshake
        # isn't reliable. Instead verify that the COM port is really an
        # STLink VCP (matching VID/PID/iSerial) -- the STM32F405 and any
        # generic USB-UART share a port namespace but have different USB
        # identities.
        info = _usb.com_port_info(h.port)
        if info is None:
            raise RuntimeError(f"mp135: {h.port} not in OS port list")

        exp_vid = spec.get("expected_usb_vid")
        exp_pid = spec.get("expected_usb_pid")
        exp_ser = spec.get("expected_usb_serial")
        exp_if  = spec.get("expected_usb_interface")
        verified = False
        if exp_vid is not None:
            if info.vid != config.as_int(exp_vid):
                raise RuntimeError(
                    f"mp135 USB VID mismatch on {h.port}: "
                    f"expected 0x{config.as_int(exp_vid):04x}, "
                    f"got 0x{info.vid or 0:04x}")
            verified = True
        if exp_pid is not None:
            if info.pid != config.as_int(exp_pid):
                raise RuntimeError(
                    f"mp135 USB PID mismatch on {h.port}: "
                    f"expected 0x{config.as_int(exp_pid):04x}, "
                    f"got 0x{info.pid or 0:04x}")
            verified = True
        if exp_ser is not None:
            got = info.serial_number or ""
            if exp_ser not in got:
                raise RuntimeError(
                    f"mp135 USB iSerial mismatch on {h.port}: "
                    f"expected {exp_ser!r} substring, got {got!r}")
            verified = True
        if exp_if is not None:
            loc = (info.location or "") + " " + (info.hwid or "")
            n = int(exp_if)
            # Various driver stacks encode the USB interface number
            # differently in hwid / location strings:
            #   Windows usbser.sys:        MI_0N
            #   Linux sysfs-derived:       :1.N
            #   Some Windows STLink VCP:   :x.N   (config index is 'x')
            needles = (f"MI_{n:02d}", f":1.{n}", f":x.{n}")
            if not any(x in loc for x in needles):
                raise RuntimeError(
                    f"mp135 USB interface mismatch on {h.port}: "
                    f"expected interface {n} "
                    f"(one of {needles}), got {loc!r}")
            verified = True

        # Claim the port briefly so contention (PuTTY etc.) fails now.
        try:
            import serial
            ser = serial.Serial(h.port, baudrate=h.baud, timeout=0.1)
            ser.close()
        except Exception as e:
            raise RuntimeError(
                f"mp135: cannot claim {h.port}: {e}")

        if verified:
            h._identity_verified = True
        return h

    def close(self, handle):
        handle.uart_close()
