# canteiro-relay-vigia — reopens a camera stream that lost its video (ara-raspberrypi2)

systemd unit on the Pi of the obra, next to the [`canteiro-relay`](../docker/canteiro-relay/README.md)
container it watches. In production since 2026-11-26 19:32 BRT, `MODE=live`.

## The problem (diagnosed 2026-11-26)

On the live mosaic the **fixed lens** stood "SEM SINAL · último quadro HH:MM:SS" for minutes
while the PT lens — same camera, same Wi-Fi, same Pi, same uplink — was live, and Frigate
showed both cameras with signal. Three days of the composer's journal:

| Lens | Holes | ≥ 30 s | ≥ 2 min | Longest |
|---|---|---|---|---|
| PT (`canteiro`) | 148 | 0 | 0 | 12 s |
| fixed (`canteiro-alt`) | 217 | 92 | 22 | 385 s |

Nearly every long hole of the fixed lens starts in a ~5 s hole that both lenses feel in the
same second; the PT lens comes back, the fixed one does not. All of them between 06 h and
19 h BRT (colour picture, higher bitrate); none at night.

Measured during one (26/11 17:45 BRT, go2rtc byte counters on bnu-raspberrypi and in the
Frigate LXC, 5 s apart): `canteiro_fixa` kept its session and carried **19 kB per 5 s** — the
AAC track — against 625–770 kB of the PT main in the same window. The video of ONE stream
stops; its audio goes on. So nothing times out anywhere: the relay still receives bytes,
`canteiro-hls` and go2rtc on bnu never see EOF or silence, and the mosaic's feeder, restarted
every 33 s, reconnects to the same half-dead producer that Frigate's record keeps open.
Frigate says "com sinal" because it detects on the substream, a different session. The hole
ends by itself after 0.5–6 min, or when the camera drops off the LAN altogether and every
session is reopened (26/11 15:54 and 17:39).

Where the video stops: the byte counters of the relay's two main-stream sockets to the camera
(same age) differed by 81 MB at 17:39 and by 8.4 MB at 17:47 — what the fixed lens' holes of
each period amount to (≈ 480 s and 55 s at ~160 kB/s). That points at the camera (iM9,
channel 2 main), not at the relay; `eventos.jsonl` below records the rate the relay received
at every stall, which settles it with the first real one.

## What it does

[`canteiro-relay-vigia.py`](canteiro-relay-vigia.py) (stdlib Python; rationale in its
docstring). Every 5 s it reads the relay's API (`127.0.0.1:9997/v3/paths/list`) and takes the
byte rate of each path. A `ready` path under its floor for 15 s gets its camera session
reopened, by a PATCH of the path's config (`sourceOnDemandStartTimeout` toggled 10 s ↔ 11 s,
which makes mediamtx close and restart the path; only the in-memory config changes).

| Path | Floor | Audio alone | Quietest picture measured (night) |
|---|---|---|---|
| `canteiro`, `canteiro-alt` (mains) | 20 kB/s | ~4 kB/s | 105 kB/s |
| `canteiro-sub`, `canteiro-alt-sub` | 7 kB/s | ~4 kB/s | 14.5 kB/s |

Guards: 40 s of grace after a reopen; at most 12 reopens per path per hour (then it only
logs); a path that is not `ready` is left to mediamtx's own retry. `MODE=shadow` logs
`reabriria` and touches nothing.

What a reopen costs downstream (measured 2026-11-26 19:33, by hand on `canteiro-alt`): bnu
`canteiro-hls` logged EOF and was `online` again 6 s later, feeding its card meanwhile; the
mosaic did not log a hole.

## State and events

```bash
ssh eduardocenci@ara-raspberrypi2 "cat /run/canteiro-relay-vigia/estado.json; tail -20 /var/lib/canteiro-relay-vigia/eventos.jsonl; journalctl -u canteiro-relay-vigia -n 20 --no-pager"
```

`eventos.jsonl`, one line per event: `parou` (rate during the stall, rate before, `so_audio`),
`reaberto` (n-th reopen of this stall), `voltou` (`s` = seconds from the start of the stall to
the video being back, `reaberturas`). The other side of the same hole is the composer's
journal in the Frigate LXC (`lens fixa: frames again after N s`).

## Install / update

The relay needs its API on loopback (`api: yes`, `apiAddress: 127.0.0.1:9997` in
`mediamtx.yml` — a `docker restart canteiro-relay` to take it; mediamtx does not hot-reload a
bind-mounted file).

```bash
cd scripts/raspberry-pi/ara-raspberrypi/canteiro-relay-vigia
scp canteiro-relay-vigia.py canteiro-relay-vigia.service eduardocenci@ara-raspberrypi2:/tmp/
ssh eduardocenci@ara-raspberrypi2 "sudo install -m 755 /tmp/canteiro-relay-vigia.py /usr/local/bin/ && sudo install -m 644 /tmp/canteiro-relay-vigia.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable canteiro-relay-vigia && sudo systemctl restart canteiro-relay-vigia"
```

Tests: `python -m pytest scripts/raspberry-pi/ara-raspberrypi/canteiro-relay-vigia/ -q`.
Rollback: `sudo systemctl disable --now canteiro-relay-vigia` (the relay is untouched by it).

## Still open

- Not yet seen against a real stall (installed at night; the long ones happen by day).
- The camera and this Pi are both on the Starlink Wi-Fi (`eth0` down). On 26/11 a ping to the
  camera lost 60 % for some seconds with the router at 0 %, and the camera left the LAN
  altogether three times (15:47, 17:39, 19:26). The watchdog treats the symptom; the trigger
  is that link.
