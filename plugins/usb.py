# SPDX-License-Identifier: MIT
# usb.py --- Raw USB descriptor/control/bulk access via pyusb
# Copyright (c) 2026 Jakob Kastelic

import json

import config
from plugin import DevicePlugin, Op


MAX_XFER = 16 * 1024 * 1024


def _lazy_usb():
    import usb.core
    import usb.util
    return usb.core, usb.util


def _hex_bytes(data):
    return bytes(data).hex(" ")


def _decode_data_arg(data):
    if data is None:
        return b""
    return bytes(data)


def _dev_serial(dev):
    try:
        return dev.serial_number or ""
    except Exception:
        return ""


def _dev_product(dev):
    try:
        return dev.product or ""
    except Exception:
        return ""


def _dev_manufacturer(dev):
    try:
        return dev.manufacturer or ""
    except Exception:
        return ""


def _find_devices(vid=None, pid=None, serial=None):
    usb_core, _usb_util = _lazy_usb()
    kwargs = {}
    if vid is not None:
        kwargs["idVendor"] = config.as_int(vid)
    if pid is not None:
        kwargs["idProduct"] = config.as_int(pid)
    devs = list(usb_core.find(find_all=True, **kwargs) or [])
    if serial is not None:
        serial = str(serial)
        devs = [d for d in devs if _dev_serial(d) == serial]
    return devs


def _match_configured(inst):
    vid = inst.get("usb_vid") or inst.get("vid")
    pid = inst.get("usb_pid") or inst.get("pid")
    if vid is None or pid is None:
        return None
    serial = inst.get("usb_serial") or inst.get("serial")
    devs = _find_devices(vid=vid, pid=pid, serial=serial)
    return devs[0] if devs else None


def _selector_args(args):
    vid = args.get("vid")
    pid = args.get("pid")
    serial = args.get("serial")
    if vid is None and pid is None and serial is None:
        return None
    return vid, pid, serial


def _match_selected(args):
    selector = _selector_args(args)
    if selector is None:
        return None
    vid, pid, serial = selector
    devs = _find_devices(vid=vid, pid=pid, serial=serial)
    if not devs:
        raise RuntimeError(
            "usb.any selector matched no devices "
            f"(vid={vid!r} pid={pid!r} serial={serial!r})")
    if len(devs) > 1:
        recs = [_device_record(d) for d in devs]
        raise RuntimeError(
            "usb.any selector is ambiguous; add vid/pid/serial. "
            f"matches={json.dumps(recs, sort_keys=True)}")
    return devs[0]


def _device_record(dev):
    return {
        "bus": getattr(dev, "bus", None),
        "address": getattr(dev, "address", None),
        "vid": f"0x{dev.idVendor:04x}",
        "pid": f"0x{dev.idProduct:04x}",
        "manufacturer": _dev_manufacturer(dev),
        "product": _dev_product(dev),
        "serial": _dev_serial(dev),
    }


def _descriptor_record(dev):
    out = _device_record(dev)
    out["bcdUSB"] = f"0x{dev.bcdUSB:04x}"
    out["bDeviceClass"] = f"0x{dev.bDeviceClass:02x}"
    out["bDeviceSubClass"] = f"0x{dev.bDeviceSubClass:02x}"
    out["bDeviceProtocol"] = f"0x{dev.bDeviceProtocol:02x}"
    out["configurations"] = []
    for cfg in dev:
        cfg_rec = {
            "bConfigurationValue": cfg.bConfigurationValue,
            "interfaces": [],
        }
        for intf in cfg:
            intf_rec = {
                "bInterfaceNumber": intf.bInterfaceNumber,
                "bAlternateSetting": intf.bAlternateSetting,
                "bInterfaceClass": f"0x{intf.bInterfaceClass:02x}",
                "bInterfaceSubClass": f"0x{intf.bInterfaceSubClass:02x}",
                "bInterfaceProtocol": f"0x{intf.bInterfaceProtocol:02x}",
                "endpoints": [],
            }
            for ep in intf:
                intf_rec["endpoints"].append({
                    "bEndpointAddress": f"0x{ep.bEndpointAddress:02x}",
                    "bmAttributes": f"0x{ep.bmAttributes:02x}",
                    "wMaxPacketSize": ep.wMaxPacketSize,
                    "bInterval": ep.bInterval,
                })
            cfg_rec["interfaces"].append(intf_rec)
        out["configurations"].append(cfg_rec)
    return out


