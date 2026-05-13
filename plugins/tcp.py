# SPDX-License-Identifier: MIT
# tcp.py --- Bench-host TCP client helpers
# Copyright (c) 2026 Jakob Kastelic

import socket
import time

from plugin import DevicePlugin, Op
from plugins._text import decode_escapes, expect_timeout_msg


DEFAULT_TIMEOUT_MS = 5000
MAX_RECV_BYTES = 64 * 1024


def _timeout_ms(args):
    timeout_ms = args.get("timeout_ms")
    if timeout_ms is None:
        return DEFAULT_TIMEOUT_MS
    if timeout_ms <= 0:
        raise ValueError("tcp:recv timeout_ms must be > 0")
    return timeout_ms


def _validate_port(port):
    if not (1 <= port <= 65535):
        raise ValueError("tcp:recv port must be in range 1..65535")


def _op_recv(session, h, args):
    host = args["host"]
    port = args["port"]
    _validate_port(port)
    timeout_ms = _timeout_ms(args)
    timeout_s = timeout_ms / 1000.0
    expect = args.get("expect")
    expected = decode_escapes(expect) if expect is not None else None
    stream = session.stream("tcp.recv")
    received = bytearray()
    deadline = time.monotonic() + timeout_s

    session.log_event(
        "TCP", "tcp:recv",
        f"connect {host}:{port} timeout_ms={timeout_ms}")

    try:
        with socket.create_connection((host, port), timeout_s) as sock:
            while len(received) < MAX_RECV_BYTES:
                session.bail_if_canceled("tcp:recv")
                remain = deadline - time.monotonic()
                if remain <= 0:
                    break
                sock.settimeout(min(0.2, remain))
                try:
                    chunk = sock.recv(MAX_RECV_BYTES - len(received))
                except socket.timeout:
                    continue
                if not chunk:
                    break
                received.extend(chunk)
                stream.append(chunk)
                if expected is not None and expected in received:
                    break
    except socket.timeout as e:
        raise TimeoutError(
            f"tcp:recv connect/read timed out after {timeout_ms} ms") from e

    session.log_event(
        "TCP", "tcp:recv",
        f"{host}:{port} received={len(received)}B")

    if expected is None:
        if not received:
            raise TimeoutError(
                f"tcp.recv received no bytes within {timeout_ms} ms")
        return

    claim = f"tcp.recv contains {expected!r} within {timeout_ms} ms"
    if expected in received:
        session.record_check(
            "tcp_recv", "tcp.recv", claim, "hit",
            {"expect": expected.decode("utf-8", "replace"),
             "bytes": len(received)})
        return

    session.record_check(
        "tcp_recv", "tcp.recv", claim, "timeout",
        {"expect": expected.decode("utf-8", "replace"),
         "bytes": len(received)})
    raise TimeoutError(
        expect_timeout_msg("tcp.recv", expected, timeout_ms, bytes(received)))


class TcpPlugin(DevicePlugin):
    name = "tcp"
    doc = (
        "Bench-host TCP receive helper. Connects from the poller host "
        "to a plan-supplied host/port, captures up to 64 KiB into "
        "stream tcp.recv, and can assert that expected bytes arrived.")

    ops = {
        "recv": Op(
            args={"host": "str", "port": "int"},
            optional_args={"expect": "str", "timeout_ms": "int"},
            doc=("Connect to host:port from the bench host, read a "
                 "bounded response into stream tcp.recv, and optionally "
                 "assert that expect appears in the captured bytes. "
                 f"Default timeout is {DEFAULT_TIMEOUT_MS} ms."),
            run=_op_recv),
    }

    def probe(self):
        return [{"id": "any"}]

    def open(self, spec):
        return _TcpHandle()

    def close(self, handle):
        pass


class _TcpHandle:
    pass
