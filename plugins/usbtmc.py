# SPDX-License-Identifier: MIT
# usbtmc.py --- Linux USBTMC class-driver access (/dev/usbtmcN)
# Copyright (c) 2026 Jakob Kastelic

import contextlib
import glob
import hashlib
import json
import os
import select
import time
import zlib

import config
from plugin import DevicePlugin, Op


MAX_QUERY_READ = 16 * 1024 * 1024
MAX_BULK_XFER = 512 * 1024 * 1024
DEFAULT_CHUNK = 1 << 20


def _fmt_crc32(v):
    return f"0x{v & 0xffffffff:08x}"


def _sys_read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _node_info(node):
    name = os.path.basename(node)
    base = f"/sys/class/usbmisc/{name}/device"
    usbdev = os.path.realpath(base)
    # Interface symlink usually points to .../<usbdev>:1.N. The parent
    # device directory carries VID/PID/strings.
    parent = os.path.dirname(usbdev)
    return {
        "node": node,
        "id": name.replace("usbtmc", "") or name,
        "vid": _sys_read(os.path.join(parent, "idVendor")),
        "pid": _sys_read(os.path.join(parent, "idProduct")),
        "serial": _sys_read(os.path.join(parent, "serial")),
        "manufacturer": _sys_read(os.path.join(parent, "manufacturer")),
        "product": _sys_read(os.path.join(parent, "product")),
        "interface": os.path.basename(usbdev),
    }


def _all_nodes():
    return sorted(glob.glob("/dev/usbtmc*"))


def _sysfs_hex_int(val):
    if val is None or val == "":
        return None
    return int(str(val), 16)


def _config_usb_id_int(val):
    if isinstance(val, int):
        return val
    s = str(val)
    if s.lower().startswith("0x"):
        return int(s, 0)
    return int(s, 16)


def _info_matches(info, inst):
    want_vid = inst.get("usb_vid") or inst.get("vid")
    want_pid = inst.get("usb_pid") or inst.get("pid")
    want_serial = inst.get("usb_serial") or inst.get("serial")
    if want_vid and _sysfs_hex_int(info["vid"]) != _config_usb_id_int(want_vid):
        return False
    if want_pid and _sysfs_hex_int(info["pid"]) != _config_usb_id_int(want_pid):
        return False
    if want_serial and info["serial"] != str(want_serial):
        return False
    return True


def _match_instance(inst):
    if inst.get("node") and os.path.exists(inst["node"]):
        info = _node_info(inst["node"])
        if _info_matches(info, inst):
            return info
        return None
    for node in _all_nodes():
        info = _node_info(node)
        if not _info_matches(info, inst):
            continue
        return info
    return None


def _selector_args(args):
    sel = {k: args.get(k) for k in ("vid", "pid", "serial")}
    if all(v is None for v in sel.values()):
        return None
    return sel


def _match_selected(args):
    sel = _selector_args(args)
    if sel is None:
        return None
    matches = [
        info for info in (_node_info(n) for n in _all_nodes())
        if _info_matches(info, sel)
    ]
    if not matches:
        raise RuntimeError(
            "usbtmc selector matched no devices "
            f"(vid={sel['vid']!r} pid={sel['pid']!r} "
            f"serial={sel['serial']!r})")
    if len(matches) > 1:
        nodes = sorted(m["node"] for m in matches)
        raise RuntimeError(
            "usbtmc selector is ambiguous; add vid/pid/serial. "
            f"matches={nodes}")
    return matches[0]


@contextlib.contextmanager
def _fd_for(h, args):
    """Yield ``(fd, ident)`` for an op.

    A concrete handle (usbtmc.<id>) already holds an open fd; selectors
    are ignored, matching usb.any. The usbtmc.any pseudo-device has no
    fd, so vid/pid/serial selectors must resolve to exactly one node,
    which is opened for the duration of the op and closed afterwards.
    """
    if h.fd is not None:
        yield h.fd, h.spec.get("id", h.spec.get("node"))
        return
    info = _match_selected(args)
    if info is None:
        raise RuntimeError(
            "usbtmc.any ops require selectors (vid=0x1234 pid=0xabcd "
            "and optional serial=\"...\") that match exactly one device; "
            "use usbtmc.<id> for a configured/enumerated instance")
    fd = os.open(info["node"], os.O_RDWR | os.O_NONBLOCK)
    try:
        yield fd, info.get("serial") or info["node"]
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _encode_text(s):
    return str(s).encode()


