#!/usr/bin/env python3
"""canteiro-relay-vigia — reopens a camera stream whose VIDEO stopped while its audio goes on.

Authoritative copy: scripts/raspberry-pi/ara-raspberrypi/canteiro-relay-vigia/canteiro-relay-vigia.py
Live copy:          ara-raspberrypi2:/usr/local/bin/canteiro-relay-vigia.py
                    (unit: canteiro-relay-vigia.service, same folder)

WHY (2026-11-26)
The live mosaic showed the fixed lens "SEM SINAL" for minutes while the PT lens, Frigate and
every log were fine: in three days the PT half had 148 holes, none over 12 s; the fixed half
217, 92 of them over 30 s and 22 over 2 min. Measured during one: the main stream of the
fixed lens kept its RTSP session and went on carrying ~4 kB/s - the AAC track - instead of
~150 kB/s. The iM9 stops sending the video of ONE stream (usually after a hiccup of its Wi-Fi
link that both lenses feel for ~5 s) and keeps the audio going, so no hop ever sees a
timeout: not the relay (bytes arrive), not canteiro-hls / go2rtc on bnu, not the mosaic's
feeder, which reconnects every 33 s to the same half-dead producer. It heals by itself after
0.5-6 min, or when the camera drops off the LAN altogether and every session is reopened.

WHAT IT DOES
Every 5 s it reads the relay's API (mediamtx, 127.0.0.1:9997 /v3/paths/list) and takes the
byte rate of each path. A path that is `ready` and stays under its floor (mains 20 kB/s, subs
7 kB/s - audio alone is ~4, the quietest night picture 105 and 14) for 15 s gets its camera
session reopened: a PATCH of the path's config (sourceOnDemandStartTimeout toggled between
10 s and 11 s - a field with no effect on a path that is being read) makes mediamtx close the
path and start it again; measured 2026-11-26: source back in 1 s, the bnu reader 1 s later.
The in-memory config is all that changes; mediamtx.yml is never written.

Guards: 40 s of grace after a reopen; at most 12 reopens per path per hour (beyond that it
only logs); a path that is not `ready` is left to mediamtx's own retry; MODE=shadow logs what
it would do and touches nothing.

RESULT OF THE FIRST MORNING (2026-11-27, MODE=live, 12 real stalls 04:52-08:54 BRT)
Every stall was the fixed lens' main at 3.8-4.3 kB/s - audio only, measured HERE, at the
relay's input: it is the camera that stops the video. But reopening does not cure it: 5 of the
12 needed 2 to 5 reopens (the new session came up audio-only as well) and the video was back
after 40-200 s, median 62 s - no better than the 50 s median of the day before without this
unit. The stall is inside the camera (the encoder of that stream), not in the session. Since
2026-11-27 the unit runs MODE=shadow: it RECORDS every stall (`parou`, `reabriria`, `voltou`)
and touches nothing; what keeps the picture live is the mosaic's substream fallback
(scripts/proxmox/frigate/bnu-frigate/canteiro-mosaico.py). MODE=live stays available.

State:  /run/canteiro-relay-vigia/estado.json (every tick)
Events: /var/lib/canteiro-relay-vigia/eventos.jsonl - one line per stall (`parou`), reopen
        (`reaberto`) and return of the video (`voltou`, with the seconds it took).
Stdlib only.
"""
import json
import os
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


def record(name, kind, data):
    row = dict(ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"), path=name, ev=kind, mode=MODE, **data)
    print("canteiro-relay-vigia: %s %s %s" % (name, kind, json.dumps(data, ensure_ascii=False)), flush=True)
    try:
        with open(os.path.join(VAR, "eventos.jsonl"), "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print("canteiro-relay-vigia: events file: %s" % e, flush=True)


def main():
    paths = dict((n, Path(f)) for n, f in FLOORS.items())
    print("canteiro-relay-vigia: mode=%s floors=%s stall=%ss" % (MODE, FLOORS, STALL_S), flush=True)
    api_down = False
    while True:
        now = time.time()
        try:
            items = dict((i["name"], i) for i in http("GET", API + "/v3/paths/list?itemsPerPage=100")["items"])
            if api_down:
                print("canteiro-relay-vigia: relay API back", flush=True)
            api_down = False
        except Exception as e:
            if not api_down:
                print("canteiro-relay-vigia: relay API unreachable (%s) - waiting" % e, flush=True)
            api_down, items = True, None
        if items is not None:
            for name, p in paths.items():
                it = items.get(name) or {}
                for kind, data in p.tick(now, bool(it.get("ready")), it.get("bytesReceived")):
                    if kind == "reabrir":
                        if MODE == "live":
                            try:
                                reopen(name)
                                record(name, "reaberto", data)
                            except Exception as e:
                                record(name, "reabrir_falhou", dict(data, erro=str(e)))
                        else:
                            record(name, "reabriria", data)
                    else:
                        record(name, kind, data)
            try:
                tmp = os.path.join(RUN, "estado.json.tmp")
                with open(tmp, "w") as f:
                    json.dump({"ts": int(now), "mode": MODE, "paths": dict(
                        (n, {"ready": bool((items.get(n) or {}).get("ready")),
                             "kB_s": None if p.rate is None else round(p.rate, 1), "piso_kB_s": p.floor,
                             "parado_desde": p.stall and int(p.stall["t0"]),
                             "reaberturas_1h": len([k for k in p.kicks if now - k < 3600])})
                        for n, p in paths.items())}, f)
                os.replace(tmp, os.path.join(RUN, "estado.json"))
            except OSError:
                pass
        time.sleep(max(0.5, TICK_S - (time.time() - now)))


if __name__ == "__main__":
    main()
