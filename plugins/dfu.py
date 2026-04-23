# SPDX-License-Identifier: MIT
# dfu.py --- STM32 DFU bootloader programming via STM32_Programmer_CLI
# Copyright (c) 2026 Jakob Kastelic

import os
import re
import shutil
import subprocess
import tempfile

import config
from plugin import DevicePlugin, Op
from . import _usb


CUBEPROG_EXE_DEFAULT = (
    "C:\\Program Files\\STMicroelectronics\\STM32Cube\\"
    "STM32CubeProgrammer\\bin\\STM32_Programmer_CLI.exe"
)
FLASH_TIMEOUT_S = 600
LIST_TIMEOUT_S = 30

# Accept only safe blob-name characters -- no path separators, no '..',
# no shell metacharacters. Applied to every @blob reference inside a
# flashlayout TSV and to each filename written into the temp staging
# directory.
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# --- shared helpers ---

def _run_cubeprog(exe, argv_tail, timeout_s):
    argv = [exe] + list(argv_tail)
    try:
        return subprocess.run(argv, capture_output=True,
                              timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"cubeprog timed out after {timeout_s}s")


def _parse_list_output(stdout):
    """Extract ``(usb_index, serial)`` tuples from ``cubeprog -l usb``."""
    devs = []
    cur = {}
    for raw in stdout.splitlines():
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
        m = re.search(r"Device Index\s*:\s*(USB\d+)", line)
        if m:
            if cur:
                devs.append(cur)
            cur = {"usb_index": m.group(1)}
            continue
        m = re.search(r"Serial number\s*:\s*([0-9A-Fa-f]+)", line)
        if m:
            cur["serial"] = m.group(1)
    if cur:
        devs.append(cur)
    return devs


# --- op: list ---

def _op_list(session, h, args):
    result = _run_cubeprog(h.cubeprog_exe, ["-l", "usb"], LIST_TIMEOUT_S)
    if result.stdout:
        session.stream("dfu.list").append(result.stdout)
    if result.stderr:
        session.stream("dfu.list.stderr").append(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"cubeprog -l usb exit={result.returncode}; "
            f"see dfu.list.stderr stream")
    devs = _parse_list_output(result.stdout or b"")
    session.log_event("DFU", "dfu:list", f"found {len(devs)} DFU device(s)")
    for d in devs:
        session.log_event(
            "DFU", "dfu:list",
            f"  {d.get('usb_index', '?')} serial={d.get('serial', '?')}")
    return devs


# --- op: flash_layout ---

def _rewrite_tsv(tsv_text, blobs, staging_dir):
    """Validate + rewrite the flashlayout so Binary cells point to files
    in ``staging_dir``.  Raises ValueError on any unsafe reference.

    Contract: each non-empty, non-``none`` Binary cell MUST be
    ``@blobname`` where ``blobname`` is both a safe identifier and
    present in ``blobs``.  Plain filenames, absolute paths, and
    traversal strings are rejected outright -- we do NOT want cubeprog
    reading /etc/passwd for an attacker who can craft a TSV.
    """
    out_lines = []
    needed = {}     # blob_name -> (staged_path, bytes)
    for lineno, raw in enumerate(tsv_text.splitlines(), 1):
        body = raw.split("#", 1)[0].strip()
        if not body:
            out_lines.append(raw)
            continue
        cols = body.split()
        if len(cols) < 7:
            raise ValueError(
                f"flashlayout line {lineno}: expected >=7 columns, "
                f"got {len(cols)}: {raw!r}")
        binary = cols[6]
        if binary in ("none", "-", ""):
            out_lines.append(raw)
            continue
        if not binary.startswith("@"):
            raise ValueError(
                f"flashlayout line {lineno}: Binary column must be "
                f"@blobname (plan blob reference), got {binary!r}; "
                f"filesystem paths are rejected for safety")
        name = binary[1:]
        if not SAFE_NAME_RE.match(name):
            raise ValueError(
                f"flashlayout line {lineno}: unsafe blob name {name!r}")
        if name not in blobs:
            raise ValueError(
                f"flashlayout line {lineno}: blob @{name} not in plan tar")
        staged = os.path.join(staging_dir, name)
        needed[name] = (staged, blobs[name])
        # Rebuild the row with the staged path in column 7. Keep
        # whitespace unspecified -- cubeprog only requires >=1
        # whitespace between columns, and tabs are equivalent.
        new_cols = cols[:6] + [staged] + cols[7:]
        out_lines.append("\t".join(new_cols))
    return "\n".join(out_lines) + "\n", needed


