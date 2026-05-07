# SPDX-License-Identifier: MIT
# ws.py --- Plain WebSocket receive/checksum/rate tests
# Copyright (c) 2026 Jakob Kastelic

import hashlib
import threading
import time
import zlib

import config
from plugin import DevicePlugin, Op


MAX_RECV = 1024 * 1024 * 1024


def _lazy_websocket():
    import websocket
    return websocket


def _check_min_rate(op_name, total, elapsed_s, min_rate_Bps):
    if not min_rate_Bps or total == 0:
        return
    if elapsed_s <= 0:
        return
    rate = total / elapsed_s
    if rate < min_rate_Bps:
        raise TimeoutError(
            f"{op_name} too slow: {rate:.0f} B/s < {min_rate_Bps} B/s")


def _fmt_crc32(v):
    return f"0x{v & 0xffffffff:08x}"


def _is_ws_timeout(exc):
    return exc.__class__.__name__ in (
        "WebSocketTimeoutException", "TimeoutError")


class WsHandle:
    def __init__(self, spec):
        self.spec = spec


def _resolve_url(h, args):
    url = args.get("url") or h.spec.get("url")
    if not url:
        raise ValueError("ws:recv requires url= or configured ws instance url")
    if not (url.startswith("ws://") or url.startswith("wss://")):
        raise ValueError(f"ws:recv bad websocket URL {url!r}")
    return url


def _recv_frame(ws, deadline, session):
    remain = deadline - time.monotonic()
    if remain <= 0:
        raise TimeoutError("ws:recv timed out")
    ws.settimeout(min(0.2, remain))
    while True:
        session.bail_if_canceled("ws:recv")
        try:
            msg = ws.recv()
            break
        except Exception as e:
            if not _is_ws_timeout(e):
                raise
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise TimeoutError("ws:recv timed out")
            ws.settimeout(min(0.2, remain))
    if isinstance(msg, str):
        return msg.encode()
    return bytes(msg)


def _connect_with_cancel(websocket, url, timeout_s, session):
    box = {}

    def _connect():
        try:
            box["ws"] = websocket.create_connection(url, timeout=timeout_s)
        except BaseException as e:
            box["err"] = e

    t = threading.Thread(target=_connect, daemon=True,
                         name="ws-connect")
    t.start()
    deadline = time.monotonic() + timeout_s
    while t.is_alive():
        session.bail_if_canceled("ws:recv connect")
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise TimeoutError("ws:recv connect timed out")
        t.join(timeout=min(0.2, remain))
    if "err" in box:
        raise box["err"]
    return box["ws"]


def _op_recv(session, h, args):
    n_expected = args["bytes"]
    if n_expected < 0 or n_expected > MAX_RECV:
        raise ValueError(f"ws:recv bytes out of range: {n_expected}")
    timeout_ms = args["timeout_ms"]
    if timeout_ms <= 0:
        raise ValueError("ws:recv timeout_ms must be > 0")
    url = _resolve_url(h, args)
    min_rate_Bps = args.get("min_rate_Bps")
    expect_sha256 = args.get("expect_sha256")
    expect_crc32 = args.get("expect_crc32")
    save_stream = bool(args.get("stream") or False)

    websocket = _lazy_websocket()
    deadline = time.monotonic() + timeout_ms / 1000.0
    sha = hashlib.sha256()
    crc = 0
    total = 0
    stream = session.stream("ws.recv") if save_stream else None
    t0 = time.monotonic()
    ws = _connect_with_cancel(
        websocket, url, timeout_ms / 1000.0, session)
    try:
        while total < n_expected:
            data = _recv_frame(ws, deadline, session)
            if not data:
                continue
            if total + len(data) > n_expected:
                raise RuntimeError(
                    f"ws:recv got too much data: "
                    f"{total + len(data)}B > {n_expected}B")
            total += len(data)
            sha.update(data)
            crc = zlib.crc32(data, crc)
            if stream is not None:
                stream.append(data)
    finally:
        try:
            ws.close()
        except Exception:
            pass

    elapsed = time.monotonic() - t0
    _check_min_rate("ws:recv", total, elapsed, min_rate_Bps)
    sha_hex = sha.hexdigest()
    crc_hex = _fmt_crc32(crc)
    rate = (total / elapsed) if elapsed > 0 else 0.0
    evidence = {
        "bytes": total,
        "elapsed_s": elapsed,
        "rate_Bps": rate,
        "sha256": sha_hex,
        "crc32": crc_hex,
    }
    session.log_event(
        "WS", "ws:recv",
        f"{total}B in {elapsed:.3f}s @ {rate:.0f} B/s "
        f"sha256={sha_hex} crc32={crc_hex}")
    if total != n_expected:
        session.record_check(
            "ws_recv", url, f"received exactly {n_expected}B",
            "miss", evidence)
        raise TimeoutError(f"ws:recv expected {n_expected}B, got {total}B")
    session.record_check(
        "ws_recv", url, f"received exactly {n_expected}B", "hit", evidence)
    if expect_sha256 is not None:
        want = expect_sha256.lower()
        ok = sha_hex == want
        session.record_check(
            "ws_sha256", url, f"sha256 == {want}", "hit" if ok else "miss",
            evidence)
        if not ok:
            raise RuntimeError(
                f"ws:recv sha256 mismatch: got {sha_hex}, expected {want}")
    if expect_crc32 is not None:
        want = expect_crc32 & 0xffffffff
        ok = (crc & 0xffffffff) == want
        session.record_check(
            "ws_crc32", url, f"crc32 == {_fmt_crc32(want)}",
            "hit" if ok else "miss", evidence)
        if not ok:
            raise RuntimeError(
                f"ws:recv crc32 mismatch: got {crc_hex}, "
                f"expected {_fmt_crc32(want)}")


class WsPlugin(DevicePlugin):
    name = "ws"
    doc = (
        "Plain WebSocket receive tests for network-attached firmware. "
        "No SSH and no TLS required for ws:// URLs. The recv op verifies "
        "exact byte count, optional SHA-256/CRC32, timeout, and minimum "
        "receive rate.")

    ops = {
        "recv": Op(
            args={"bytes": "int", "timeout_ms": "int"},
            optional_args={
                "url": "str",
                "expect_sha256": "str",
                "expect_crc32": "int",
                "min_rate_Bps": "int",
                "stream": "bool",
            },
            doc=("Receive exactly bytes from a WebSocket URL or configured "
                 "ws instance URL. Incrementally computes sha256 and crc32; "
                 "optional expect_sha256/expect_crc32 verify contents; "
                 "min_rate_Bps verifies transfer speed; stream=true stores "
                 "payload bytes in ws.recv."),
            run=_op_recv),
    }

    def probe(self):
        out = [{"id": "any", "list_only": True,
                "description": "WebSocket URL pseudo-device"}]
        for inst in config.instances(self.name):
            spec = dict(inst)
            spec.setdefault("id", "target")
            out.append(spec)
        return out

    def open(self, spec):
        return WsHandle(spec)

    def close(self, handle):
        pass
