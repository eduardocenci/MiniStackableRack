#!/usr/bin/env python3
"""canteiro-relay-vigia — the long-term RECORD of the canteiro camera's streams, at the source.

Authoritative copy: scripts/raspberry-pi/ara-raspberrypi/canteiro-relay-vigia/canteiro-relay-vigia.py
Live copy:          ara-raspberrypi2:/usr/local/bin/canteiro-relay-vigia.py
                    (unit: canteiro-relay-vigia.service, same folder)

WHY (2026-11-26)
The live mosaic showed the fixed lens "SEM SINAL" for minutes while the PT lens, Frigate and
every log were fine: in three days the PT half had 148 holes, none over 12 s; the fixed half
217, 92 of them over 30 s and 22 over 2 min. Measured during one: the main stream of the
fixed lens kept its RTSP session and went on carrying ~4 kB/s - the AAC track - instead of
~150 kB/s. The iM9 stops sending the video of ONE stream and keeps the audio going, so no hop
ever sees a timeout. It heals by itself after 0.5-6 min.

RESULT OF THE FIRST MORNING (2026-11-27, MODE=live, 12 real stalls 04:52-08:54 BRT)
Every stall was the fixed lens' main at 3.8-4.3 kB/s - audio only, measured HERE, at the
relay's input: it is the camera that stops the video. But reopening does not cure it: 5 of the
12 needed 2 to 5 reopens (the new session came up audio-only as well) and the video was back
after 40-200 s, median 62 s - no better than the 50 s median of the day before without this
unit. The stall is inside the camera (the encoder of that stream), not in the session. Since
2026-11-27 the unit runs MODE=shadow: it records and touches nothing; what keeps the picture
live is the mosaic's substream fallback (scripts/proxmox/frigate/bnu-frigate/
canteiro-mosaico.py). MODE=live (reopen through a PATCH of the path's config) stays available.

WHAT IT RECORDS (2026-11-27: built to answer, months from now, "what is the right fix?")
Why the camera's encoder stalls is not known. So besides the stalls themselves the unit keeps
what could explain them - what the streams and the camera's link were doing BEFORE each one:

  every 5 s    the byte rate of each path from the relay's API (127.0.0.1:9997 /v3/paths/list)
  every 30 s   5 pings to the camera and 5 to the router (loss, average and worst rtt)
  every 60 s   the camera's line in the Starlink router's Wi-Fi client list (signal, SNR,
               PHY rates, time associated) - through `docker exec starlink-names grpcurl`

  amostras-YYYY-MM.jsonl   one line a minute: per path ready / average / lowest / highest kB/s
                           of the minute, the last ping and Wi-Fi readings
  eventos-YYYY-MM.jsonl    one line per event:
      parou            a ready path under its floor for 15 s. Carries `antes`: the 5 s rates
                       of EVERY path over the 2 min before (the trace of what led to it),
                       plus the ping and Wi-Fi readings of that moment
      reabriria / reaberto   what MODE=shadow would do / MODE=live did
      voltou           video back: seconds it took, rate
      fonte_caiu / fonte_voltou   a path stopped / started being `ready` (camera session
                       lost by the relay - the camera off its LAN, or a relay restart)
      camera_reassociou      the camera's Wi-Fi association time went back to zero
      inicio           the unit started (a gap in the samples before it = it was down)

Floors (under = video gone): mains 20 kB/s, subs 7 kB/s - audio alone is ~4, the quietest
night picture measured 105 and 14. Reading the record: `canteiro-camera-saude.py` (same
folder) joins it with the mosaic's own events (bnu Frigate LXC) into one report.

State: /run/canteiro-relay-vigia/estado.json (every tick). Files: /var/lib/canteiro-relay-vigia/.
~0.5 kB a minute of samples (~20 MB a month); nothing is ever deleted by the unit.
Stdlib only.
"""
import collections
import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.request

E = os.environ.get
API = E("API", "http://127.0.0.1:9997").rstrip("/")
MODE = E("MODE", "shadow")                    # shadow (records) | live (also reopens)
TICK_S = float(E("TICK_S", "5"))
STALL_S = float(E("STALL_S", "15"))           # under the floor for this long = video gone
GRACE_S = float(E("GRACE_S", "40"))           # after a reopen, before judging the path again
MAX_H = int(E("MAX_H", "12"))                 # reopens per path per hour
FLOORS = dict((k, float(v)) for k, v in (p.split("=") for p in E(
    "FLOORS", "canteiro=20,canteiro-alt=20,canteiro-sub=7,canteiro-alt-sub=7").split(",")))
