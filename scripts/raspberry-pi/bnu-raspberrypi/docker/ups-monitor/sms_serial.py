#!/usr/bin/env python3
"""SMS Brazil UPS serial protocol — read-only (the bnu rack nobreak, SMS Premium 1500 VA).

The protocol NUT's `sms_ser` driver speaks: 2400 8N1, 7-byte query frames, 18-byte
replies. This module sends ONLY the three query frames: Q (status), I
(identification) and F (rating). The protocol also has T/L (battery test), M (beeper
toggle), S/R (shutdown / restore) and C/D (cancel), which act on the UPS. They are
deliberately not implemented here.

Used by ups_monitor.py (the ups-monitor container). Also runs as a one-shot CLI on
bnu-raspberrypi. The port takes one reader at a time, so stop the container first,
or just read http://bnu-raspberrypi:8795/api/status instead:

    python3 sms_serial.py [--json] [--raw] [--dev /dev/ttyUSB0]

Stdlib only. termios is imported lazily, so the parsers also import on Windows (tests).
"""
import json
import os
import select
import sys
import time

DEFAULT_DEV = ("/dev/serial/by-id/"
               "usb-Prolific_Technology_Inc._USB-Serial_Controller_BHAFb149907-if00-port0")
FRAME_LEN = 18  # every reply: header + 15 payload bytes + checksum + '\r'

# Status byte bit -> meaning (order from NUT drivers/sms_ser.c)
STATUS_BITS = [
    (0x01, "beep_on"),
    (0x02, "shutdown_active"),
    (0x04, "test_active"),
    (0x08, "ups_ok"),
    (0x10, "boost"),
    (0x20, "bypass"),
    (0x40, "low_battery"),
    (0x80, "on_battery"),
]
HEADERS = {"Q": 0x3D, "I": 0x3A, "F": 0x3B}  # '=', ':', ';'


class UpsError(Exception):
    """No valid reply (port missing, cable out, UPS silent or garbled frame)."""


def frame(cmd):
    """7-byte query: cmd, 4 x 0xFF, checksum (all 6 bytes sum to 0 mod 256), CR."""
    body = bytes([ord(cmd), 0xFF, 0xFF, 0xFF, 0xFF])
    return body + bytes([(-sum(body)) & 0xFF]) + b"\r"


def valid(buf, cmd):
    return (len(buf) >= FRAME_LEN and buf[0] == HEADERS[cmd]
            and sum(buf[:FRAME_LEN - 1]) % 256 == 0)


class SmsPort:
    """Keeps the serial port open across queries; reopens after any error."""

    def __init__(self, dev=DEFAULT_DEV, timeout=1.5):
        self.dev = dev
        self.timeout = timeout
        self.fd = None

    def open(self):
        import termios  # POSIX only
        fd = os.open(self.dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            a = termios.tcgetattr(fd)
            a[0] = a[1] = a[3] = 0                  # raw: no iflag/oflag/lflag
            a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            a[4] = a[5] = termios.B2400
            a[6][termios.VMIN] = a[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, a)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except Exception:
            os.close(fd)
            raise
        self.fd = fd

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def query(self, cmd, retries=2):
        """Send one query frame, return the 18-byte reply. Raises UpsError."""
        import termios
        last = "nothing"
        for _ in range(retries):
            try:
                if self.fd is None:
                    self.open()
                termios.tcflush(self.fd, termios.TCIFLUSH)
                os.write(self.fd, frame(cmd))
                buf, end = b"", time.monotonic() + self.timeout
                while time.monotonic() < end and len(buf) < FRAME_LEN:
                    if select.select([self.fd], [], [], 0.1)[0]:
                        chunk = os.read(self.fd, 64)
                        if not chunk:          # adapter unplugged
                            raise OSError("serial port returned EOF")
                        buf += chunk
            except OSError as e:
                self.close()
                last = str(e)
                continue
            if valid(buf, cmd):
                return buf[:FRAME_LEN]
            last = buf.hex(" ") or "nothing"
        raise UpsError(f"no valid reply to '{cmd}' ({last})")


def _u16(b, i):
    return ((b[i] << 8) | b[i + 1]) / 10.0


def parse_status(b):
    bits = b[15]
    return {
        "last_input_v": _u16(b, 1),
        "input_v": _u16(b, 3),
        "output_v": _u16(b, 5),
        "load_pct": _u16(b, 7),
        "output_hz": _u16(b, 9),
        "battery_pct": _u16(b, 11),
        "temperature_c": _u16(b, 13),
        "status_byte": f"0x{bits:02x}",
        "flags": {name: bool(bits & mask) for mask, name in STATUS_BITS},
    }


def parse_info(b):
    txt = b[1:16].decode("ascii", "replace")
    return {"model": txt[:12].strip(), "firmware": txt[12:15].strip()}


def parse_rating(b):
    txt = b[1:16].decode("ascii", "replace")

    def num(s):
        s = s.strip()
        return int(s) if s.isdigit() else None
    return {
        "voltage_range": txt[0:7],          # e.g. "EBiS115" = entrada bivolt, saída 115 V
        "current_range": txt[7:10],
        "battery_v": num(txt[10:13]),
        "frequency_hz": num(txt[13:15]),
    }


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Read the SMS UPS once (Q/I/F queries only).")
    ap.add_argument("--dev", default=os.environ.get("UPS_DEV", DEFAULT_DEV))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args(argv)

    port = SmsPort(args.dev)
    try:
        raw = {c: port.query(c) for c in "QIF"}
    finally:
        port.close()
    out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "device": args.dev,
           "info": parse_info(raw["I"]), "rating": parse_rating(raw["F"]),
           "status": parse_status(raw["Q"])}
    if args.raw:
        out["raw"] = {c: v.hex(" ") for c, v in raw.items()}
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return
    i, r, s = out["info"], out["rating"], out["status"]
    on = [k for k, v in s["flags"].items() if v]
    print(f"Model     {i['model']}  firmware {i['firmware']}")
    print(f"Rating    {r['voltage_range']}  current {r['current_range']}  "
          f"battery {r['battery_v']} V  {r['frequency_hz']} Hz")
    print(f"Input     {s['input_v']:.1f} V   (last input {s['last_input_v']:.1f} V)")
    print(f"Output    {s['output_v']:.1f} V  {s['output_hz']:.1f} Hz  load {s['load_pct']:.1f} %")
    print(f"Battery   {s['battery_pct']:.1f} %   temperature {s['temperature_c']:.1f} °C")
    print(f"Status    {s['status_byte']}  set: {', '.join(on) or '-'}")
    for c, v in out.get("raw", {}).items():
        print(f"raw {c}     {v}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, UpsError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
