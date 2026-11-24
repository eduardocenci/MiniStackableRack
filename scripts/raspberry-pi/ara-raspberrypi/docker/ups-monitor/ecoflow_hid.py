#!/usr/bin/env python3
"""EcoFlow RIVER 3 Plus as a USB HID Power Device — read-only (the ara canteiro nobreak).

The station's USB port enumerates as `3746:ffff` "EF-UPS-RIVER 3 Plus": interface 0
is a HID Power Device (the standard UPS class), interfaces 1–2 a CDC-ACM serial
port (not used). The kernel's HID parser rejects the report descriptor ("unbalanced
collection"), so there is no /dev/hidraw: this module reads the reports straight
from usbfs (/dev/bus/usb/BBB/DDD) with HID GET_REPORT control transfers.

It sends ONLY GET_REPORT (device-to-host). It never sends SET_REPORT, so it cannot
change the beeper, the shutdown delays or anything else on the station.

Reports used (ids from the device's descriptor, captured 2026-11-24):

    0x07  PresentStatus, 16 bits (see STATUS_BITS)      polled
    0x0c  RemainingCapacity, %                           polled
    0x0d  RunTimeToEmpty, s                              polled
    0x0b  Voltage, 0.01 V (battery pack)                 polled
    0x01  ConfigActivePower — reads 286 = the pack's Wh  identification
    0x0a  ConfigVoltage, 0.01 V                          identification
    0x0f  WarningCapacityLimit, %                        identification

The station reports no input voltage, no load and no temperature over HID.

Used by ups_monitor.py (the ups-monitor container). Also a one-shot CLI on
ara-raspberrypi2 (the container and the CLI can read at the same time only if the
container is stopped: the interface has one owner):

    python3 ecoflow_hid.py [--json] [--raw]

Stdlib only. fcntl is imported lazily, so the parsers also import on Windows (tests).
"""
import ctypes
import glob
import json
import os
import sys
import time

VENDOR, PRODUCT = "3746", "ffff"
HID_INTERFACE = 0
SYSFS = "/sys/bus/usb/devices"

# usbfs ioctls (64-bit and 32-bit layouts differ only in the pointer size)
_P64 = ctypes.sizeof(ctypes.c_void_p) == 8
USBDEVFS_CONTROL = 0xC0185500 if _P64 else 0xC0105500
USBDEVFS_DISCONNECT_CLAIM = 0x8108551B   # detach usbhid from the interface and claim it
USBDEVFS_CLAIMINTERFACE = 0x8004550F

GET_REPORT = 0x01
FEATURE = 3

# PresentStatus (report 0x07) bit -> meaning, in descriptor order
STATUS_BITS = [
    (0x0001, "charging"),
    (0x0002, "discharging"),
    (0x0004, "ac_present"),
    (0x0008, "battery_present"),
    (0x0010, "below_capacity_limit"),
    (0x0020, "time_limit_expired"),
    (0x0040, "need_replacement"),
    (0x0080, "voltage_not_regulated"),
    (0x0100, "fully_charged"),
    (0x0200, "fully_discharged"),
    (0x0400, "shutdown_requested"),
    (0x0800, "shutdown_imminent"),
]
POLL_REPORTS = {0x07: 3, 0x0C: 2, 0x0D: 3, 0x0B: 3}     # id -> length incl. the id byte
INFO_REPORTS = {0x01: 3, 0x0A: 3, 0x0F: 2}


class UpsError(Exception):
    """No valid reply (station unplugged, no permission on the usbfs node, bad report)."""


class _Ctrl(ctypes.Structure):
    _fields_ = [("bRequestType", ctypes.c_uint8), ("bRequest", ctypes.c_uint8),
                ("wValue", ctypes.c_uint16), ("wIndex", ctypes.c_uint16),
                ("wLength", ctypes.c_uint16), ("timeout", ctypes.c_uint32),
                ("data", ctypes.c_void_p)]