def _check_bounds(op_name, length, limit):
    if length < 0 or length > limit:
        raise ValueError(f"{op_name} length out of range: {length}")


def _check_chunk_size(op_name, chunk_size):
    if chunk_size <= 0 or chunk_size > MAX_QUERY_READ:
        raise ValueError(f"{op_name} bad chunk_size: {chunk_size}")


def _check_min_rate(op_name, total, elapsed_s, min_rate_Bps):
    if not min_rate_Bps or total == 0:
        return
    if elapsed_s <= 0:
        return
    rate = total / elapsed_s
    if rate < min_rate_Bps:
        raise TimeoutError(
            f"{op_name} too slow: {rate:.0f} B/s < {min_rate_Bps} B/s")


def _read_with_timeout(fd, length, timeout_ms, chunk_size=DEFAULT_CHUNK,
                       session=None):
    _check_chunk_size("usbtmc:read", chunk_size)
    deadline = time.monotonic() + timeout_ms / 1000.0
    chunks = []
    total = 0
    while total < length:
        if session is not None:
            session.bail_if_canceled(f"usbtmc:read {total}/{length}B")
        remain = max(0.0, deadline - time.monotonic())
        if remain <= 0:
            break
        try:
            data = os.read(fd, min(length - total, chunk_size))
        except (BlockingIOError, InterruptedError):
            time.sleep(min(0.01, remain))
            continue
        if not data:
            break
        chunks.append(data)
        total += len(data)
        if len(data) < chunk_size:
            break
    return b"".join(chunks)


def _read_loop(session, fd, length, timeout_ms, chunk_size=DEFAULT_CHUNK,
               stream=None, hashing=False):
    """Read up to ``length`` bytes from ``fd`` within the timeout.

    When ``stream`` is given each chunk is appended to it. When
    ``hashing`` is set the bytes are folded into a running sha256/crc32
    and otherwise discarded, so GB-scale transfers can be verified
    byte-perfectly without buffering the payload. Returns
    ``(total, elapsed_s, sha_or_None, crc)``.
    """
    _check_chunk_size("usbtmc:read", chunk_size)
    deadline = time.monotonic() + timeout_ms / 1000.0
    total = 0
    sha = hashlib.sha256() if hashing else None
    crc = 0
    t0 = time.monotonic()
    while total < length:
        session.bail_if_canceled(f"usbtmc:read {total}/{length}B")
        remain = max(0.0, deadline - time.monotonic())
        if remain <= 0:
            break
        try:
            data = os.read(fd, min(length - total, chunk_size))
        except (BlockingIOError, InterruptedError):
            session.cancel_event.wait(min(0.01, remain))
            continue
        if not data:
            break
        if stream is not None:
            stream.append(data)
        if sha is not None:
            sha.update(data)
            crc = zlib.crc32(data, crc)
        total += len(data)
    return total, time.monotonic() - t0, sha, crc


def _write_all(fd, data, timeout_ms, session=None, chunk_size=DEFAULT_CHUNK):
    _check_chunk_size("usbtmc:write", chunk_size)
    deadline = time.monotonic() + timeout_ms / 1000.0
    total = 0
    view = memoryview(data)
    t0 = time.monotonic()
    while total < len(data):
        if session is not None:
            session.bail_if_canceled(
                f"usbtmc:write {total}/{len(data)}B")
        remain = max(0.0, deadline - time.monotonic())
        if remain <= 0:
            break
        _r, w, _x = select.select([], [fd], [], min(0.2, remain))
        if not w:
            continue
        try:
            n = os.write(fd, view[total:total + chunk_size])
        except (BlockingIOError, InterruptedError):
            continue
        if n <= 0:
            break
        total += n
    return total, time.monotonic() - t0


class UsbTmcHandle:
    def __init__(self, spec):
        self.spec = spec
        self.fd = None


def _op_list(session, h, args):
    devs = [_node_info(n) for n in _all_nodes()]
    session.stream("usbtmc.list").append(
        json.dumps(devs, indent=2, sort_keys=True).encode() + b"\n")
    session.log_event("USBTMC", "usbtmc:list", f"{len(devs)} device(s)")


