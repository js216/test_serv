# SPDX-License-Identifier: MIT
# mp257.py --- STM32MP257 eval board serial console via STLink VCP
# Copyright (c) 2026 Jakob Kastelic

import threading
import time

import config
from plugin import DevicePlugin, Op
from . import _usb
from ._text import decode_escapes, expect_timeout_msg


# --- handle with background UART reader ---

class Mp257Handle:
    def __init__(self, port, baud, stream_name):
        self.port = port
        self.baud = baud
        self.stream_name = stream_name
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
        self._stream = session.stream(self.stream_name)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def uart_close(self):
        if self._ser is None:
            return
        self._stop.set()
        # Pre-empt a wedged blocking read: pyserial 3.5+ exposes
        # cancel_read() which interrupts a read() stuck in the
        # kernel/driver. Without this, a flaky USB tty can leak the
        # drain thread (and its serial fd) forever.
        try:
            self._ser.cancel_read()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                print(f"mp257 uart_close: drain thread on "
                      f"{self.port!r} did not exit; leaked")
        try:
            self._ser.close()
        except Exception:
            pass
        self._ser = None
        self._thread = None

    def uart_write(self, data):
        if self._ser is None:
            raise RuntimeError(
                "mp257 uart not open (call mp257:uart_open first)")
        self._ser.write(data)
        self._ser.flush()

    def _drain(self):
        # Snapshot self._ser locally; uart_close sets self._ser = None
        # after self._stop.set(), and a naive self._ser.read() could
        # AttributeError after that race. Local ref keeps the read on a
        # (possibly closed-by-now) handle, which raises and exits cleanly.
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
    stream = session.stream(h.stream_name)
    deadline = time.monotonic() + timeout_ms / 1000.0
    claim = f"{h.stream_name} contains {sentinel!r} within {timeout_ms} ms"
    cursor = {}
    while time.monotonic() < deadline:
        if session.canceled:
            return
        if stream.contains_bytes(sentinel, cursor):
            session.log_event("EXPECT", "mp257:uart_expect",
                              f"HIT {sentinel!r}")
            session.record_check(
                "uart_expect", h.stream_name, claim, "hit",
                {"sentinel": sentinel.decode("utf-8", "replace"),
                 "elapsed_ms": int((time.monotonic()
                                    - (deadline - timeout_ms / 1000.0))
                                   * 1000)})
            if end_session:
                session.signal_early_done(
                    f"{h.stream_name} saw {sentinel!r}")
            return
        # cancel_event wakes us instantly on cancel; otherwise tick at
        # 10 ms like the original poll loop.
        session.cancel_event.wait(0.01)
    session.record_check(
        "uart_expect", h.stream_name, claim, "timeout",
        {"sentinel": sentinel.decode("utf-8", "replace")})
    raise TimeoutError(expect_timeout_msg(
        h.stream_name, sentinel, timeout_ms, stream.snapshot_bytes()))


# --- plugin ---

class Mp257Plugin(DevicePlugin):
    name = "mp257"
    doc = ("STM32MP257 eval board (MP257F-EV1) serial console via the "
           "on-board STLink-V3 VCP (ST VID 0483 PID 3753). The STLink-V3 "
           "exposes two VCP UARTs -- interface MI_01 and MI_04 -- so each "
           "configured instance pins its own interface and is "
           "background-drained into its own mp257.<id>.uart stream. "
           "Unlike the mp135 bench boards, this board is not wired to "
           "bench_mcu, so reset is manual (board button / power).")

    ops = {
        "uart_open": Op(args={},
                        doc="Open console, start capture into "
                            "mp257.<id>.uart.",
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
            doc=("Block until sentinel appears in mp257.<id>.uart, then "
                 "continue.  Pass end_session=true to short-circuit the "
                 "rest of the plan."),
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
        stream_name = f"mp257.{spec.get('id', '0')}.uart"
        h = Mp257Handle(port=spec["serial_port"], baud=spec["baudrate"],
                        stream_name=stream_name)

        # Identity check: look up the USB properties backing this tty.
        # The Linux boot console emits no stable prompt, so an on-wire
        # handshake isn't reliable. Instead verify the tty is really the
        # expected STLink VCP (matching VID/PID/iSerial/interface) -- the
        # two VCPs share a port namespace but differ by interface number.
        verified = _usb.verify_com_identity(
            h.port, label="mp257",
            exp_vid=spec.get("expected_usb_vid"),
            exp_pid=spec.get("expected_usb_pid"),
            exp_serial=spec.get("expected_usb_serial"),
            exp_interface=spec.get("expected_usb_interface"))

        # Claim the port briefly so contention (PuTTY etc.) fails now.
        try:
            import serial
            ser = serial.Serial(h.port, baudrate=h.baud, timeout=0.1)
            ser.close()
        except Exception as e:
            from plugin import BusyError
            raise BusyError(
                f"mp257: cannot claim {h.port}: {e}; another process "
                f"may have it (PuTTY / minicom / a stale poller?)")

        if verified:
            h._identity_verified = True
        return h

    def close(self, handle):
        # Flush UART buffers before close so the next session's uart_open
        # starts clean even if an op was interrupted mid-write.
        ser = getattr(handle, "_ser", None)
        if ser is not None:
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
            try:
                ser.reset_output_buffer()
            except Exception:
                pass
        handle.uart_close()
