# bnu-frigate — Frigate NVR (bnu LXC 105)

Frigate 0.17 running **natively** (no Docker) in LXC 105 on `bnu-proxmox`:
a source checkout at `/opt/frigate`, launched by systemd units that wrap the
s6 run scripts. LAN `10.1.1.160` (DHCP); **also a tailnet node**
(`bnu-frigate`). Web UI `http://10.1.1.160:5000` (auth disabled; nginx also
listens on the LXC).

| Fact | Value |
|---|---|
| Active config | **`/config/config.yml`** — mirrored by [`config.yml`](config.yml) here. Placeholders: `<BNU_DOORBELL_PW>` (the Hikvision NVR `192.168.0.22` and the doorbell share one admin password — there is **no** `BNU_CAMERA_PASSWORD` key) and `<BNU_MQTT_PASSWORD>`; values in `.env`. Edits made in the web UI's config editor land only in the live file (the canteiro `motion.mask` lived there alone until 2026-10-23) — **pull before you push** |
| ⚠ Decoy config | `/opt/frigate/config/config.yml` is the dev-checkout 16-line example — **not** the active file (cost a session real time on 2026-08-26) |
| Services | `frigate.service`, `go2rtc.service` (Frigate's internal go2rtc — its config is generated from `config.yml`'s `go2rtc:` block), `nginx.service` |
| Logs | `/dev/shm/logs/frigate/current`, `/dev/shm/logs/go2rtc/current` |
| Memory | **6 GB since 2026-11-16** (`pct set 105 -memory 6144` on bnu-proxmox, live; was 4 GB). At 4 GB the LXC sat on its limit all day with the 512 MB swap full, and the kernel killed the mosaic composer 21 times on 2026-11-16 (15 at the service's own 1.2 GB cap, 6 at the LXC limit) |
| Media | `/media/frigate` on the LXC root disk — **64 GB since 2026-10-23** (`pct resize 105 rootfs +40G` on bnu-proxmox, online, no restart; was 24 GB and 80 % full). Recordings are event-gated (alerts/detections, 10 days, `mode: all`), no continuous retention — see *Recording & retention* below for what that really keeps |
| ffmpeg | bundled at `/usr/lib/ffmpeg/7.0/bin` (not in `PATH`) |
| Detector | CPU tflite `/models/cpu_model.tflite` — its labelmap has **no `truck`** (config warning if tracked; trucks come through as `car`); the OpenVINO CPU plugin fails to load in this LXC. **The truck TYPE on the canteiro (caçamba · munck · poliguindaste com caçamba (para retirar entulho) · carroceria · prancha · betoneira · bomba — V3 on recording frames since 29/10/2026 18:56) is not decided here** — see *Consumers* |
| Consumers of the `canteiro` events | **`canteiro-alertas`** on bnu-raspberrypi — asks the OpenAI Decisions API about every vehicle event ≥ 10 s and posts one WhatsApp alert per truck to the obra group (runbook globalnet `docs/runbooks/canteiro-alertas.md` — globalnet#57, open; verdicts in `/var/lib/canteiro-alertas/alertas.jsonl`; code `scripts/raspberry-pi/bnu-raspberrypi/canteiro-alertas/` (PR #45 merged 29/10/2026; engine home-ara#210, gallery/V3 PR #52)); `canteiro-diario` (20:15 evidence pack for the Diário de Obra); `canteiro-vigia` (empty-site watch); HA `frigate_whatsapp.py` (raw alerts only while the canteiro LAN is empty). Before answering "what did the camera see / which truck", read these, not only this file |
| Consumers of the `canteiro` / `canteiro_fixa` review items | **`canteiro-painel`** on bnu-raspberrypi (since 2026-11-23) — every 15 s reads `/api/review?cameras=<lens>` and the preview GIF of the last 3 ended items of each lens (`/api/review/<id>/preview`), and on a click the recording of the stretch (`/api/<camera>/start/<ts>/end/<ts>/clip.mp4`); the `/live` page shows them over the mosaic video. Read-only. Changing the review config of these two cameras (alerts/detections labels, previews) changes what `/live` shows — `scripts/raspberry-pi/bnu-raspberrypi/canteiro-painel/README.md` |
| GenAI review | Ollama at `mia-desktop:11434` (`qwen3-vl:8b`), Portuguese descriptions per event |
| MQTT | `10.1.1.124` (bnu HA VM) — events feed Home Assistant |

## Config change flow (from this repo)

The mia-desktop ssh key is not authorized on bnu, so everything goes through
`devtool.py` (password fallback), staged via the Proxmox host. `pull`/`push`
take a bare `/tmp/…` argument, which Git Bash rewrites into `%TEMP%` unless
`MSYS_NO_PATHCONV=1` is set (REMOTE_ACCESS.md §3):

```bash
python scripts/devtool.py run bnu-proxmox "pct pull 105 /config/config.yml /tmp/f.yml"
MSYS_NO_PATHCONV=1 python scripts/devtool.py pull bnu-proxmox /tmp/f.yml ./f.yml
# edit ./f.yml; keep this folder's config.yml mirror in sync (passwords masked)
MSYS_NO_PATHCONV=1 python scripts/devtool.py push bnu-proxmox ./f.yml /tmp/f.yml
python scripts/devtool.py run bnu-proxmox "pct exec 105 -- cp /config/config.yml /config/config.yml.bak && pct push 105 /tmp/f.yml /config/config.yml && pct exec 105 -- systemctl restart frigate"
```

If the `go2rtc:` block changed, restart `go2rtc` **before** `frigate`.
Verify after any restart:

```bash
python scripts/devtool.py guest bnu 105 "curl -s http://127.0.0.1:5000/api/stats | head -c 400; grep -icE 'error|warning' /dev/shm/logs/frigate/current"
```

Every camera should sit at ~2 fps (`detect.fps` global). A restart cuts all
cameras for ~30 s and logs a burst of "Invalid or missing video stream in
segment … Discarding" for the segments cut mid-write — normal, not a fault.

## Recording & retention

**Policy** — the global `record:` block; every camera inherits it (the
canteiro has no block of its own): no continuous video (`continuous.days: 0`),
no motion-only video (`motion.days: 0`); **alerts** (review labels `person`,
`car` — Frigate's default) and **detections** (the other tracked labels:
bus, motorcycle, bicycle) kept **10 days**, `mode: all` (every segment of the
event window, motion or not), 5 s before / 10 s after. Snapshots 10 days.

**The configured days are a ceiling, not a promise.** Frigate deletes the
oldest recordings whenever the disk holding `/media/frigate/recordings` has
less than ~1 h of all-camera bitrate free (≈ 3.3 GB here), regardless of
`retain.days`. On the 24 GB disk that capped the real retention at **~2
days** (2026-10-23: 18 GB used, of which ~13 GB is the native install —
`/usr` 8 GB, `/opt` 3.3 GB — and only 4.3 GB of recordings, every camera's
oldest segment at 21/10 10:17). Check the real figure, not the config:

```bash
python scripts/devtool.py guest bnu 105 "df -h /media/frigate; curl -s http://127.0.0.1:5000/api/recordings/storage"
```

(`usage` = MB on disk per camera, `bandwidth` = MB/h while recording.)

**Sizing (2026-10-23)**: all cameras ≈ 2 GB/day event-gated; **the canteiro
is ~2/3 of it** (~1.2 GB/day, 1.7–2 h of segments per workday, ~600 MB/h on
the HEVC main stream — continuous 24/7 would be ~14 GB/day for that camera
alone). 64 GB − 13 GB install − 3.3 GB purge floor ≈ 47 GB for recordings
≈ 10 days with 2× headroom for busy days (concretagem). Grow again with
`pct resize 105 rootfs +NNG` on bnu-proxmox (thin-provisioned; `local-lvm`
had 147 GB free).

**Per-camera override**, if the canteiro ever needs its own policy (push +
restart `frigate` per the flow above; pre/post capture can also be set in
the UI under Settings → Camera configuration → canteiro → Recording — then
re-sync this mirror):

```yaml
cameras:
  canteiro:
    record:
      continuous:
        days: 0        # 24/7 — ~14 GB/day on this camera
      motion:
        days: 0        # motion segments — ≈ continuous in work hours (the PT lens moves by itself)
      alerts:
        pre_capture: 5
        post_capture: 10
        retain:
          days: 10
          mode: all    # all | motion | active_objects
      detections:
        retain:
          days: 10
          mode: all
    review:
      alerts:
        labels: [person, car, bus, motorcycle]   # what counts as an alert
      detections:
        labels: [bicycle]                        # the rest
```

When continuous and event retention both cover a segment, the longer one
wins.

## Birdseye restream (added 2026-08-26)

`birdseye: restream: true, mode: continuous` exposes the all-cameras composed
view as `rtsp://10.1.1.160:8554/birdseye` (go2rtc also serves it as HLS/MP4 on
`:1984`). The go2rtc producer is an on-demand `exec:` ffmpeg — the **first
consumer waits several seconds** for spawn + keyframe (5 s GOP), so warm it
with `curl 'http://10.1.1.160:1984/api/frame.jpeg?src=birdseye'` before
latency-sensitive use. Continuous mode encodes 720p H264 24/7 (small, steady
CPU cost). Consumer: mia HA `camera.frigate_birdseye` via a forward on
mia-proxmox → `script.cast_frigate_birdseye` casts it to the mia
entertainment-room TV (see `scripts/proxmox/mia-proxmox/README.md`).

## canteiro-mosaico — live mosaic of the ARA canteiro (added 2026-11-14)

A second, independent systemd unit in this LXC composes **the** live view of
the obra: fixed lens (left, street side) | PT lens (right, parked at its
rest position) side by side — the "almost 180°" view (decisão Eduardo
2026-11-14). It runs here because this is where the GPU is (VAAPI) and where
both HEVC 2304×1296 mains already arrive for `record`: it reads Frigate's
own go2rtc (`rtsp://127.0.0.1:8554/canteiro_fixa` and `/canteiro`), so the
canteiro's Starlink carries nothing extra.

- **Pipeline** — since 2026-11-18 [`canteiro-mosaico.py`](canteiro-mosaico.py), three ffmpeg
  processes around a 15 fps pump (see *canteiro-mosaico: holds the last frame* below). Per
  lens: VAAPI decode of the main → CPU `fps=15,scale=1280:720` → raw frames; the encoder
  stacks them and encodes with `h264_vaapi` → **H.264 2560×720, 15 fps, ~3 Mbps**, no audio.
  The all-GPU graph (`scale_vaapi` + `hstack_vaapi`) ran out of VAAPI surfaces on this Mesa
  22.3. (Until 2026-11-18: one ffmpeg, both lenses in one filter graph —
  `canteiro-mosaico.sh`, in the git history.)
- **Output**: published over RTSP to `canteiro-hls` (mediamtx) on
  bnu-raspberrypi, `rtsp://10.1.1.123:8555/mosaico` — from there the `/live`
  page (`/hls/mosaico`), go2rtc `canteiro_mosaico` (TV cast, watchdog frame).
  See `scripts/raspberry-pi/bnu-raspberrypi/docker/canteiro-hls/README.md`.
- **Why a separate unit and not a go2rtc `exec:` source**: Frigate refuses
  `exec:` sources unless `GO2RTC_ALLOW_ARBITRARY_EXEC` is set (left alone),
  and a publisher that dies is a clean EOF for mediamtx's readers.
  **`config.yml` is unchanged** by the mosaic.
- **Failure mode**: a lens that stops (EOF, or 10 s without packets) only restarts its own
  feeder; the picture keeps going out with that half held on its last frame. The whole unit
  restarts only if the pump itself dies (`Restart=always`, 5 s).

| File | Live copy (LXC 105) |
|---|---|
| [`canteiro-mosaico.sh`](canteiro-mosaico.sh) | `/usr/local/bin/canteiro-mosaico.sh` (755) — the unit's `ExecStart`: starts the `.py`; `--cinza` = diagnosis |
| [`canteiro-mosaico.py`](canteiro-mosaico.py) | `/usr/local/bin/canteiro-mosaico.py` (755) — the composer since 2026-11-18 |
| [`canteiro-mosaico-cinza.py`](canteiro-mosaico-cinza.py) | `/usr/local/bin/canteiro-mosaico-cinza.py` (755) |
| [`canteiro-mosaico.service`](canteiro-mosaico.service) | `/etc/systemd/system/canteiro-mosaico.service` (enabled; `After=go2rtc.service`) |

```bash
python scripts/devtool.py run bnu-proxmox "pct exec 105 -- systemctl status canteiro-mosaico"
python scripts/devtool.py run bnu-proxmox "pct exec 105 -- journalctl -u canteiro-mosaico -n 30 --no-pager"
python scripts/devtool.py run bnu-proxmox "pct exec 105 -- systemctl restart canteiro-mosaico"
```

**Rollback** = `systemctl disable --now canteiro-mosaico` in the LXC — Frigate
is untouched either way; the `/live` page and the TV then lose the mosaic
(`/live?src=canteiro` still plays the PT lens alone).

## Cameras

8 bnu house cameras (Hikvision NVR `192.168.0.22` + doorbell `10.1.1.65`)
**plus the ARA canteiro camera** (added 2026-08-26):

```
iM9+ camera (ARA LAN) ─▶ mediamtx on ara-raspberrypi2 ─tailnet/Starlink─▶ go2rtc on bnu-raspberrypi (10.1.1.123)
  PT lens (channel 1)        (mains via canteiro-hls) (1 copy per stream)      └─▶ this LXC: canteiro (HEVC main, record)
                                                                                  canteiro_sub (H264 640×480, detect)
  fixed lens (channel 2, since 2026-11-04)                                       canteiro_fixa (HEVC main, record — since 2026-11-05;
                                                                                  H264 640×480 substream, detect 5 fps)
```

**`canteiro_fixa` (2026-11-04)** — the same iM9's FIXED lens, aimed by Eduardo at the path
vehicles take in (many only pass the PT lens and park out of its angle); re-aimed on 2026-11-14
to look left, to the street, while the PT lens rests looking right (the two halves of the mosaic
above — the zones/masks in `config.yml` were not touched by that change). Detect comes from its
640×480 substream; **record comes from its HEVC main (2304×1296, ~1.4 Mbps) since 2026-11-05**
(bnu go2rtc `canteiro_fixa` → bnu `canteiro-hls` path `canteiro-fixa` → relay `canteiro-alt`; go2rtc
pulled the relay directly until 2026-11-14) — the second 24/7 main stream over the obra's
Starlink uplink, re-tried with measurements after the 2026-08-26 stall (numbers, limits and
rollback: `scripts/raspberry-pi/bnu-raspberrypi/docker/go2rtc/README.md`, "Two main streams").
On 2026-11-04 both roles were on the substream and recording frames were 640×480. Detect runs at 5 fps
(`min_initialized: 2`, car `threshold: 0.7`, `min_area: 800`) so a 2–3 s crossing still becomes an
event; GenAI review is off for it (same scene as the PT lens). `canteiro-alertas` reads both lenses
and keeps ONE episode per truck across them (home-ara decision 0010, adendo 04/11/2026); the HA
scripts treat it as an obra camera (`CEUAZUL_CAMERAS`, digest exclusion). If the uplink stalls
(relay "reader is too slow" in series, `camera_fps` dropping on either canteiro camera), point the
`record` role back at `canteiro_fixa_sub` — until 2026-11-14 go2rtc then dropped the main pull on
its own; since the mosaic, `canteiro-hls` holds the fixed main open 24/7 regardless (the mosaic
needs it), so freeing the uplink also means stopping `canteiro-mosaico` and the `canteiro-fixa`
path on bnu-raspberrypi.

**Never point this LXC at `ara-raspberrypi2` directly** — bnu-raspberrypi
(`canteiro-hls` for the mains, go2rtc for the substreams) is the single shared Starlink pull; a second main-stream reader
saturates the canteiro uplink (incident 2026-08-26, see
`scripts/raspberry-pi/bnu-raspberrypi/go2rtc/README.md`). Canteiro events
(person/vehicle) ride the same MQTT → HA pipeline and GenAI review
descriptions as the house cameras. Recordings are HEVC — Frigate handles
them fine; some browsers without HEVC decode may not play them back in the
web UI (use Edge/Safari or export).

## canteiro-mosaico: data band + self-restart (2026-11-15)

- `mosaico-tv` is now **1920×1080**: the 1920×540 mosaic on top and the 1920×540 band of
  `canteiro-painel` (bnu-raspberrypi `:8557`) under it. The script fetches the PNG every 30 s
  into `/run/canteiro-painel/painel.png` (atomic rename; only a complete PNG replaces the last
  good one) and ffmpeg reads it with `-re -f image2 -loop 1` — `-re` is what makes it re-open
  the file (measured: without it the picture never changes). `mosaico` stays 2560×720.
  Measured with the band: composer RSS ~660 MB, stable.
- The script watches ffmpeg's two publisher connections to mediamtx (`ss -p`): fewer than two
  in 2 readings 5 s apart (after 60 s of grace; it was 30 s until 2026-11-15, when a composer
  with dead outputs grew from 0.8 to 1.4 GB in 28 s and the LXC's memory limit killed it first;
  the unit now also has `MemoryMax`, 2000M since 2026-11-16) → exit, systemd starts a fresh composer. Before, a restart
  of `canteiro-hls` could leave ffmpeg alive publishing nothing (14 and 15/11/2026).
- **Halves seconds apart** (fixed 2026-11-15): hstack pairs frames by timestamp and ffmpeg
  zeroes each input at its own first frame, so a lens that connected N s after the other left
  the other half N s late for the life of the process (14 s that evening, after a restart that
  followed `canteiro-hls`). Both lenses are now stamped with the wall clock after decoding
  (`setpts=(RTCTIME-RTCSTART)/(TB*1000000)`); a 12 s staggered start on the sub streams gave
  15 s apart without it and the same second with it. Measure: crop the two burned clocks of
  `frame.jpeg?src=canteiro_mosaico`.
- **Grey half** (added 2026-11-15): twice that day, right after back-to-back restarts, the
  VAAPI HEVC decoder lost its references and painted ~75 % of the PT half as a flat grey slab
  that never healed (2 h, then 1 h 15, until Eduardo saw it); a software decode of the same
  stream was whole. Every 30 s the script decodes one frame of the newest complete
  `canteiro-clipe` segment and [`canteiro-mosaico-cinza.py`](canteiro-mosaico-cinza.py) counts,
  per half, the flat neutral-grey 32×30 cells of a 640×180 copy: ≥ 30 % in 3 checks in a row
  (90 s) → exit, systemd starts a fresh decoder. At most 3 such restarts per hour
  (`/run/canteiro-painel/cinza-restarts`); beyond that it only logs. Measured: stuck half
  0.67–0.82 on every sample of an hour of buffer, healthy halves 0.00 (daylight; night not
  yet sampled). Check by hand: `canteiro-mosaico.sh --cinza` (prints the shares; exit 1 = grey).
  It needs `canteiro-clipe` running — no segment = no verdict.

## canteiro-mosaico: holds the last frame (2026-11-18)

**The problem.** On the night of 16–17/11/2026 the mosaic on the bnu TV dropped 20 times in
15 h. Every drop was a hole in the canteiro feed (Starlink uplink, camera off its LAN): the
single ffmpeg had both lenses in one filter graph, and a graph only emits a frame when every
input has one — so the output stopped with the input, mediamtx closed both publisher sessions
after 10 s without RTP ("0 of 2 publisher connections"), the restarted ffmpeg got no frame
either ("No filtered frames for output stream", 1 s of CPU in 65 s), and the cycle repeated
every ~70 s until the `canteiro-watchdog` healed go2rtc + canteiro-hls, 1 to 7 min later.
`alwaysAvailable` on the lens paths does not cover it: the card only comes in when mediamtx
sees the source END, and during a stall the source is connected and silent. No ffmpeg option
fixes it inside one process — a stalled live input cannot be told from a slow one, and an
input that ended is never reopened.

**The design** ([`canteiro-mosaico.py`](canteiro-mosaico.py), stdlib Python; rationale in its
docstring). The output no longer depends on any input being alive:

```
go2rtc canteiro_fixa ─▶ feeder ffmpeg (decode, fps=15, 1280×720, raw) ─pipe─┐
go2rtc canteiro ──────▶ feeder ffmpeg (same) ───────────────────────pipe─┤
canteiro-painel PNG (every 30 s) ─▶ one-shot ffmpeg ─▶ raw ───────────────┤
                                                                          ▼
                       PUMP — 15 fps clock: next frame of each lens, or its LAST one again
                                                                          │ one raw stream, 1280×2712
                                                                          ▼
                       encoder ffmpeg (stdin only) ─▶ h264_vaapi ×2 ─▶ mosaico, mosaico-tv
```

- **Feeder, one per lens.** Ends on EOF or 10 s without packets; the pump starts another 3 s
  later, for ever. Nothing else is touched.
- **Pump.** Each lens has a small jitter queue (plays ~12 % faster above 0.5 s of backlog,
  jumps to the newest frames above 3 s — the bursts of the site uplink). Queue empty = the
  last frame again. After **5 s** without a new frame that half is dimmed and labelled
  **"SEM SINAL · último quadro HH:MM:SS"** (Brasília time of the last frame; "SEM SINAL" on a
  dark half if there was never one). The next frame clears it. No pixel work in Python: the
  two lenses and the panel (folded to 1280 px wide) have the same width, so their planes are
  just concatenated into one frame; the dimmed copy is made once per outage.
- **Encoder.** One raw input on stdin, no network input, no clock of its own: every filter
  sees the same timestamps, so there is nothing to stall or drift (the panel's
  `-re -f image2` input and the wall-clock `setpts` of the lenses went away with the single
  process). The label is `drawtext` reading `/run/canteiro-painel/sem-sinal-{fixa,pt}.txt`.
  Restarted alone (5 s, nothing to reopen) when it loses its publishers (`ss`, 2 readings
  5 s apart after 20 s of grace — what a restart of `canteiro-hls` causes), exits, or stops
  reading its pipe for 3 s.
- **Grey half** — same test and limits as 2026-11-15 (`canteiro-mosaico-cinza.py`, ≥ 30 % of a
  lens' cells in 3 checks 30 s apart, max 3 restarts/h), but on the lens' own frames (no
  `canteiro-clipe` needed) and it restarts only that lens' feeder.
- **A child that survives SIGKILL** (the amdgpu hang of 2026-11-17: every ffmpeg that opened
  `renderD128` sat in state D, one more every 90 s): nothing is started in its place while
  it lives; one log line every 10 min. Since 2026-11-18 the host watches for that hang
  itself — [`gpu-vigia`](../../bnu-proxmox/gpu-vigia/README.md) stops this unit and alerts
  the Casa SmokeTests group; the cure is still a host reboot.

What still shows the mediamtx card instead of the last frame: a source that ENDS upstream
(camera/relay EOF) — `canteiro-hls` then feeds its own "Sem sinal" card, which reaches the
composer as ordinary frames. The held frame covers the stalls, which is what took the
picture down.

**State**: `cat /run/canteiro-painel/mosaico-estado.json` (every 5 s — per lens `live`,
`last_frame_age_s`, `feeder_exits`, `label`; encoder `starts`, `publishers`; `painel_age_s`).
The journal has one line per transition (`lens pt: no frame for 5 s - holding the last one
(14:32:10 BRT)` / `lens pt: frames again after 213 s`).

```bash
python scripts/devtool.py guest bnu 105 "cat /run/canteiro-painel/mosaico-estado.json; journalctl -u canteiro-mosaico -n 20 --no-pager"
```

**Bench test** — [`teste-buraco.sh`](teste-buraco.sh): synthetic lenses, software codecs, two
local mediamtx (one as Frigate's go2rtc, one as `canteiro-hls`, same HLS settings), away
from production. Four phases with 75 s holes: one lens stalled (publisher SIGSTOPped —
connection up, no packets), both ended (EOF), both stalled, then the "canteiro-hls" side
restarted. Result of 2026-11-18 (WSL, ffmpeg 4.2 + mediamtx 1.20.1; the encoder's filter
graph also run on ffmpeg 7.0):

| Phase (75 s hole) | Picture during the hole | Back by itself |
|---|---|---|
| A — fixed lens stalled | fixed half held + dimmed + "SEM SINAL · último quadro 10:34:48"; PT half live | 10 s after the feed returned |
| B — both lenses ended (EOF) | both halves held + labelled; panel live | 6 s |
| C — both lenses stalled | both halves held + labelled; panel live | 13 s |
| D — "canteiro-hls" restarted | — (the server itself is down) | encoder restarted alone; publishing again ~10 s after the server was back |

Across A–C (351 s): **86 HLS segments of `mosaico-tv`, one every ~4 s without a gap**; the
bytes of an RTSP reader never stood still for more than 2 s and it was **the same reader
connection from start to end**; **the encoder was started once** (the second start is phase
D); the pump took ~7 % of a core. The "back by itself" times are the feeder's retry (3 s
pause + up to 10 s of RTSP timeout) plus the first keyframe; no watchdog, nothing restarted
by hand.

**In production since 2026-11-18 12:43 BRT** (LXC 105, after the host reboot that cleared
the hung amdgpu; previous script kept as `/usr/local/bin/canteiro-mosaico.sh.bak-20261118`,
`canteiro-mosaico-cinza.py.bak-20261118`, `/root/canteiro-mosaico.service.bak-20261118`).
VAAPI in three processes works: 2 publishers, 15 fps, unit memory ~410–435 MB (limit
2000 M), ~0.5 core in all. Two provoked holes (`kill -STOP` on both feeders):

| Provoked hole | What happened |
|---|---|
| 30 s, both lenses | both halves held + "SEM SINAL · último quadro 12:44:31" after 5 s; `frames again after 30 s` on `kill -CONT`; encoder never restarted, 2 publishers throughout |
| 70 s asked, both lenses | the composer itself killed the mute feeders at 30 s (`feeder up 30 s without a frame - restarting it`) and the picture was back at 35 s; `mosaico` and `mosaico-tv` playlists on bnu-raspberrypi went from media sequence 35 to 66 in 125 s — one segment every ~4 s, no gap; `canteiro-watchdog` logged nothing (no heal) |

Still to observe: a night of real uplink holes against the watchdog's heal count.

### Substream fallback — one lens without video for minutes (2026-11-27)

**The problem.** The fixed lens stood "SEM SINAL" for 0.5–6 min at a time while the PT lens,
Frigate and every log were fine: 92 holes ≥ 30 s in three days, against none over 12 s of the
PT lens. The camera stops sending the VIDEO of one stream (the fixed lens' main, by day) and
keeps its audio, so go2rtc's producer never times out and every restarted feeder reads the
same half-dead session. Reopening the camera session from the relay does not bring the video
back either (tried 2026-11-27, 12 real stalls —
[`canteiro-relay-vigia`](../../../raspberry-pi/ara-raspberrypi/canteiro-relay-vigia/README.md),
which now only records them). The substream of the same lens goes on all the while.

**What the composer does.** A lens whose main has had no frame for 8 s (`ALT_AFTER_S`) gets a
second feeder on its substream (`SRC_FIXA_ALT` / `SRC_PT_ALT`, default Frigate's go2rtc
`canteiro_fixa_sub` / `canteiro_sub`; software decode; 640×480 stretched to 1280×720, the same
field of view) and shows it under the label **"BAIXA RESOLUÇÃO · desde HH:MM:SS"** (time of the
last main frame) until the main has delivered frames steadily for 10 s (`ALT_BACK_S` — a 6 s
"sem sinal" card of a reconnect does not end it). The substream feeder stays up 2 min more
(`ALT_LINGER_S`; the holes come in clusters) and is then stopped. No substream either (camera
off the LAN) = the held, dimmed frame and "SEM SINAL" as before. `SRC_FIXA_ALT=""` turns it off.

`live`, `last_frame_age_s` and the journal lines `no frame for 5 s` / `frames again after N s`
still describe the MAIN, so the hole counts stay comparable with before; the new lines are
`lens fixa: showing the substream (main without video since HH:MM:SS BRT)` and
`lens fixa: back on the main`, and the state file has `fonte` (`principal` | `sub`),
`sub_feeder_up`, `sub_vezes` per lens.

**Provoked on 2026-11-27 09:08 BRT** (`kill -STOP` on the fixed lens' main feeder): held at
5 s, substream on screen 13 s after the last main frame, `back on the main` 10 s after the
restarted feeder delivered again; encoder never restarted.

**First real stalls, same morning** (the camera itself, fixed lens' main):

| Main without video (BRT) | For | Substream on screen after |
|---|---|---|
| 09:25:55 | 17 s | 11 s (feeder cold) |
| 09:35:07 | 167 s | 11 s |
| 09:38:16 | 72 s | 8 s (feeder still warm) |
| 09:39:39 | 55 s | 8 s |

311 s of main without video, ~38 s of held picture; `back on the main` 10 s after each
return; encoder started once; the composer at ~8 % of a core. Tests of the pump side:
`python -m pytest scripts/proxmox/frigate/bnu-frigate/test_canteiro_mosaico_sub.py -q`.
Previous script on the LXC: `/usr/local/bin/canteiro-mosaico.py.bak-20261127`.

## canteiro-mosaico: últimas detecções on the TV (2026-11-23)

Pedido Eduardo 23/11/2026: the last three Frigate detections of each lens as small moving
pictures at the bottom right of that lens — on `/live` they are GIFs laid over the video by the
page; on the TV they are burned into **`mosaico-tv` only** (`mosaico` also feeds the
canteiro-vigia videos, `canteiro-clipe` and the watchdog frame, which stay clean).

- `canteiro-painel` (bnu-raspberrypi) draws them: `/det/tv.json` (six keys) and
  `/det/tv/<key>.yuv` (a loop of raw I420 frames 172×96 with border, age and icon on it).
- `canteiro-mosaico.py`, class `Thumbs`: a poll thread fetches what changed every 15 s; an 8 fps
  thread copies the current frame of each loop into a fourth block of the raw frame, 1280×192 —
  96 rows of picture (three thumbnails from x=0, three from x=640, 180 px apart) over 96 rows of
  mask (235 under a thumbnail, 16 elsewhere). Cost measured: 0.4 ms per recomposed block.
- Encoder graph, TV branch only: crop the two groups, `alphamerge` with their masks, two
  `overlay`s on the 1920×540 mosaic before the panel is stacked under it. Empty slot = mask 16 =
  nothing drawn.
- No list from the panel for 5 min (`MINIATURAS_STALE_S`) → the thumbnails are cleared (a frozen
  "3m atrás" would lie). `MINIATURAS_URL=""` in the unit's environment turns them off.
- State: `miniaturas` in `/run/canteiro-painel/mosaico-estado.json` (`slots`, `age_s`, `erro`).
- Rehearsed off the LXC first (software mode, the graph from `Encoder.cmd()` on a raw file) —
  never run a second composer in this LXC.

## canteiro-clipe — mosaic rolling buffer + clips on demand (added 2026-11-14)

[`canteiro-clipe.py`](canteiro-clipe.py) + [`canteiro-clipe.service`](canteiro-clipe.service)
(live: `/usr/local/bin/canteiro-clipe.py`, `/etc/systemd/system/canteiro-clipe.service`,
enabled). One Python process:

- **recorder** — `ffmpeg -c copy` of `rtsp://10.1.1.123:8555/mosaico` (what `canteiro-mosaico`
  publishes) into 10 s MPEG-TS segments `/var/lib/canteiro-clipe/<epoch>.ts`; the last 60 min
  are kept (~1,4 GB). No decode — negligible CPU.
- **HTTP `:8556`** (clients `127.0.0.1` and `10.1.1.123` only) —
  `GET /clip.mp4?start=<epoch>&end=<epoch>` trims and re-encodes on the GPU to H.264 1920×540,
  bitrate chosen to stay ≤ 14 MB (30 s → 4,5 MB in 3,6 s, measured); `GET /health`.

**Consumer:** `canteiro-vigia` on bnu-raspberrypi (video of each detection stretch —
[`../../../raspberry-pi/bnu-raspberrypi/canteiro-vigia/README.md`](../../../raspberry-pi/bnu-raspberrypi/canteiro-vigia/README.md)).

```bash
python scripts/devtool.py run bnu-proxmox "pct exec 105 -- systemctl status canteiro-clipe"
python scripts/devtool.py run bnu-proxmox "pct exec 105 -- curl -s 127.0.0.1:8556/health"
python scripts/devtool.py run bnu-proxmox "pct exec 105 -- journalctl -u canteiro-clipe -n 30 --no-pager"
```

**Rollback** = `systemctl disable --now canteiro-clipe` (+ `rm -r /var/lib/canteiro-clipe`);
the vigia then skips the videos and keeps sending the photos.