def _op_write(session, h, args):
    data = _encode_text(args["data"])
    session.bail_if_canceled("usbtmc:write")
    timeout_ms = args.get("timeout_ms") or 1000
    with _fd_for(h, args) as (fd, _ident):
        written, elapsed = _write_all(fd, data, timeout_ms, session=session)
    if written != len(data):
        raise TimeoutError(
            f"usbtmc:write timed out after {written}/{len(data)}B")
    session.log_event("USBTMC", "usbtmc:write",
                      f"{written}B in {elapsed:.3f}s")


def _op_write_blob(session, h, args):
    data = bytes(args["data"])
    _check_bounds("usbtmc:write_blob", len(data), MAX_BULK_XFER)
    timeout_ms = args.get("timeout_ms") or 30000
    chunk_size = args.get("chunk_size") or DEFAULT_CHUNK
    min_rate_Bps = args.get("min_rate_Bps")
    with _fd_for(h, args) as (fd, _ident):
        written, elapsed = _write_all(
            fd, data, timeout_ms, session=session, chunk_size=chunk_size)
    if written != len(data):
        raise TimeoutError(
            f"usbtmc:write_blob timed out after {written}/{len(data)}B")
    _check_min_rate("usbtmc:write_blob", written, elapsed, min_rate_Bps)
    mbps = (written / elapsed / (1024 * 1024)) if elapsed > 0 else 0.0
    session.log_event("USBTMC", "usbtmc:write_blob",
                      f"{written}B in {elapsed:.3f}s @ {mbps:.2f} MiB/s")


def _op_read(session, h, args):
    length = args["length"]
    timeout_ms = args.get("timeout_ms") or 30000
    chunk_size = args.get("chunk_size") or DEFAULT_CHUNK
    min_rate_Bps = args.get("min_rate_Bps")
    exact = bool(args.get("exact") or False)
    expect_sha256 = args.get("expect_sha256")
    expect_crc32 = args.get("expect_crc32")
    verify = expect_sha256 is not None or expect_crc32 is not None
    # Verify mode hashes-and-discards, so it has no buffering ceiling and
    # lifts the stored-read cap to allow byte-perfect GB-scale checks.
    if length < 0 or (not verify and length > MAX_BULK_XFER):
        raise ValueError(f"usbtmc:read length out of range: {length}")
    with _fd_for(h, args) as (fd, ident):
        got, elapsed, sha, crc = _read_loop(
            session, fd, length, timeout_ms, chunk_size,
            stream=None if verify else session.stream("usbtmc.read"),
            hashing=verify)
    if exact and got != length:
        raise TimeoutError(
            f"usbtmc:read expected {length}B, got {got}B")
    _check_min_rate("usbtmc:read", got, elapsed, min_rate_Bps)
    mbps = (got / elapsed / (1024 * 1024)) if elapsed > 0 else 0.0
    if verify:
        sha_hex = sha.hexdigest()
        crc_hex = _fmt_crc32(crc)
        evidence = {"bytes": got, "elapsed_s": elapsed,
                    "sha256": sha_hex, "crc32": crc_hex}
        session.log_event(
            "USBTMC", "usbtmc:read",
            f"{got}B in {elapsed:.3f}s @ {mbps:.2f} MiB/s "
            f"sha256={sha_hex} crc32={crc_hex}")
        if expect_sha256 is not None:
            want = expect_sha256.lower()
            ok = sha_hex == want
            session.record_check(
                "usbtmc_read_sha256", ident, f"sha256 == {want}",
                "hit" if ok else "miss", evidence)
            if not ok:
                raise RuntimeError(
                    f"usbtmc:read sha256 mismatch: got {sha_hex}, "
                    f"expected {want}")
        if expect_crc32 is not None:
            want = expect_crc32 & 0xffffffff
            ok = (crc & 0xffffffff) == want
            session.record_check(
                "usbtmc_read_crc32", ident, f"crc32 == {_fmt_crc32(want)}",
                "hit" if ok else "miss", evidence)
            if not ok:
                raise RuntimeError(
                    f"usbtmc:read crc32 mismatch: got {crc_hex}, "
                    f"expected {_fmt_crc32(want)}")
        return
    session.log_event("USBTMC", "usbtmc:read",
                      f"{got}B in {elapsed:.3f}s @ {mbps:.2f} MiB/s")