class _DisconnectClaim(ctypes.Structure):
    _fields_ = [("interface", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("driver", ctypes.c_char * 256)]


def _read(path):
    with open(path) as f:
        return f.read().strip()


def find_device(sysfs=SYSFS):
    """→ {"node": "/dev/bus/usb/001/003", "model", "manufacturer", "serial", "firmware"}
    of the first EcoFlow UPS on the bus. Raises UpsError when it is not plugged in."""
    for d in sorted(glob.glob(os.path.join(sysfs, "*"))):
        try:
            if _read(d + "/idVendor") != VENDOR or _read(d + "/idProduct") != PRODUCT:
                continue
            info = {"node": "/dev/bus/usb/%03d/%03d" % (int(_read(d + "/busnum")), int(_read(d + "/devnum")))}
            for key, name in (("model", "product"), ("manufacturer", "manufacturer"),
                              ("serial", "serial"), ("firmware", "bcdDevice")):
                try:
                    info[key] = _read(f"{d}/{name}")
                except OSError:
                    info[key] = None
            if info["firmware"] and info["firmware"].isdigit():     # "0100" -> "1.00"
                info["firmware"] = f"{int(info['firmware'][:2])}.{info['firmware'][2:]}"
            return info
        except (OSError, ValueError):
            continue
    raise UpsError(f"EcoFlow UPS ({VENDOR}:{PRODUCT}) not on the USB bus")


class EcoflowPort:
    """Keeps the usbfs node open across polls; re-finds the device after any error
    (a re-plug gives it a new device number)."""

    def __init__(self, timeout_ms=1000):
        self.timeout_ms = timeout_ms
        self.fd = None
        self.device = None

    def open(self):
        import fcntl  # POSIX only
        dev = find_device()
        try:
            fd = os.open(dev["node"], os.O_RDWR)
        except OSError as e:
            raise UpsError(f"cannot open {dev['node']}: {e.strerror}") from e
        try:
            try:
                fcntl.ioctl(fd, USBDEVFS_DISCONNECT_CLAIM, _DisconnectClaim(HID_INTERFACE, 0, b""))
            except OSError:   # older kernels: plain claim (works when no driver is bound)
                fcntl.ioctl(fd, USBDEVFS_CLAIMINTERFACE, ctypes.c_uint(HID_INTERFACE))
        except OSError as e:
            os.close(fd)
            raise UpsError(f"cannot claim HID interface on {dev['node']}: {e.strerror}") from e
        self.fd, self.device = fd, dev

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def get_report(self, rid, length):
        """One HID GET_REPORT(feature, rid) → the raw report (first byte = rid)."""
        import fcntl
        buf = ctypes.create_string_buffer(length)
        ctrl = _Ctrl(0xA1, GET_REPORT, (FEATURE << 8) | rid, HID_INTERFACE, length,
                     self.timeout_ms, ctypes.cast(buf, ctypes.c_void_p))
        got = fcntl.ioctl(self.fd, USBDEVFS_CONTROL, ctrl)
        raw = buf.raw[:got]
        if got != length or raw[0] != rid:
            raise UpsError(f"bad report 0x{rid:02x}: {raw.hex(' ') or 'nothing'}")
        return raw

    def read(self, reports=None, retries=2):
        """→ {report id: raw bytes} for POLL_REPORTS (or `reports`). Raises UpsError."""
        reports = POLL_REPORTS if reports is None else reports
        last = "nothing"
        for _ in range(retries):
            try:
                if self.fd is None:
                    self.open()
                return {rid: self.get_report(rid, n) for rid, n in reports.items()}
            except OSError as e:
                last = e.strerror or str(e)
            except UpsError as e:
                last = str(e)
            self.close()
        raise UpsError(f"no valid reply from the EcoFlow ({last})")


def _u16(b):
    return b[1] | (b[2] << 8)


def parse_status(raw):
    """{0x07, 0x0c, 0x0d, 0x0b: raw} → the reading ups_monitor.py works on.

    input_v / output_v / output_hz / load_pct / temperature_c are part of the
    ups-monitor reading shape (bnu's SMS UPS reports them); this station does not,
    so they are None."""
    bits = _u16(raw[0x07])
    flags = {name: bool(bits & mask) for mask, name in STATUS_BITS}
    # the two names the rule and the dashboard card read on every ups-monitor
    flags["on_battery"] = not flags["ac_present"]
    flags["low_battery"] = flags["below_capacity_limit"] or flags["shutdown_imminent"]
    return {
        "battery_pct": float(raw[0x0C][1]),
        "runtime_s": _u16(raw[0x0D]),
        "battery_v": _u16(raw[0x0B]) / 100.0,
        "input_v": None, "output_v": None, "output_hz": None, "load_pct": None,
        "temperature_c": None,
        "status_byte": f"0x{bits:04x}",
        "flags": flags,
    }


def parse_rating(raw):
    return {"capacity_wh": _u16(raw[0x01]), "battery_v": _u16(raw[0x0A]) / 100.0,
            "warning_pct": raw[0x0F][1]}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Read the EcoFlow UPS once (HID GET_REPORT only).")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args(argv)

    port = EcoflowPort()
    try:
        raw = port.read()
        raw_info = port.read(INFO_REPORTS)
        dev = port.device
    finally:
        port.close()
    out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "device": dev,
           "rating": parse_rating(raw_info), "status": parse_status(raw)}
    if args.raw:
        out["raw"] = {f"0x{rid:02x}": v.hex(" ") for rid, v in {**raw_info, **raw}.items()}
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return
    r, s = out["rating"], out["status"]
    on = [k for k, v in s["flags"].items() if v]
    print(f"Model     {dev['manufacturer']} {dev['model']}  firmware {dev['firmware']}  serial {dev['serial']}")
    print(f"Rating    {r['capacity_wh']} Wh  pack {r['battery_v']:.2f} V  warning below {r['warning_pct']} %")
    print(f"Battery   {s['battery_pct']:.0f} %   {s['battery_v']:.2f} V   runtime {s['runtime_s'] // 60} min")
    print(f"Status    {s['status_byte']}  set: {', '.join(on) or '-'}")
    for rid, v in out.get("raw", {}).items():
        print(f"raw {rid}  {v}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, UpsError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
