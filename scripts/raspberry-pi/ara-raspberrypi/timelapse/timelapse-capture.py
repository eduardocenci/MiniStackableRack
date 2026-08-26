#!/usr/bin/env python3
"""timelapse-capture — daily construction-timelapse frames of the ARA canteiro.

Runs on ara-raspberrypi, driven by systemd timers (see *.timer next to this
file). Frames are grabbed from the LOCAL mediamtx relay (127.0.0.1:8554), so
no camera credentials are needed here:

  canteiro      = PT lens   (camera channel 1 — the auto-tracking lens)
  canteiro-alt  = fixed lens (camera channel 2 — pulled on demand)

Grabs must be keyframe-only (-skip_frame nokey): HEVC mid-GOP grabs from the
relay produce gray garbage (see ../ptz/README.md, 2026-08-24).

Subcommands
  trabalho   one PT frame -> outbox/Trabalho/YYYY-MM/   (worker-presence log;
             timer fires every 25 min 07:00-17:50)
  sunset     computes today's sunset (NOAA, coords of Lote 56 / Céu Azul
             aerodrome: 26°33'41"S 48°41'46"W, UTC-3 fixed) and shoots the
             five solar windows T-20 T-10 T T+10 T+20 — PT lens into one
             folder per window + fixed-lens twin into fixa/ — sleeping
             between windows (start the service before T-20; timer at 16:40
             covers the earliest T-20 of the year, 17:09 in June)

Every frame is written to outbox/ (drained to Google Drive by
timelapse-upload at 20:00, rclone move) and hardlinked into archive/
(30-day local safety copy, pruned by the upload unit).
"""
import math
import os
import subprocess
import sys
import time
from datetime import date, datetime

BASE = "/var/lib/timelapse"
OUTBOX = os.path.join(BASE, "outbox")
ARCHIVE = os.path.join(BASE, "archive")
RELAY_PT = "rtsp://127.0.0.1:8554/canteiro"
RELAY_FIXA = "rtsp://127.0.0.1:8554/canteiro-alt"

# Lote 56, Cond. Aeronáutico Céu Azul, Araquari SC — aerodrome coordinates
LAT = -(26 + 33 / 60 + 41 / 3600)
LON = -(48 + 41 / 60 + 46 / 3600)
TZ_H = -3.0  # America/Sao_Paulo, no DST

# offset (min from sunset), Drive folder, tag used in fixa/ twin filenames
WINDOWS = [
    (-20, "por-do-sol-menos-20min", "m20"),
    (-10, "por-do-sol-menos-10min", "m10"),
    (0, "por-do-sol", "pds"),
    (10, "por-do-sol-mais-10min", "p10"),
    (20, "por-do-sol-mais-20min", "p20"),
]


def sunset_minutes(d):
    """Local-clock sunset in minutes after midnight (NOAA solar equations)."""
    a = (14 - d.month) // 12
    y = d.year + 4800 - a
    m = d.month + 12 * a - 3
    jdn = d.day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    jd = jdn + (12 - TZ_H) / 24 - 0.5
    T = (jd - 2451545.0) / 36525.0
    L0 = (280.46646 + T * (36000.76983 + 0.0003032 * T)) % 360
    M = 357.52911 + T * (35999.05029 - 0.0001537 * T)
    e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T)
    Mr = math.radians(M)
    C = (math.sin(Mr) * (1.914602 - T * (0.004817 + 0.000014 * T))
         + math.sin(2 * Mr) * (0.019993 - 0.000101 * T)
         + math.sin(3 * Mr) * 0.000289)
    omega = 125.04 - 1934.136 * T
    lam = L0 + C - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    eps0 = 23 + (26 + (21.448 - T * (46.8150 + T * (0.00059 - T * 0.001813))) / 60) / 60
    eps = eps0 + 0.00256 * math.cos(math.radians(omega))
    decl = math.asin(math.sin(math.radians(eps)) * math.sin(math.radians(lam)))
    yv = math.tan(math.radians(eps / 2)) ** 2
    L0r = math.radians(L0)
    eqtime = 4 * math.degrees(
        yv * math.sin(2 * L0r) - 2 * e * math.sin(Mr)
        + 4 * e * yv * math.sin(Mr) * math.cos(2 * L0r)
        - 0.5 * yv * yv * math.sin(4 * L0r) - 1.25 * e * e * math.sin(2 * Mr))
    latr = math.radians(LAT)
    cosH = (math.cos(math.radians(90.833)) / (math.cos(latr) * math.cos(decl))
            - math.tan(latr) * math.tan(decl))
    H = math.degrees(math.acos(max(-1.0, min(1.0, cosH))))
    return 720 - 4 * LON - eqtime + TZ_H * 60 + 4 * H


def grab(url, rel_dest, tries=3):
    """One keyframe JPEG from `url` into outbox/rel_dest (+ archive hardlink)."""
    dest = os.path.join(OUTBOX, rel_dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    for attempt in range(1, tries + 1):
        try:
            r = subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error",
                 "-rtsp_transport", "tcp", "-skip_frame", "nokey",
                 "-i", url, "-frames:v", "1", "-q:v", "2", "-f", "image2", "-y", tmp],
                timeout=75)
            # gray/garbage frames come out tiny; a real 3MP frame is >100 KB
            if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 50_000:
                os.replace(tmp, dest)
                apath = os.path.join(ARCHIVE, rel_dest)
                os.makedirs(os.path.dirname(apath), exist_ok=True)
                try:
                    os.link(dest, apath)
                except FileExistsError:
                    pass
                print(f"ok {rel_dest}")
                return True
        except subprocess.TimeoutExpired:
            pass
        print(f"retry {attempt}/{tries} {rel_dest}", file=sys.stderr)
        time.sleep(5)
    if os.path.exists(tmp):
        os.remove(tmp)
    print(f"FAILED {rel_dest}", file=sys.stderr)
    return False


def cmd_trabalho():
    now = datetime.now()
    mins = now.hour * 60 + now.minute
    if not (6 * 60 + 55 <= mins <= 18 * 60 + 5):  # guard against odd manual runs
        print("outside 07:00-18:00 window, skipping")
        return 0
    stamp = now.strftime("%Y-%m-%d_%H%M")
    ok = grab(RELAY_PT, f"Trabalho/{now.strftime('%Y-%m')}/{stamp}.jpg")
    return 0 if ok else 1


def cmd_sunset():
    T = sunset_minutes(date.today())
    print(f"sunset today: {int(T // 60):02d}:{int(T % 60):02d}")
    failures = 0
    for off, folder, tag in WINDOWS:
        target = (T + off) * 60  # seconds after midnight
        now = datetime.now()
        now_s = now.hour * 3600 + now.minute * 60 + now.second
        if now_s < target:
            time.sleep(target - now_s)
        elif now_s > target + 300:
            print(f"window {folder} already >5 min past, skipping", file=sys.stderr)
            failures += 1
            continue
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        if not grab(RELAY_PT, f"{folder}/{stamp}.jpg"):
            failures += 1
        grab(RELAY_FIXA, f"fixa/{stamp}_{tag}.jpg")
    return 0 if failures == 0 else 1


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "trabalho":
        return cmd_trabalho()
    if cmd == "sunset":
        return cmd_sunset()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