RUN = E("RUNTIME_DIRECTORY", "/run/canteiro-relay-vigia")
VAR = E("STATE_DIRECTORY", "/var/lib/canteiro-relay-vigia")
TOGGLE = ("10s", "11s")
TRACE_S = float(E("TRACE_S", "120"))          # how much of the past a `parou` event carries
CAM_IP = E("CAM_IP", "192.168.1.56")
GW_IP = E("GW_IP", "192.168.1.1")
PING_EVERY_S = float(E("PING_EVERY_S", "30"))
WIFI_EVERY_S = float(E("WIFI_EVERY_S", "60"))
WIFI_CMD = E("WIFI_CMD", "docker exec starlink-names grpcurl -plaintext -max-time 5 -d "
                         "'{\"wifi_get_clients\":{}}' 192.168.1.1:9000 SpaceX.API.Device.Device/Handle")


class Path:
    """Decision for one path; no I/O, so the tests drive it with numbers."""

    def __init__(self, floor):
        self.floor = floor
        self.bytes = self.t = None
        self.rate = None          # kB/s over the last tick
        self.good = None          # last rate at or above the floor
        self.low_since = None     # start of the current run under the floor
        self.low_bytes = 0
        self.stall = None         # {"t0":…, "kicks":n} while the video is gone
        self.grace_until = 0.0
        self.kicks = []           # times of the reopens of the last hour

    def tick(self, now, ready, nbytes):
        """-> list of events: ("parou", {...}) / ("reabrir", {...}) / ("voltou", {...})"""
        ev = []
        if not ready or nbytes is None or self.bytes is None or nbytes < self.bytes:
            # not ready, first sample, or the counter restarted with the path (a reopen):
            # start measuring again; an open stall stays open until the video is back
            self.bytes, self.t = (nbytes if ready else None), now
            self.rate, self.low_since = None, None
            return ev
        dt = now - self.t
        if dt <= 0:
            return ev
        self.rate = (nbytes - self.bytes) / dt / 1000.0
        delta, self.bytes, self.t = nbytes - self.bytes, nbytes, now
        if self.rate >= self.floor:
            if self.stall:
                ev.append(("voltou", {"s": round(now - self.stall["t0"]), "reaberturas": self.stall["kicks"],
                                      "taxa_kB_s": round(self.rate, 1)}))
                self.stall = None
            self.good, self.low_since = self.rate, None
            return ev
        if self.low_since is None:
            self.low_since, self.low_bytes = now - dt, 0
        self.low_bytes += delta
        low_for = now - self.low_since
        if low_for < STALL_S or now < self.grace_until:
            return ev
        avg = self.low_bytes / low_for / 1000.0
        if not self.stall:
            self.stall = {"t0": self.low_since, "kicks": 0}
            ev.append(("parou", {"taxa_kB_s": round(avg, 1), "antes_kB_s": self.good and round(self.good, 1),
                                 "so_audio": avg > 0.5}))
        self.kicks = [k for k in self.kicks if now - k < 3600]
        if len(self.kicks) >= MAX_H:
            return ev
        self.kicks.append(now)
        self.stall["kicks"] += 1
        self.grace_until = now + GRACE_S
        self.low_since = None
        ev.append(("reabrir", {"n": self.stall["kicks"], "taxa_kB_s": round(avg, 1)}))
        return ev


