# SPDX-License-Identifier: MIT
# bench_mcu.py --- Small bench-helper MCU (STM32F405): identity + DUT reset
# Copyright (c) 2026 Jakob Kastelic

import time

import config
from plugin import DevicePlugin, Op
from . import _usb
from ._text import decode_escapes


READ_WINDOW_S = 0.5
EXPECTED_IDENTITY = b"STM32F405"   # override per-instance in config.json


def _lazy_serial():
    import serial
    return serial


def _drain(ser, window_s):
    end = time.monotonic() + window_s
    buf = bytearray()
    while time.monotonic() < end:
        data = ser.read(256)
        if data:
            buf += data
            end = time.monotonic() + 0.05
        else:
            time.sleep(0.005)
    return bytes(buf)


def _query_identity(port, baud, timeout_s=READ_WINDOW_S):
    serial = _lazy_serial()
    ser = serial.Serial(port, baudrate=baud, timeout=0.1)
    try:
        ser.reset_input_buffer()
        ser.write(b"?")
        ser.flush()
        return _drain(ser, timeout_s)
    finally:
        ser.close()


# --- ops ---

def _op_identify(session, h, args):
    reply = _query_identity(h.port, h.baud)
    session.stream("bench_mcu.identity").append(reply)
    if not reply:
        raise RuntimeError("bench_mcu returned nothing to '?'")


def _op_reset_dut(session, h, args):
    serial = _lazy_serial()
    ser = serial.Serial(h.port, baudrate=h.baud, timeout=0.1)
    try:
        ser.reset_input_buffer()
        ser.write(b"r")
        ser.flush()
        reply = _drain(ser, READ_WINDOW_S)
    finally:
        ser.close()
    if reply:
        session.stream("bench_mcu.reset_dut").append(reply)


def _op_send(session, h, args):
    """Send arbitrary bytes, drain reply into bench_mcu.send.

    Python-style escapes in ``data`` are decoded, same as uart_write
    on the other UART plugins. Lets plans issue short commands like
    ``data="h"`` (help listing) or ``data="v"`` without the plugin
    needing a new named op per command.
    """
    payload = decode_escapes(args["data"])
    serial = _lazy_serial()
    ser = serial.Serial(h.port, baudrate=h.baud, timeout=0.1)
    try:
        ser.reset_input_buffer()
        ser.write(payload)
        ser.flush()
        reply = _drain(ser, READ_WINDOW_S)
    finally:
        ser.close()
    session.stream("bench_mcu.send").append(reply)


# --- plugin ---

class BenchMcuHandle:
    def __init__(self, port, baud, expected_identity):
        self.port = port
        self.baud = baud
        self.expected_identity = expected_identity


class BenchMcuPlugin(DevicePlugin):
    name = "bench_mcu"
    doc = ("Small bench-helper MCU (STM32F405 wired to COMxx). '?' replies "
           "an identity string; 'r' pulses its D13 to reset whatever DUT "
           "is wired there.  Port comes from config.json instances; an "
           "autodetect VID/PID rule is honoured when the port field is "
           "omitted.")

    ops = {
        "identify": Op(args={},
                       doc="Send '?', capture reply to stream "
                           "bench_mcu.identity.",
                       run=_op_identify),
        "reset_dut": Op(args={},
                        doc="Send 'r' to assert D13 reset on the DUT.",
                        run=_op_reset_dut),
        "send": Op(args={"data": "str"},
                   doc=("Send arbitrary bytes (escapes decoded: "
                        "\\r \\n \\0 \\xNN), drain reply into stream "
                        "bench_mcu.send.  Use for help ('h'), version "
                        "('v'), any one-shot bench-MCU command."),
                   run=_op_send),
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
                "expected_identity": (inst.get("expected_identity")
                                      or EXPECTED_IDENTITY.decode()),
                "description": inst.get("description"),
            })
        return out

    def open(self, spec):
        handle = BenchMcuHandle(
            port=spec["serial_port"],
            baud=spec["baudrate"],
            expected_identity=spec["expected_identity"].encode(),
        )
        # Identity handshake: refuse to hand back a handle if the device
        # at ``port`` does not answer '?' with the expected string.
        # Prevents handing ops a misassigned COM port (wrong device on
        # this slot after Windows re-enumerated USB).
        reply = _query_identity(handle.port, handle.baud)
        if handle.expected_identity not in reply:
            raise RuntimeError(
                f"bench_mcu identity mismatch on {handle.port}: "
                f"expected {handle.expected_identity!r}, got {reply!r}"
            )
        handle._identity_verified = True
        return handle

    def close(self, handle):
        pass
