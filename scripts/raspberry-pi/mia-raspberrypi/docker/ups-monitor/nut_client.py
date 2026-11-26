#!/usr/bin/env python3
"""The mia rack nobreak (CyberPower CP1000PFCLCD) read through NUT — read-only.

The UPS hangs on the Pi's USB (0764:0501, a standard HID Power Device). The
`nut-upsd` container on the same Pi owns the USB interface (driver usbhid-ups) and
serves it on :3493; this module is a minimal client of that server (NUT network
protocol, `LIST VAR`). It is the same server the Synology NAS reads.

It only ever sends `LIST VAR <ups>` — no login, no SET, no INSTCMD — and the server
has no account allowed to change anything, so nothing here can touch the beeper, the
shutdown delays or the outlets.

Variables used (CP1000PFCLCD through NUT 2.8.0 "CyberPower HID 0.6", captured
2026-11-26):

    ups.status              OL · OB · LB · CHRG · DISCHRG · CAL …
    input.voltage           V
    output.voltage          V
    ups.load                % of ups.realpower.nominal (600 W) → load_w (estimate)
    battery.charge          %
    battery.runtime         s (the UPS's own estimate)
    battery.voltage         V
    ups.test.result         "In progress" while a self-test runs

The UPS reports no temperature and no frequency.

Used by ups_monitor.py (the ups-monitor container). Also a one-shot CLI:

    python3 nut_client.py [--dev ups@127.0.0.1:3493] [--json] [--raw]

Stdlib only.
"""
import json
import os
import socket
import sys
import time

DEFAULT_DEV = "ups@127.0.0.1:3493"

# flag name -> ups.status token (the names the rule in ups_monitor.py reads)
STATUS_BITS = [
    ("OL", "on_line"),
    ("OB", "on_battery"),
    ("LB", "low_battery"),
    ("CHRG", "charging"),
    ("DISCHRG", "discharging"),
    ("RB", "replace_battery"),
    ("OVER", "overload"),
    ("CAL", "test_active"),
]


class UpsError(Exception):
    pass


def split_dev(dev):
    """'ups@host:port' → (ups, host, port); host and port are optional."""
    name, _, rest = dev.partition("@")
    host, _, port = (rest or "127.0.0.1").partition(":")
    return name or "ups", host or "127.0.0.1", int(port or 3493)


def parse_list(text, ups):
    """The reply to `LIST VAR <ups>` → {name: value}. Raises UpsError on ERR lines
    (DATA-STALE, DRIVER-NOT-CONNECTED, UNKNOWN-UPS …) or a truncated list."""
    out, done = {}, False
    for line in text.splitlines():
        if line.startswith("ERR"):
            raise UpsError(line.strip())
        if line.startswith(f"END LIST VAR {ups}"):
            done = True
        elif line.startswith(f"VAR {ups} "):
            name, _, val = line[len(f"VAR {ups} "):].partition(" ")
            out[name] = val.strip().strip('"').replace('\\"', '"')
    if not done:
        raise UpsError("truncated reply from upsd")
    return out


class NutPort:
    """Keeps the TCP connection to upsd open across queries; reconnects after any error."""

    def __init__(self, dev=DEFAULT_DEV, timeout=3.0):
        self.dev = dev
        self.ups, self.host, self.port = split_dev(dev)
        self.timeout = timeout
        self.sock = None

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def query(self, _cmd="Q"):
        """Every query is the same full variable list; the argument only mirrors the
        bnu serial reader (Q status · I info · F rating)."""
        try:
            if self.sock is None:
                self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.sock.sendall(f"LIST VAR {self.ups}\n".encode())
            buf = b""
            end = f"END LIST VAR {self.ups}\n".encode()
            deadline = time.monotonic() + self.timeout
            while not buf.endswith(end) and not buf.startswith(b"ERR"):
                if time.monotonic() > deadline:
                    raise UpsError("timeout reading upsd")
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise UpsError("upsd closed the connection")
                buf += chunk
            return parse_list(buf.decode("utf-8", "replace"), self.ups)
        except (OSError, UpsError) as e:
            self.close()
            raise e if isinstance(e, UpsError) else UpsError(f"upsd {self.host}:{self.port}: {e}")


def _f(v, key):
    try:
        return float(v[key])
    except (KeyError, ValueError):
        return None


def parse_status(v):
    if "ups.status" not in v:
        raise UpsError("no ups.status in the reply")
    tokens = v["ups.status"].split()
    flags = {name: tok in tokens for tok, name in STATUS_BITS}
    if v.get("ups.test.result", "").lower().startswith("in progress"):
        flags["test_active"] = True
    vin, batt, load = _f(v, "input.voltage"), _f(v, "battery.charge"), _f(v, "ups.load")
    if vin is None or batt is None:
        raise UpsError("no input.voltage / battery.charge in the reply")
    nominal = _f(v, "ups.realpower.nominal")
    return {
        "input_v": vin,
        "output_v": _f(v, "output.voltage"),
        "load_pct": load if load is not None else 0.0,
        "load_w": round(load * nominal / 100.0) if load is not None and nominal else None,
        "battery_pct": batt,
        "battery_v": _f(v, "battery.voltage"),
        "runtime_s": _f(v, "battery.runtime"),
        "temperature_c": None,
        "status_byte": v["ups.status"],
        "flags": flags,
    }


def parse_info(v):
    return {"model": v.get("ups.model") or v.get("device.model") or "?",
            "firmware": v.get("ups.firmware", ""),
            "driver": f"{v.get('driver.name', '?')} {v.get('driver.version', '')}".strip()}


def parse_rating(v):
    def num(key):
        x = _f(v, key)
        return int(x) if x is not None else None
    vn = num("input.voltage.nominal")
    return {
        "voltage_range": f"{vn} V" if vn else "?",
        "battery_v": num("battery.voltage.nominal"),
        "frequency_hz": None,
        "realpower_w": num("ups.realpower.nominal"),
        "transfer_low_v": num("input.transfer.low"),
        "transfer_high_v": num("input.transfer.high"),
        "runtime_low_s": num("battery.runtime.low"),
        "charge_low_pct": num("battery.charge.low"),
    }


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Read the mia UPS once through NUT (LIST VAR only).")
    ap.add_argument("--dev", default=os.environ.get("UPS_DEV", DEFAULT_DEV))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args(argv)

    port = NutPort(args.dev)
    try:
        v = port.query()
    finally:
        port.close()
    out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "device": args.dev,
           "info": parse_info(v), "rating": parse_rating(v), "status": parse_status(v)}
    if args.raw:
        out["raw"] = v
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return
    i, r, s = out["info"], out["rating"], out["status"]
    print(f"{i['model']} ({i['driver']}) · {r['voltage_range']} · {r['realpower_w']} W")
    print(f"status {s['status_byte']} · entrada {s['input_v']:.0f} V · saída {s['output_v'] or 0:.0f} V · "
          f"carga {s['load_pct']:.0f} % (~{s['load_w']} W) · bateria {s['battery_pct']:.0f} % · "
          f"autonomia {(s['runtime_s'] or 0) / 60:.0f} min")


if __name__ == "__main__":
    try:
        main()
    except UpsError as e:
        sys.exit(f"UPS read failed: {e}")