class Minute:
    """The samples of one path within the current minute -> its line in amostras."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.n = self.ready = 0
        self.sum = 0.0
        self.lo = self.hi = None

    def add(self, ready, rate):
        self.n += 1
        if ready:
            self.ready += 1
        if rate is not None:
            self.sum += rate
            self.lo = rate if self.lo is None else min(self.lo, rate)
            self.hi = rate if self.hi is None else max(self.hi, rate)

    def row(self):
        if not self.n:
            return None
        got = self.lo is not None
        return {"pronto": round(self.ready / self.n, 2),
                "kB_s": round(self.sum / max(1, self.n), 1) if got else None,
                "min": round(self.lo, 1) if got else None, "max": round(self.hi, 1) if got else None}


def parse_ping(out):
    """`ping -q` output -> {"perda": %, "rtt": avg ms, "rtt_max": ms} (None fields when all lost)."""
    m = re.search(r"(\d+) packets transmitted, (\d+) received", out)
    if not m or not int(m.group(1)):
        return None
    sent, got = int(m.group(1)), int(m.group(2))
    r = re.search(r"= [\d.]+/([\d.]+)/([\d.]+)/", out)
    return {"perda": round(100.0 * (sent - got) / sent), "rtt": r and round(float(r.group(1)), 1),
            "rtt_max": r and round(float(r.group(2)), 1)}


def parse_wifi(raw, ip):
    """The router's wifi_get_clients answer -> the fields worth keeping of the client `ip`."""
    for c in (json.loads(raw).get("wifiGetClients") or {}).get("clients") or []:
        if c.get("ipAddress") != ip:
            continue
        tx, rx = c.get("txStats") or {}, c.get("rxStats") or {}
        return {"sinal_dBm": c.get("signalStrength"), "snr": c.get("snr"),
                "banda": (c.get("iface") or "").replace("RF_", "") or None,
                "tx_Mbps": tx.get("rateMbps"), "tx_mcs": tx.get("mcs"),
                "rx_Mbps": rx.get("rateMbps"), "rx_mcs": rx.get("mcs"),
                "vazao_Mbps": tx.get("throughputMbpsLast15sAvg") and round(tx["throughputMbpsLast15sAvg"], 2),
                "associado_s": c.get("associatedTimeS")}
    return None


class Link:
    """Ping and Wi-Fi readings of the camera, taken in its own thread (they can block)."""

    def __init__(self):
        self.ping = {}            # {"cam": {...}, "gw": {...}, "ts": epoch}
        self.wifi = None          # {..., "ts": epoch}
        self.events = collections.deque()
        self.said = None

    def ping_once(self, ip):
        try:
            r = subprocess.run(["ping", "-q", "-c", "5", "-i", "0.2", "-W", "1", ip],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
            return parse_ping(r.stdout.decode("ascii", "replace"))
        except (OSError, subprocess.TimeoutExpired):
            return None

    def wifi_once(self):
        r = subprocess.run(shlex.split(WIFI_CMD), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        if r.returncode:
            raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()[:120] or "exit %d" % r.returncode)
        return parse_wifi(r.stdout.decode("utf-8", "replace"), CAM_IP)

    def loop(self):
        next_wifi = 0.0
        while True:
            t = time.time()
            self.ping = {"cam": self.ping_once(CAM_IP), "gw": self.ping_once(GW_IP), "ts": int(t)}
            if WIFI_CMD and t >= next_wifi:
                next_wifi = t + WIFI_EVERY_S
                try:
                    w = self.wifi_once()
                    if self.said:
                        print("canteiro-relay-vigia: wifi readings back", flush=True)
                        self.said = None
                    old = self.wifi
                    if w is not None:
                        w["ts"] = int(t)
                        if (old and old.get("associado_s") is not None and w.get("associado_s") is not None
                                and w["associado_s"] < old["associado_s"]):
                            self.events.append(("camera_reassociou", {
                                "associado_s": w["associado_s"], "estava_s": old["associado_s"],
                                "sinal_dBm": w.get("sinal_dBm"), "snr": w.get("snr")}))
                    self.wifi = w             # None = the camera is not in the router's list
                except Exception as e:
                    msg = "%s: %s" % (e.__class__.__name__, e)
                    if msg != self.said:
                        self.said = msg
                        print("canteiro-relay-vigia: wifi readings unavailable (%s)" % msg[:160], flush=True)
            time.sleep(max(1.0, PING_EVERY_S - (time.time() - t)))

    def now(self):
        return {"ping": self.ping or None, "wifi": self.wifi}


def http(method, url, body=None):
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=4) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def reopen(name):
    cur = http("GET", "%s/v3/config/paths/get/%s" % (API, name))["sourceOnDemandStartTimeout"]
    http("PATCH", "%s/v3/config/paths/patch/%s" % (API, name),
         {"sourceOnDemandStartTimeout": TOGGLE[1] if cur == TOGGLE[0] else TOGGLE[0]})


def append(kind, row, when=None):
    """One JSON line into <kind>-YYYY-MM.jsonl (the month of the line's own time)."""
    t = time.localtime(when)
    try:
        with open(os.path.join(VAR, "%s-%s.jsonl" % (kind, time.strftime("%Y-%m", t))), "a") as f:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError as e:
        print("canteiro-relay-vigia: %s file: %s" % (kind, e), flush=True)


