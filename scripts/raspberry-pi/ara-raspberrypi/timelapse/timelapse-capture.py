#!/usr/bin/env python3
"""timelapse-capture — daily construction-timelapse frames of the ARA canteiro.

Runs ENTIRELY on ara-raspberrypi (systemd timers, see *.timer next to this
file): frame grabs hit the LOCAL mediamtx relay (127.0.0.1:8554) and PTZ
moves go from the Pi to the camera over the house LAN via `canteiro-ptz` —
internet (Starlink) is only involved in the 20:00 rclone upload, never in
capture or movement.

  canteiro      = PT lens   (channel 1 — auto-tracking; the firmware returns
                  it to the guard position ~1 min after losing a tracked
                  person/vehicle, but NOT after manual ONVIF moves — probed
                  27/08/2026, still parked 95 s after a move)
  canteiro-alt  = fixed lens (channel 2 — pulled on demand)

Grabs are keyframe-only (-skip_frame nokey): HEVC mid-GOP grabs from the
relay produce gray garbage (see ../ptz/README.md).

Drive/outbox layout (decisão Eduardo 27/08/2026):
  posicao1/<janela>/YYYY-MM-DD_HHMM.jpg   PT lens at the guard (main, T+0:00)
  posicao2/<janela>/...                   PT lens at the calibrated secondary
                                          position (T+0:40)
  lentefixa/<janela>/...                  fixed lens, shot with the main
  trabalho/YYYY-MM/...                    presence log, every 15 min 07:00-18:00

<janela> = nascer-do-sol / nascer-do-sol-mais-10min / nascer-do-sol-mais-20min
and por-do-sol-menos-20min / -menos-10min / por-do-sol / -mais-10min /
-mais-20min.

Posição 2 is dead-reckoned from the guard with the burst RECIPE calibrated by
Eduardo on 27/08/2026 — this firmware has no usable ONVIF presets
(MaximumNumberOfPresets=0, GetStatus lies; ../ptz/README.md), but every
excursion starts from the same guard position, so replaying the exact bursts
reproduces the framing. After the posicao2 shot the mirrored reverse recipe
walks the lens back (residual ±3-4% per round trip; the firmware guard-return
wipes it at the first tracked person of the day).

Subcommands
  trabalho   one PT frame -> outbox/trabalho/YYYY-MM/
  sunset     today's sunset windows T-20 T-10 T T+10 T+20 (NOAA, coords of
             the Céu Azul aerodrome 26°33'41"S 48°41'46"W, UTC-3 fixed);
             per window: posicao1 + lentefixa, wait 30 s, RECIPE, posicao2,
             reverse. Timer at 16:40 covers the earliest T-20 (17:09, June).
  sunrise    same for the sunrise windows T T+10 T+20; timer at 05:00 covers
             the earliest sunrise of the year (~05:15, December).
  pos2test   RECIPE -> snap to /tmp/pos2test.jpg -> reverse, no outbox
             writes — health/re-calibration check.
"""
import math
import os
import subprocess
import sys
import time
from datetime import date, datetime

BASE = "/var/lib/timelapse"
OUTBOX = os.path.join(BASE, "outbox")
RELAY_PT = "rtsp://127.0.0.1:8554/canteiro"
RELAY_FIXA = "rtsp://127.0.0.1:8554/canteiro-alt"
PTZ = "/usr/local/bin/canteiro-ptz"

# Lote 56, Cond. Aeronáutico Céu Azul, Araquari SC — aerodrome coordinates
LAT = -(26 + 33 / 60 + 41 / 3600)
LON = -(48 + 41 / 60 + 46 / 3600)
TZ_H = -3.0  # America/Sao_Paulo, no DST

SUNSET_WINDOWS = [
    (-20, "por-do-sol-menos-20min"),
    (-10, "por-do-sol-menos-10min"),
    (0, "por-do-sol"),
    (10, "por-do-sol-mais-10min"),
    (20, "por-do-sol-mais-20min"),
]
SUNRISE_WINDOWS = [
    (0, "nascer-do-sol"),
    (10, "nascer-do-sol-mais-10min"),
    (20, "nascer-do-sol-mais-20min"),
]

# Posição 2: burst recipe from the guard (calibrated by Eduardo, 27/08/2026).
# Replayed as the EXACT sequence — motor ramps make 2x0.5s != 1x1.0s. The
# reverse is the mirrored sequence with inverted signs. +vx pans right,
# +vy tilts up.
RECIPE = [(0.4, 0.0, 0.5), (0.4, 0.0, 0.5), (0.0, 0.4, 0.2)]
SECONDARY_DELAY_S = 30   # wait after the main shots before moving
SETTLE_S = 2             # settle after arriving, before shooting