def _query_bytes(session, fd, cmd, length, timeout_ms):
    data = _encode_text(cmd)
    written, _elapsed = _write_all(
        fd, data, timeout_ms, session=session, chunk_size=len(data) or 1)
    if written != len(data):
        raise TimeoutError(
            f"usbtmc query write timed out after {written}/{len(data)}B")
    return _read_with_timeout(fd, length, timeout_ms, session=session)


def _op_query(session, h, args):
    length = args.get("length") or 4096
    _check_bounds("usbtmc:query", length, MAX_QUERY_READ)
    timeout_ms = args.get("timeout_ms") or 1000
    session.bail_if_canceled("usbtmc:query")
    with _fd_for(h, args) as (fd, _ident):
        data = _query_bytes(session, fd, args["data"], length, timeout_ms)
    session.stream("usbtmc.query").append(data)
    session.log_event("USBTMC", "usbtmc:query", f"{len(data)}B")


def _op_expect(session, h, args):
    length = args.get("length") or 4096
    _check_bounds("usbtmc:expect", length, MAX_QUERY_READ)
    timeout_ms = args.get("timeout_ms") or 1000
    sentinel = _encode_text(args["sentinel"])
    session.bail_if_canceled("usbtmc:expect")
    with _fd_for(h, args) as (fd, ident):
        data = _query_bytes(session, fd, args["data"], length, timeout_ms)
    session.stream("usbtmc.expect").append(data)
    hit = sentinel in data
    session.record_check(
        "usbtmc_expect", ident,
        f"response contains {args['sentinel']!r}",
        "hit" if hit else "miss",
        {"response_len": len(data)})
    if not hit:
        raise TimeoutError(
            f"usbtmc:expect missing {args['sentinel']!r} in "
            f"{len(data)}B response")


def _op_identify(session, h, args):
    timeout_ms = args.get("timeout_ms") or 1000
    with _fd_for(h, args) as (fd, ident):
        data = _query_bytes(session, fd, "*IDN?\n", 4096, timeout_ms)
    session.stream("usbtmc.idn").append(data)
    if args.get("expect") is not None:
        want = _encode_text(args["expect"])
        hit = want in data
        session.record_check(
            "usbtmc_idn", ident,
            f"*IDN? contains {args['expect']!r}",
            "hit" if hit else "miss",
            {"response": data.decode("utf-8", "replace")})
        if not hit:
            raise RuntimeError(
                f"usbtmc:identify expected {args['expect']!r}, got "
                f"{data.decode('utf-8', 'replace')!r}")


def _op_clear(session, h, args):
    # Linux usbtmc has class-specific ioctl support, but the constants
    # vary across old downstream kernels. Keep this op portable: drain
    # currently readable bytes so the next query starts from a clean
    # userspace buffer. If the device supports SCPI, plans can also send
    # "*CLS\n" with usbtmc:write.
    timeout_ms = args.get("timeout_ms") or 100
    drained = 0
    while True:
        r, _w, _x = select.select([h.fd], [], [], timeout_ms / 1000.0)
        if not r:
            break
        try:
            data = os.read(h.fd, 65536)
        except (BlockingIOError, InterruptedError):
            continue
        if not data:
            break
        drained += len(data)
        timeout_ms = 0
    session.log_event("USBTMC", "usbtmc:clear", f"drained {drained}B")