def _op_flash_layout(session, h, args):
    tsv_bytes = args["layout"]
    no_reconnect = bool(args.get("no_reconnect"))
    try:
        tsv_text = tsv_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"flashlayout is not UTF-8: {e}")

    staging = tempfile.mkdtemp(prefix=f"dfu-{h.instance_id}-", dir=h.tmpdir)
    try:
        plan_blobs = session.plan.blobs
        rewritten, needed = _rewrite_tsv(tsv_text, plan_blobs, staging)

        for name, (path, data) in needed.items():
            with open(path, "wb") as f:
                f.write(data)

        tsv_path = os.path.join(staging, "flashlayout.tsv")
        with open(tsv_path, "w", encoding="utf-8") as f:
            f.write(rewritten)

        argv = ["-c", f"port={h.usb_index}", "-w", tsv_path]
        session.log_event(
            "DFU", "dfu:flash_layout",
            f"cubeprog -c port={h.usb_index} -w <{len(needed)} blob(s) "
            f"staged in {staging}> no_reconnect={no_reconnect}")

        if no_reconnect:
            _run_with_early_kill(
                session, h.cubeprog_exe, argv, FLASH_TIMEOUT_S)
        else:
            result = _run_cubeprog(h.cubeprog_exe, argv, FLASH_TIMEOUT_S)
            if result.stdout:
                session.stream("dfu.flash.stdout").append(result.stdout)
            if result.stderr:
                session.stream("dfu.flash.stderr").append(result.stderr)
            if result.returncode != 0:
                raise RuntimeError(
                    f"cubeprog flash exit={result.returncode}; see "
                    f"dfu.flash.stderr stream")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _run_with_early_kill(session, exe, argv_tail, timeout_s):
    """Run cubeprog, tee stdout to the stream as it arrives, and
    terminate as soon as the target has been handed control.

    Cubeprog's TSV flow always loops at the end trying to reconnect
    to the target (so it can chain further partitions). When the
    target is a SYSRAM-resident image that never re-enters DFU, this
    loop is pure dead time (~30 s on MP135). Kill cubeprog once we
    observe "Reconnecting the device" on stdout; declare success iff
    "RUNNING Program" was seen before then.

    Runs a single reader thread draining stdout line-by-line; stderr
    is drained post-exit via communicate() on the kill path.
    """
    import subprocess
    import threading

    argv = [exe] + list(argv_tail)
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

    seen_running = [False]
    seen_reconnect = [False]
    reader_err = []
    deadline = time.monotonic() + timeout_s

    stream_out = session.stream("dfu.flash.stdout")

    def _reader():
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    return
                stream_out.append(line)
                if b"RUNNING Program" in line:
                    seen_running[0] = True
                if b"Reconnecting the device" in line:
                    seen_reconnect[0] = True
                    return
        except Exception as e:
            reader_err.append(repr(e))

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    # Wait for either the reader to signal reconnect, for cubeprog to
    # exit on its own, or for the overall timeout to expire.
    while time.monotonic() < deadline:
        if seen_reconnect[0]:
            break
        if proc.poll() is not None:
            break
        time.sleep(0.05)

    # Kill path: give cubeprog a chance to exit cleanly, then escalate.
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    # Drain whatever's left on both pipes.
    try:
        _rest_stdout, rest_stderr = proc.communicate(timeout=5)
    except Exception:
        _rest_stdout, rest_stderr = (b"", b"")
    if _rest_stdout:
        stream_out.append(_rest_stdout)
    if rest_stderr:
        session.stream("dfu.flash.stderr").append(rest_stderr)
    t.join(timeout=2)

    if reader_err:
        session.log_event("DFU", "dfu:flash_layout",
                          f"reader errors: {reader_err}")

    if seen_reconnect[0] and not seen_running[0]:
        raise RuntimeError(
            "cubeprog reached reconnect phase without RUNNING Program "
            "-- flash did not complete the launch step")
    if not seen_running[0]:
        raise RuntimeError(
            f"cubeprog exited rc={proc.returncode} before RUNNING "
            f"Program; see dfu.flash.stdout/stderr streams")
    session.log_event(
        "DFU", "dfu:flash_layout",
        f"no_reconnect: early-killed cubeprog after RUNNING Program")


# --- op: flash (single file) ---

def _op_flash(session, h, args):
    image = args["image"]
    address = args["address"]

    tmp = tempfile.NamedTemporaryFile(
        prefix=f"dfu-{h.instance_id}-", suffix=".bin",
        delete=False, dir=h.tmpdir)
    try:
        tmp.write(image)
        tmp.flush()
        tmp.close()

        argv = ["-c", f"port={h.usb_index}",
                "-w", tmp.name, f"0x{address:08x}", "-v"]
        session.log_event(
            "DFU", "dfu:flash",
            f"cubeprog -c port={h.usb_index} -w <{len(image)}B> "
            f"0x{address:08x}")
        result = _run_cubeprog(h.cubeprog_exe, argv, FLASH_TIMEOUT_S)
        if result.stdout:
            session.stream("dfu.stdout").append(result.stdout)
        if result.stderr:
            session.stream("dfu.stderr").append(result.stderr)
        if result.returncode != 0:
            raise RuntimeError(
                f"cubeprog exit={result.returncode}; see dfu.stderr stream")
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass


# --- plugin ---

class DfuHandle:
    def __init__(self, instance_id, cubeprog_exe, usb_index, tmpdir,
                 expected_serial):
        self.instance_id = instance_id
        self.cubeprog_exe = cubeprog_exe
        self.usb_index = usb_index
        self.tmpdir = tmpdir
        self.expected_serial = expected_serial


class DfuPlugin(DevicePlugin):
    name = "dfu"
    doc = ("STM32 DFU bootloader programming via STM32_Programmer_CLI. "
           "Ops: list (enumerate DFU devices), flash (single file to "
           "address), flash_layout (TSV-described multi-partition, each "
           "row's Binary column is a plan @blob reference).  Binary path "
           "is bench-owned; attacker-controllable inputs are limited to "
           "typed plan args and plan blobs.")

    ops = {
        "list": Op(args={},
                   doc="Run cubeprog -l usb; append output to dfu.list "
                       "stream; log parsed device list.",
                   run=_op_list),
        "flash": Op(args={"image": "blob", "address": "int"},
                    doc="Single-file flash to 0xADDRESS. Verify on.",
                    run=_op_flash),
        "flash_layout": Op(
            args={"layout": "blob"},
            optional_args={"no_reconnect": "bool"},
            doc=("Multi-partition flash from a TSV flashlayout (see "
                 "STM32CubeProgrammer docs).  Every Binary column must "
                 "be @blobname; filesystem paths are rejected. "
                 "no_reconnect=true kills cubeprog once "
                 "'Reconnecting the device' appears on stdout -- "
                 "saves ~30 s per iteration when loading a "
                 "SYSRAM-resident image that never re-enters DFU. "
                 "Success requires 'RUNNING Program' seen first."),
            run=_op_flash_layout),
    }

    def probe(self):
        section = config.section(self.name)
        cubeprog = section.get("cubeprog_exe", CUBEPROG_EXE_DEFAULT)
        if not os.path.exists(cubeprog):
            return []

        # pyusb/libusb cannot see WinUSB-bound STM32 DFU devices without
        # an extra filter driver, so pyusb-based presence checks have
        # too many false negatives to be useful. Instead ask cubeprog
        # itself which DFU devices are currently enumerated -- that is
        # the single authoritative source, takes ~1 s, and is
        # side-effect-free (pure USB listing).
        try:
            result = _run_cubeprog(cubeprog, ["-l", "usb"], LIST_TIMEOUT_S)
        except Exception:
            return []
        if result.returncode != 0:
            return []
        enumerated = _parse_list_output(result.stdout or b"")
        serials = [d.get("serial", "") for d in enumerated]

        out = []
        for i, inst in enumerate(section.get("instances", []) or []):
            expect_serial = inst.get("usb_serial")
            # When the config pins an iSerial, filter by it -- catches
            # "two MP135 boards plugged in" and also "DFU mode not
            # entered yet, no device enumerated" in one check.
            # When no iSerial is pinned, match any DFU device (useful
            # during first bring-up before the exact serial is known).
            if expect_serial:
                if not any(expect_serial in s for s in serials):
                    continue
            elif not enumerated:
                continue
            out.append({
                "id": inst.get("id", f"{i}"),
                "cubeprog_exe": cubeprog,
                "usb_index": inst.get("usb_index", "usb1"),
                "usb_serial": expect_serial,
            })
        return out

    def open(self, spec):
        tmpdir = os.environ.get(
            "TEST_SERV_DFU_TMPDIR", tempfile.gettempdir())
        h = DfuHandle(
            instance_id=spec["id"],
            cubeprog_exe=spec["cubeprog_exe"],
            usb_index=spec["usb_index"],
            tmpdir=tmpdir,
            expected_serial=spec.get("usb_serial"),
        )

        # Identity handshake: run `cubeprog -l usb`, parse out the
        # enumerated DFU devices, and confirm one of them reports the
        # expected USB serial. Proves both that the bench has the right
        # physical board attached AND that cubeprog is working before
        # we attempt a long flash.
        if h.expected_serial:
            result = _run_cubeprog(
                h.cubeprog_exe, ["-l", "usb"], LIST_TIMEOUT_S)
            if result.returncode != 0:
                raise RuntimeError(
                    f"dfu: cubeprog -l usb exit={result.returncode}: "
                    f"{(result.stderr or b'').decode(errors='replace')!r}")
            devs = _parse_list_output(result.stdout or b"")
            serials = [d.get("serial", "") for d in devs]
            if not any(h.expected_serial in s for s in serials):
                raise RuntimeError(
                    f"dfu identity mismatch: expected serial "
                    f"{h.expected_serial!r}, enumerated {serials!r}")
            h._identity_verified = True
        return h

    def close(self, handle):
        pass
