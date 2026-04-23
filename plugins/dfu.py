# SPDX-License-Identifier: MIT
# dfu.py --- STM32 DFU bootloader programming via STM32_Programmer_CLI
# Copyright (c) 2026 Jakob Kastelic

import os
import subprocess
import tempfile

import config
from plugin import DevicePlugin, Op
from . import _usb


CUBEPROG_EXE_DEFAULT = (
    "C:\\Program Files\\STMicroelectronics\\STM32Cube\\"
    "STM32CubeProgrammer\\bin\\STM32_Programmer_CLI.exe"
)
FLASH_TIMEOUT_S = 180


# --- ops ---

def _op_flash(session, h, args):
    image = args["image"]
    address = args["address"]

    # Write the blob to a host-side temp file so cubeprog can read it.
    # The file never leaves tempdir and is deleted in the finally block.
    # (Path is not attacker-supplied; only the bytes are.)
    tmp = tempfile.NamedTemporaryFile(
        prefix=f"dfu-{h.instance_id}-", suffix=".bin",
        delete=False, dir=h.tmpdir)
    try:
        tmp.write(image)
        tmp.flush()
        tmp.close()

        argv = [
            h.cubeprog_exe,
            "-c", f"port=USB{h.usb_index}",
            "-d", tmp.name, f"0x{address:08x}",
            "-v",
        ]
        session.log_event("DFU", "dfu:flash",
                          f"cubeprog -c port=USB{h.usb_index} "
                          f"-d <{len(image)}B> 0x{address:08x}")
        try:
            result = subprocess.run(
                argv, capture_output=True,
                timeout=FLASH_TIMEOUT_S, check=False,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError(
                f"cubeprog timed out after {FLASH_TIMEOUT_S}s")
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
    def __init__(self, instance_id, cubeprog_exe, usb_index, tmpdir):
        self.instance_id = instance_id
        self.cubeprog_exe = cubeprog_exe
        self.usb_index = usb_index
        self.tmpdir = tmpdir


class DfuPlugin(DevicePlugin):
    name = "dfu"
    doc = ("STM32 DFU bootloader programming.  Wraps STMicro's "
           "STM32_Programmer_CLI.exe (cubeprog).  Binary path is taken "
           "from config.json; only attacker-controllable input is the "
           "blob bytes and the destination address, both typed.")

    ops = {
        "flash": Op(args={"image": "blob", "address": "int"},
                    doc=("Write image blob to 0xADDRESS on the target's "
                         "DFU-selected region and verify."),
                    run=_op_flash),
    }

    def probe(self):
        section = config.section(self.name)
        cubeprog = section.get("cubeprog_exe", CUBEPROG_EXE_DEFAULT)
        if not os.path.exists(cubeprog):
            return []
        out = []
        for i, inst in enumerate(section.get("instances", []) or []):
            vid = inst.get("usb_vid")
            pid = inst.get("usb_pid")
            serial = inst.get("usb_serial")
            # Side-effect-free USB enumeration via pyusb, if available.
            # When backend is unavailable (returns None), optimistically
            # advertise the instance and let open() fail fast if missing.
            present = _usb.winusb_device_present(vid, pid, serial)
            if present is False:
                continue
            out.append({
                "id": inst.get("id", f"{i}"),
                "cubeprog_exe": cubeprog,
                # cubeprog's port=USBN index. Starts at 1; in practice the
                # bench uses one DFU target so USB1. Override in config if
                # multiple DFU devices are attached.
                "usb_index": int(inst.get("usb_index", 1)),
                "usb_serial": serial,
            })
        return out

    def open(self, spec):
        tmpdir = os.environ.get(
            "TEST_SERV_DFU_TMPDIR",
            tempfile.gettempdir(),
        )
        return DfuHandle(
            instance_id=spec["id"],
            cubeprog_exe=spec["cubeprog_exe"],
            usb_index=spec["usb_index"],
            tmpdir=tmpdir,
        )

    def close(self, handle):
        pass