class UsbHandle:
    def __init__(self, spec, dev=None):
        self.spec = spec
        self.dev = dev
        self.claimed = []
        self.selected = []

    def claim(self, interface=None, detach=False, dev=None):
        dev = dev or self.dev
        if interface is None:
            interface = self.spec.get("interface")
        if interface is None:
            return
        interface = int(interface)
        usb_core, usb_util = _lazy_usb()
        if detach:
            try:
                if dev.is_kernel_driver_active(interface):
                    dev.detach_kernel_driver(interface)
            except (NotImplementedError, usb_core.USBError):
                pass
        usb_util.claim_interface(dev, interface)
        self.claimed.append((dev, interface))


def _require_dev(h, args=None):
    if h.dev is not None:
        return h.dev
    if args is not None:
        dev = _match_selected(args)
        if dev is not None:
            h.selected.append(dev)
            return dev
    if h.dev is None:
        raise RuntimeError(
            "usb.any descriptor/control/bulk ops require selectors "
            "(vid=0x1234 pid=0xabcd and optional serial=\"...\") that "
            "match exactly one device; configure a usb instance with "
            "usb_vid/usb_pid[/usb_serial] to omit selectors")


def _op_list(session, h, args):
    vid = args.get("vid")
    pid = args.get("pid")
    serial = args.get("serial")
    devs = [_device_record(d) for d in _find_devices(vid, pid, serial)]
    data = json.dumps(devs, indent=2, sort_keys=True).encode() + b"\n"
    session.stream("usb.list").append(data)
    session.log_event("USB", "usb:list", f"{len(devs)} device(s)")


def _op_descriptor(session, h, args):
    dev = _require_dev(h, args)
    rec = _descriptor_record(dev)
    session.stream("usb.descriptor").append(
        json.dumps(rec, indent=2, sort_keys=True).encode() + b"\n")


def _op_control(session, h, args):
    dev = _require_dev(h, args)
    length = args.get("length") or 0
    if length < 0 or length > MAX_XFER:
        raise ValueError(f"usb:control length out of range: {length}")
    timeout_ms = args.get("timeout_ms") or 1000
    data = _decode_data_arg(args.get("data"))
    if args["bmRequestType"] & 0x80:
        payload = length
    else:
        payload = data
    session.bail_if_canceled("usb:control")
    got = dev.ctrl_transfer(
        args["bmRequestType"], args["bRequest"], args["wValue"],
        args["wIndex"], payload, timeout=timeout_ms)
    if args["bmRequestType"] & 0x80:
        out = bytes(got)
        session.stream("usb.control").append(out)
        session.log_event("USB", "usb:control",
                          f"IN {len(out)}B: {_hex_bytes(out[:64])}")
    else:
        if got != len(data):
            raise IOError(
                f"usb:control short OUT transfer: {got}/{len(data)}B")
        session.log_event("USB", "usb:control", f"OUT {len(data)}B")


def _op_bulk_write(session, h, args):
    dev = _require_dev(h, args)
    data = _decode_data_arg(args["data"])
    if len(data) > MAX_XFER:
        raise ValueError(f"usb:bulk_write too large: {len(data)}B")
    h.claim(args.get("interface"), bool(args.get("detach") or False), dev)
    timeout_ms = args.get("timeout_ms") or 1000
    written = 0
    while written < len(data):
        session.bail_if_canceled(
            f"usb:bulk_write {written}/{len(data)}B")
        n = dev.write(args["endpoint"], data[written:],
                      timeout=timeout_ms)
        if n <= 0:
            raise IOError(
                f"usb:bulk_write stalled at {written}/{len(data)}B")
        written += n
    session.log_event("USB", "usb:bulk_write",
                      f"ep=0x{args['endpoint']:02x} {written}B")


def _op_bulk_read(session, h, args):
    dev = _require_dev(h, args)
    length = args["length"]
    if length < 0 or length > MAX_XFER:
        raise ValueError(f"usb:bulk_read length out of range: {length}")
    h.claim(args.get("interface"), bool(args.get("detach") or False), dev)
    session.bail_if_canceled("usb:bulk_read")
    got = bytes(dev.read(args["endpoint"], length,
                         timeout=args.get("timeout_ms") or 1000))
    session.stream("usb.bulk").append(got)
    session.log_event("USB", "usb:bulk_read",
                      f"ep=0x{args['endpoint']:02x} {len(got)}B")