def _solar(d):
    """(solar noon, half day-arc) in local-clock minutes (NOAA equations)."""
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
    return 720 - 4 * LON - eqtime + TZ_H * 60, 4 * H


def sunset_minutes(d):
    noon, half_arc = _solar(d)
    return noon + half_arc


def sunrise_minutes(d):
    noon, half_arc = _solar(d)
    return noon - half_arc


def _grab_abs(url, dest, tries=3):
    """One keyframe JPEG from `url` into an absolute path."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    for attempt in range(1, tries + 1):
        try:
            r = subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error",
                 "-rtsp_transport", "tcp", "-skip_frame", "nokey",
                 "-i", url, "-frames:v", "1", "-q:v", "2", "-f", "image2",
                 "-y", tmp],
                timeout=75)
            # gray/garbage frames come out tiny; a real 3MP frame is >100 KB
            if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 50_000:
                os.replace(tmp, dest)
                print(f"ok {dest}")
                return True
        except subprocess.TimeoutExpired:
            pass
        print(f"retry {attempt}/{tries} {dest}", file=sys.stderr)
        time.sleep(5)
    if os.path.exists(tmp):
        os.remove(tmp)
    print(f"FAILED {dest}", file=sys.stderr)
    return False


def grab(url, rel_dest, tries=3):
    return _grab_abs(url, os.path.join(OUTBOX, rel_dest), tries)


def ptz_move(vx, vy, dur):
    try:
        r = subprocess.run([PTZ, "move", str(vx), str(vy), str(dur)],
                           capture_output=True, timeout=30)
        ok = r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        ok = False
    if not ok:
        print(f"PTZ move FAILED ({vx},{vy},{dur})", file=sys.stderr)
    return ok


def goto_secondary():
    """Walk to posição 2; returns the bursts actually executed (for reversal)."""
    done = []
    for vx, vy, dur in RECIPE:
        if not ptz_move(vx, vy, dur):
            break
        done.append((vx, vy, dur))
        time.sleep(1)
    time.sleep(SETTLE_S)
    return done


def back_to_guard(done):
    """Mirror the executed bursts back, best-effort even on partial failure."""
    for vx, vy, dur in reversed(done):
        ptz_move(-vx, -vy, dur)
        time.sleep(1)


def cmd_trabalho():
    now = datetime.now()
    mins = now.hour * 60 + now.minute
    if not (6 * 60 + 55 <= mins <= 18 * 60 + 5):  # guard against odd manual runs
        print("outside 07:00-18:00 window, skipping")
        return 0
    stamp = now.strftime("%Y-%m-%d_%H%M")
    ok = grab(RELAY_PT, f"trabalho/{now.strftime('%Y-%m')}/{stamp}.jpg")
    return 0 if ok else 1


def run_windows(T, windows, label):
    print(f"{label} today: {int(T // 60):02d}:{int(T % 60):02d}")
    failures = 0
    for off, folder in windows:
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
        if not grab(RELAY_PT, f"posicao1/{folder}/{stamp}.jpg"):
            failures += 1
        grab(RELAY_FIXA, f"lentefixa/{folder}/{stamp}.jpg")
        # posição 2: move, shoot, walk back — camera parks at the guard again
        time.sleep(SECONDARY_DELAY_S)
        done = goto_secondary()
        if len(done) == len(RECIPE):
            stamp2 = datetime.now().strftime("%Y-%m-%d_%H%M")
            if not grab(RELAY_PT, f"posicao2/{folder}/{stamp2}.jpg"):
                failures += 1
        else:
            print(f"posicao2 {folder} skipped (incomplete move)", file=sys.stderr)
            failures += 1
        back_to_guard(done)
    return 0 if failures == 0 else 1


def cmd_pos2test():
    done = goto_secondary()
    ok = len(done) == len(RECIPE) and _grab_abs(RELAY_PT, "/tmp/pos2test.jpg")
    back_to_guard(done)
    print("pos2test:", "ok -> /tmp/pos2test.jpg" if ok else "FAILED")
    return 0 if ok else 1


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "trabalho":
        return cmd_trabalho()
    if cmd == "sunset":
        return run_windows(sunset_minutes(date.today()), SUNSET_WINDOWS, "sunset")
    if cmd == "sunrise":
        return run_windows(sunrise_minutes(date.today()), SUNRISE_WINDOWS, "sunrise")
    if cmd == "pos2test":
        return cmd_pos2test()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
