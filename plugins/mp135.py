# SPDX-License-Identifier: MIT
# mp135.py --- STM32MP135 eval board serial console via STLink VCP
# Copyright (c) 2026 Jakob Kastelic

import threading
import time

import config
from plugin import DevicePlugin, Op
from . import _usb


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
    h.uart_write(args["data"].encode("utf-8"))


def _op_uart_expect(session, h, args):
    sentinel = args["sentinel"].encode("utf-8")
    timeout_ms = args["timeout_ms"]
    stream = session.stream("mp135.uart")
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if sentinel in stream.snapshot_bytes():
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
                         doc="Write UTF-8 bytes to console.",
                         run=_op_uart_write),
        "uart_expect": Op(args={"sentinel": "str", "timeout_ms": "int"},
                          doc="Block until sentinel in mp135.uart; sets "
                              "early_done.",
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
            })
        return out

    def open(self, spec):
        h = Mp135Handle(port=spec["serial_port"], baud=spec["baudrate"])
        # Claim the port briefly to verify the OS lets us have it, then
        # release. Catches "another process already holds this port" at
        # open-time rather than on first uart_open. No handshake byte on
        # the wire -- a Linux boot console has no prompt we can rely on.
        try:
            import serial
            ser = serial.Serial(h.port, baudrate=h.baud, timeout=0.1)
            ser.close()
        except Exception as e:
            raise RuntimeError(
                f"mp135: cannot claim {h.port}: {e}")
        return h

    def close(self, handle):
        handle.uart_close()