class UsbPlugin(DevicePlugin):
    name = "usb"
    doc = (
        "Raw USB access via pyusb/libusb for firmware bring-up. "
        "Use this for descriptors, control requests, and endpoint-level "
        "debugging before a class driver binds. Configure real instances "
        "with usb_vid, usb_pid, optional usb_serial and interface. "
        "usb.any is always present: use usb.any:list to discover devices, "
        "then pass vid=0x1234 pid=0xabcd and optional serial=\"...\" to "
        "usb.any:descriptor/control/bulk_read/bulk_write. Selectors must "
        "match exactly one device.")

    ops = {
        "list": Op(
            args={},
            optional_args={"vid": "int", "pid": "int", "serial": "str"},
            doc=("Enumerate USB devices visible to libusb. Optional vid/pid "
                 "filters are integers, e.g. vid=0xf4ec pid=0x1011; "
                 "serial is an exact string match. Use this on usb.any to "
                 "find selectors for later ops."),
            run=_op_list),
        "descriptor": Op(
            args={},
            optional_args={"vid": "int", "pid": "int", "serial": "str"},
            doc=("Emit device/config/interface/endpoint descriptors as JSON. "
                 "For usb.any pass vid=0x1234 pid=0xabcd and optional "
                 "serial=\"...\"; selectors must match exactly one device."),
            run=_op_descriptor),
        "control": Op(
            args={"bmRequestType": "int", "bRequest": "int",
                  "wValue": "int", "wIndex": "int"},
            optional_args={"length": "int", "data": "blob",
                           "timeout_ms": "int",
                           "vid": "int", "pid": "int", "serial": "str"},
            doc=("Issue one USB control transfer. IN transfers use "
                 "length=N and append bytes to usb.control; OUT transfers "
                 "use optional data=@blob. For usb.any pass vid=0x1234 "
                 "pid=0xabcd and optional serial=\"...\"; selectors must "
                 "match exactly one device."),
            run=_op_control),
        "bulk_write": Op(
            args={"endpoint": "int", "data": "blob"},
            optional_args={"interface": "int", "detach": "bool",
                           "timeout_ms": "int",
                           "vid": "int", "pid": "int", "serial": "str"},
            doc=("Write bytes to a bulk endpoint. Optional interface claims "
                 "that interface first; detach=true detaches a kernel driver "
                 "before claiming. For usb.any pass vid=0x1234 pid=0xabcd "
                 "and optional serial=\"...\"; selectors must match exactly "
                 "one device."),
            run=_op_bulk_write),
        "bulk_read": Op(
            args={"endpoint": "int", "length": "int"},
            optional_args={"interface": "int", "detach": "bool",
                           "timeout_ms": "int",
                           "vid": "int", "pid": "int", "serial": "str"},
            doc=("Read bytes from a bulk endpoint into usb.bulk. Optional "
                 "interface claims that interface first. For usb.any pass "
                 "vid=0x1234 pid=0xabcd and optional serial=\"...\"; "
                 "selectors must match exactly one device."),
            run=_op_bulk_read),
    }

    def probe(self):
        configured = list(config.instances(self.name))
        out = [{
            "id": "any",
            "list_only": True,
            "description": "raw USB enumeration pseudo-device",
            "configured_instances_note": (
                "Planned USB instances from config.json. These are not "
                "connected/resolvable unless they also appear as separate "
                "device rows such as usb.<id>."),
            "configured_instances": [
                {
                    "id": inst.get("id"),
                    "usb_vid": inst.get("usb_vid") or inst.get("vid"),
                    "usb_pid": inst.get("usb_pid") or inst.get("pid"),
                    "usb_serial": inst.get("usb_serial") or inst.get("serial"),
                    "description": inst.get("description"),
                }
                for inst in configured
            ],
        }]
        for inst in configured:
            try:
                dev = _match_configured(inst)
            except Exception:
                dev = None
            if dev is None:
                continue
            spec = dict(inst)
            spec.setdefault("id", f"{dev.bus}-{dev.address}")
            spec.update(_device_record(dev))
            out.append(spec)
        return out

    def open(self, spec):
        if spec.get("list_only"):
            return UsbHandle(spec, None)
        dev = _match_configured(spec)
        if dev is None:
            raise RuntimeError(
                f"usb.{spec.get('id', '?')}: configured device not present")
        return UsbHandle(spec, dev)

    def close(self, handle):
        if handle.dev is None:
            return
        try:
            _usb_core, usb_util = _lazy_usb()
            for dev, interface in list(handle.claimed):
                try:
                    usb_util.release_interface(dev, interface)
                except Exception:
                    pass
            seen = []
            if handle.dev is not None:
                seen.append(handle.dev)
            for dev in handle.selected:
                if not any(dev is old for old in seen):
                    seen.append(dev)
            for dev, _interface in handle.claimed:
                if not any(dev is old for old in seen):
                    seen.append(dev)
            for dev in seen:
                usb_util.dispose_resources(dev)
        except Exception:
            pass