def record(name, kind, data, quiet=()):
    row = dict(ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"), path=name, ev=kind, mode=MODE, **data)
    short = dict((k, v) for k, v in data.items() if k not in quiet)
    print("canteiro-relay-vigia: %s %s %s" % (name or "-", kind, json.dumps(short, ensure_ascii=False)), flush=True)
    append("eventos", row)


def migrate():
    """The single eventos.jsonl of 26-27/11/2026 -> the monthly files."""
    old = os.path.join(VAR, "eventos.jsonl")
    if not os.path.isfile(old):
        return
    try:
        for line in open(old):
            m = re.search(r'"ts": ?"(\d{4}-\d{2})', line)
            if m:
                with open(os.path.join(VAR, "eventos-%s.jsonl" % m.group(1)), "a") as f:
                    f.write(line if line.endswith("\n") else line + "\n")
        os.rename(old, old + ".migrado")
    except OSError as e:
        print("canteiro-relay-vigia: migration: %s" % e, flush=True)


def main():
    os.makedirs(VAR, exist_ok=True)
    migrate()
    paths = dict((n, Path(f)) for n, f in FLOORS.items())
    minute = dict((n, Minute()) for n in FLOORS)
    ready_was = dict((n, None) for n in FLOORS)
    trace = collections.deque(maxlen=int(TRACE_S / TICK_S))      # (epoch, {path: kB/s or None})
    link = Link()
    threading.Thread(target=link.loop, daemon=True).start()
    record(None, "inicio", {"floors": FLOORS, "stall_s": STALL_S})
    api_down = False
    cur_min = int(time.time() // 60)
    while True:
        now = time.time()
        try:
            items = dict((i["name"], i) for i in http("GET", API + "/v3/paths/list?itemsPerPage=100")["items"])
            if api_down:
                record(None, "relay_voltou", {})
            api_down = False
        except Exception as e:
            if not api_down:
                record(None, "relay_fora", {"erro": str(e)[:120]})
            api_down, items = True, None
        while link.events:
            kind, data = link.events.popleft()
            record(None, kind, data)
        if items is not None:
            snap = {}
            for name, p in paths.items():
                it = items.get(name) or {}
                ready = bool(it.get("ready"))
                if ready_was[name] is not None and ready != ready_was[name]:
                    record(name, "fonte_voltou" if ready else "fonte_caiu", link.now(), quiet=("ping", "wifi"))
                ready_was[name] = ready
                events = p.tick(now, ready, it.get("bytesReceived"))
                snap[name] = None if p.rate is None else round(p.rate, 1)
                minute[name].add(ready, p.rate)
                for kind, data in events:
                    if kind == "parou":
                        data = dict(data, antes={"passo_s": TICK_S, "fim": int(now), "taxas": dict(
                            (n, [s[1].get(n) for s in trace] + [snap.get(n)]) for n in FLOORS)}, **link.now())
                        record(name, kind, data, quiet=("antes", "ping", "wifi"))
                    elif kind == "reabrir":
                        if MODE == "live":
                            try:
                                reopen(name)
                                record(name, "reaberto", data)
                            except Exception as e:
                                record(name, "reabrir_falhou", dict(data, erro=str(e)))
                        else:
                            record(name, "reabriria", data)
                    else:
                        record(name, kind, dict(data, **link.now()), quiet=("ping", "wifi"))
            trace.append((now, snap))
            try:
                tmp = os.path.join(RUN, "estado.json.tmp")
                with open(tmp, "w") as f:
                    json.dump({"ts": int(now), "mode": MODE, "paths": dict(
                        (n, {"ready": bool((items.get(n) or {}).get("ready")),
                             "kB_s": None if p.rate is None else round(p.rate, 1), "piso_kB_s": p.floor,
                             "parado_desde": p.stall and int(p.stall["t0"]),
                             "reaberturas_1h": len([k for k in p.kicks if now - k < 3600])})
                        for n, p in paths.items()), "ping": link.ping or None, "wifi": link.wifi}, f)
                os.replace(tmp, os.path.join(RUN, "estado.json"))
            except OSError:
                pass
        m = int(now // 60)
        if m != cur_min:
            rows = dict((n, minute[n].row()) for n in FLOORS)
            if any(rows.values()):
                append("amostras", dict(ts=time.strftime("%Y-%m-%dT%H:%M", time.localtime(cur_min * 60)),
                                        paths=rows, **link.now()), cur_min * 60)
            for mm in minute.values():
                mm.reset()
            cur_min = m
        time.sleep(max(0.5, TICK_S - (time.time() - now)))


if __name__ == "__main__":
    main()
