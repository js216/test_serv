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
        self.claimed = set()

    def claim(self, interface=None, detach=False):
        if interface is None:
            interface = self.spec.get("interface")
        if interface is None:
            return
        interface = int(interface)
        usb_core, usb_util = _lazy_usb()
        if detach:
            try:
                if self.dev.is_kernel_driver_active(interface):
                    self.dev.detach_kernel_driver(interface)
            except (NotImplementedError, usb_core.USBError):
                pass
        usb_util.claim_interface(self.dev, interface)
        self.claimed.add(interface)


def _require_dev(h):
    if h.dev is None:
        raise RuntimeError(
            "usb.any supports usb:list only; configure a usb instance "
            "with usb_vid/usb_pid[/usb_serial] for descriptor/control/bulk")
    return h.dev


def _op_list(session, h, args):
    vid = args.get("vid")
    pid = args.get("pid")
    serial = args.get("serial")
    devs = [_device_record(d) for d in _find_devices(vid, pid, serial)]
    data = json.dumps(devs, indent=2, sort_keys=True).encode() + b"\n"
    session.stream("usb.list").append(data)
    session.log_event("USB", "usb:list", f"{len(devs)} device(s)")


def _op_descriptor(session, h, args):
    dev = _require_dev(h)
    rec = _descriptor_record(dev)
    session.stream("usb.descriptor").append(
        json.dumps(rec, indent=2, sort_keys=True).encode() + b"\n")


def _op_control(session, h, args):
    dev = _require_dev(h)
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
    dev = _require_dev(h)
    data = _decode_data_arg(args["data"])
    if len(data) > MAX_XFER:
        raise ValueError(f"usb:bulk_write too large: {len(data)}B")
    h.claim(args.get("interface"), bool(args.get("detach") or False))
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
    dev = _require_dev(h)
    length = args["length"]
    if length < 0 or length > MAX_XFER:
        raise ValueError(f"usb:bulk_read length out of range: {length}")
    h.claim(args.get("interface"), bool(args.get("detach") or False))
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
        "usb.any is always present and supports usb:list only.")

    ops = {
        "list": Op(
            args={},
            optional_args={"vid": "int", "pid": "int", "serial": "str"},
            doc=("Enumerate USB devices visible to libusb. Optional vid/pid "
                 "filters are integers, e.g. vid=0xf4ec pid=0x1011."),
            run=_op_list),
        "descriptor": Op(
            args={},
            doc="Emit device/config/interface/endpoint descriptors as JSON.",
            run=_op_descriptor),
        "control": Op(
            args={"bmRequestType": "int", "bRequest": "int",
                  "wValue": "int", "wIndex": "int"},
            optional_args={"length": "int", "data": "blob",
                           "timeout_ms": "int"},
            doc=("Issue one USB control transfer. IN transfers use "
                 "length=N and append bytes to usb.control; OUT transfers "
                 "use optional data=@blob."),
            run=_op_control),
        "bulk_write": Op(
            args={"endpoint": "int", "data": "blob"},
            optional_args={"interface": "int", "detach": "bool",
                           "timeout_ms": "int"},
            doc=("Write bytes to a bulk endpoint. Optional interface claims "
                 "that interface first; detach=true detaches a kernel driver "
                 "before claiming."),
            run=_op_bulk_write),
        "bulk_read": Op(
            args={"endpoint": "int", "length": "int"},
            optional_args={"interface": "int", "detach": "bool",
                           "timeout_ms": "int"},
            doc=("Read bytes from a bulk endpoint into usb.bulk. Optional "
                 "interface claims that interface first."),
            run=_op_bulk_read),
    }

    def probe(self):
        out = [{"id": "any", "list_only": True,
                "description": "raw USB enumeration pseudo-device"}]
        for inst in config.instances(self.name):
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
            for interface in list(handle.claimed):
                try:
                    usb_util.release_interface(handle.dev, interface)
                except Exception:
                    pass
            usb_util.dispose_resources(handle.dev)
        except Exception:
            pass
