# canteiro-relay-vigia — the long-term record of the canteiro camera's streams (ara-raspberrypi2)

systemd unit on the Pi of the obra, next to the [`canteiro-relay`](../docker/canteiro-relay/README.md)
container it watches. In production since 2026-11-26 19:32 BRT; `MODE=live` (reopening the
camera session) until 2026-11-27 09:04, **`MODE=shadow` since then** — reopening turned out not
to cure the stall (below), so the unit is the RECORD of the failure and the picture is kept
live elsewhere: the mosaic shows the lens' substream while its main has no video
([`canteiro-mosaico`](../../../proxmox/frigate/bnu-frigate/README.md), 2026-11-27).

## Result of the first morning (2026-11-27, `MODE=live`)

12 real stalls between 04:52 and 08:54 BRT, all on `canteiro-alt` (fixed lens, main):

- **It is the camera.** Each one was 3.8–4.3 kB/s at the relay's input — the audio alone.
- **Reopening does not cure it.** 5 of the 12 needed 2–5 reopens: the new session came up
  audio-only too. Video back after 40–200 s, median 62 s — the day before, without this unit,
  the median hole was 50 s. The stall is in the camera's encoder of that stream, not in the
  RTSP session; it ends by itself.
- Each reopen also put 6 s of the bnu "sem sinal" card into the feed, for nothing.
- Camera Wi-Fi at 09:10: 2.4 GHz, signal −41 dBm, SNR 53, 72–86 Mbps PHY for ~4.1 Mbps sent —
  a strong link; weak signal is not the trigger. It re-associated at 19:26 the night before
  (`associatedTimeS`), which is the "camera off the LAN" event seen at the relay.

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

## What it does (in `MODE=live`; `shadow` logs `reabriria` instead of reopening)

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

## The long-term record (2026-11-27)

Why the camera's encoder stalls is not known, and the right fix may only be decided months
from now. So both ends keep, for good, what happened and what led to it; nothing is deleted.

**At the source — this unit** (`/var/lib/canteiro-relay-vigia/`, readable without sudo,
~20 MB a month):

| File | One line per | What is in it |
|---|---|---|
| `amostras-YYYY-MM.jsonl` | minute | per path: share of the minute `pronto` (ready), average / lowest / highest kB/s; `ping` (5 pings each to the camera and to the router every 30 s: `perda` %, `rtt`, `rtt_max`); `wifi` (the camera's line in the Starlink router's client list every 60 s: `sinal_dBm`, `snr`, `banda`, `tx_Mbps`/`tx_mcs`, `rx_Mbps`/`rx_mcs`, `vazao_Mbps`, `associado_s`) |
| `eventos-YYYY-MM.jsonl` | event | `parou` (a ready path under its floor for 15 s — with `antes`: the 5 s rates of EVERY path over the 2 min before, and the `ping`/`wifi` of that moment) · `reabriria` / `reaberto` · `voltou` (`s` it took) · `fonte_caiu` / `fonte_voltou` (a path stopped / started being ready: the camera off its LAN, or a relay restart) · `camera_reassociou` (its Wi-Fi association time went back to zero) · `relay_fora` / `relay_voltou` · `inicio` (the unit started — a gap in the samples before it = it was down) |

The Wi-Fi reading goes through `docker exec starlink-names grpcurl` (the unit runs as
`eduardocenci`, docker group); without that container the `wifi` field is `null` and the rest
goes on. `eventos.jsonl.migrado` is the single file of 26–27/11, already copied into the
monthly one.

**At the picture — `canteiro-mosaico`** (bnu Frigate LXC 105,
`/var/lib/canteiro-mosaico/eventos-YYYY-MM.jsonl`): `principal_parou` / `principal_voltou` per
lens, `sub_entrou` (`espera_s` = how long the picture stood still) / `sub_saiu` / `sub_parou`,
`feeder_reiniciado`, `encoder_reiniciado`, `inicio`. `journal-ate-2026-11-27.txt.gz` there is
the composer's journal from 14/11 to the day the record started.

**Reading both — [`canteiro-camera-saude.py`](canteiro-camera-saude.py)**, from the dev PC:

```bash
python scripts/raspberry-pi/ara-raspberrypi/canteiro-relay-vigia/canteiro-camera-saude.py              # today
python scripts/raspberry-pi/ara-raspberrypi/canteiro-relay-vigia/canteiro-camera-saude.py 2026-11-27 2026-12-03
python scripts/raspberry-pi/ara-raspberrypi/canteiro-relay-vigia/canteiro-camera-saude.py --json > saude.json
```

Per day: stalls of each stream at the source (count, audio-only, total / median / p90 /
longest, rate before, by hour), in how many of them every other stream dipped just before
(a hiccup of the camera's link) and how long before, ping loss at that moment; the camera's
link over the day (minutes with ping loss, Wi-Fi signal / SNR / PHY rate, re-associations);
on the mosaic, main holes per lens, how often the substream came in and after how long, and
the seconds the picture actually stood still; and whether every stall at the source had the
substream on screen. Run from a clone without the repo-root `.env`, point `MSR_ROOT` at a
checkout that has it (the mosaic's file is read through `devtool.py`).

Questions the record is meant to settle: does the stall follow a link hiccup or come out of
nothing; does it track bitrate (daylight, motion), PHY rate or time of day; do the substreams
or the PT main ever stall; did a firmware or setting change make a difference (compare weeks).
Not yet done: a second copy off the SD card / the LXC disk.

## State and events

```bash
ssh eduardocenci@ara-raspberrypi2 "cat /run/canteiro-relay-vigia/estado.json; tail -20 /var/lib/canteiro-relay-vigia/eventos-$(date +%Y-%m).jsonl; journalctl -u canteiro-relay-vigia -n 20 --no-pager"
```

Events, one line each: `parou` (rate during the stall, rate before, `so_audio`),
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

(The mosaic's substream fallback was seen against 4 real stalls on 2026-11-27 09:25–09:40 —
table in the bnu-frigate README.)

- Why the camera's encoder of one stream stalls, and whether a setting of the camera (bitrate,
  smart codec) avoids it. Not looked into: the camera is configured through the Mibo app.
- The camera and this Pi are both on the Starlink Wi-Fi (`eth0` down). On 26/11 a ping to the
  camera lost 60 % for some seconds with the router at 0 %, and the camera left the LAN
  altogether three times (15:47, 17:39, 19:26) — with a strong signal (above), so interference
  on 2.4 GHz or the camera's own Wi-Fi stack, not distance.