class UsbTmcPlugin(DevicePlugin):
    name = "usbtmc"
    doc = (
        "USB Test & Measurement Class via Linux /dev/usbtmcN. Use this "
        "when the gadget descriptors bind to the kernel usbtmc driver. "
        "Ops are SCPI-friendly write/read/query/expect plus identify. "
        "usbtmc.any is always present: use usbtmc.any:list to discover "
        "nodes, then pass vid=0x1234 pid=0xabcd and optional "
        "serial=\"...\" to any op to target exactly one device by serial "
        "instead of usbtmc.0/.1. Selectors must match exactly one device.")

    ops = {
        "list": Op(args={}, doc="List /dev/usbtmc* devices as JSON.",
                   run=_op_list),
        "identify": Op(
            args={},
            optional_args={"expect": "str", "timeout_ms": "int",
                           "vid": "int", "pid": "int", "serial": "str"},
            doc=("Send '*IDN?\\n' and append response to usbtmc.idn. On "
                 "usbtmc.any pass vid=0x1234 pid=0xabcd and optional "
                 "serial=\"...\" to target exactly one device."),
            run=_op_identify),
        "write": Op(args={"data": "str"},
                    optional_args={"timeout_ms": "int",
                                   "vid": "int", "pid": "int",
                                   "serial": "str"},
                    doc=("Write text bytes; escapes are decoded by plan "
                         "parser. On usbtmc.any pass vid/pid/serial "
                         "selectors to target exactly one device."),
                    run=_op_write),
        "write_blob": Op(
            args={"data": "blob"},
            optional_args={"timeout_ms": "int", "chunk_size": "int",
                           "min_rate_Bps": "int",
                           "vid": "int", "pid": "int", "serial": "str"},
            doc=("Write blob bytes using large chunks. Use for throughput "
                 "tests; min_rate_Bps can enforce USB high-speed-class "
                 "performance. On usbtmc.any pass vid/pid/serial selectors "
                 "to target exactly one device."),
            run=_op_write_blob),
        "read": Op(
            args={"length": "int"},
            optional_args={"timeout_ms": "int", "chunk_size": "int",
                           "min_rate_Bps": "int", "exact": "bool",
                           "expect_sha256": "str", "expect_crc32": "int",
                           "vid": "int", "pid": "int", "serial": "str"},
            doc=("Read up to length bytes into usbtmc.read using large "
                 "chunks. Use exact=true and min_rate_Bps for fast "
                 "bulk-transfer checks. Passing expect_sha256 and/or "
                 "expect_crc32 switches to streaming-verify mode: bytes "
                 "are hashed and discarded (not stored), so the 512 MiB "
                 "cap is lifted for byte-perfect GB-scale checks. On "
                 "usbtmc.any pass vid/pid/serial selectors to target "
                 "exactly one device."),
            run=_op_read),
        "query": Op(
            args={"data": "str"},
            optional_args={"length": "int", "timeout_ms": "int",
                           "vid": "int", "pid": "int", "serial": "str"},
            doc=("Write command then read response into usbtmc.query. On "
                 "usbtmc.any pass vid/pid/serial selectors to target "
                 "exactly one device."),
            run=_op_query),
        "expect": Op(
            args={"data": "str", "sentinel": "str"},
            optional_args={"length": "int", "timeout_ms": "int",
                           "vid": "int", "pid": "int", "serial": "str"},
            doc=("Query and require the response to contain sentinel. On "
                 "usbtmc.any pass vid/pid/serial selectors to target "
                 "exactly one device."),
            run=_op_expect),
        "clear": Op(
            args={},
            optional_args={"timeout_ms": "int"},
            doc="Drain currently pending readable bytes.",
            run=_op_clear),
    }

    def probe(self):
        out = [{"id": "any", "list_only": True,
                "description": "USBTMC enumeration pseudo-device"}]
        configured = list(config.instances(self.name))
        if configured:
            for inst in configured:
                info = _match_instance(inst)
                if not info:
                    continue
                spec = dict(inst)
                spec.update(info)
                spec.setdefault("id", info["id"])
                out.append(spec)
            return out
        # No config: expose current class-driver nodes directly so a
        # bring-up bench can try usbtmc.0:identify immediately.
        out.extend(_node_info(n) for n in _all_nodes())
        return out

    def open(self, spec):
        if spec.get("list_only"):
            return UsbTmcHandle(spec)
        info = _match_instance(spec) if (
            spec.get("usb_vid") or spec.get("vid") or spec.get("node")
        ) else (spec if os.path.exists(spec.get("node", "")) else None)
        if info is None:
            raise RuntimeError(
                f"usbtmc.{spec.get('id', '?')}: device not present")
        h = UsbTmcHandle({**spec, **info})
        h.fd = os.open(info["node"], os.O_RDWR | os.O_NONBLOCK)
        return h

    def close(self, handle):
        if handle.fd is not None:
            try:
                os.close(handle.fd)
            except OSError:
                pass
            handle.fd = None
