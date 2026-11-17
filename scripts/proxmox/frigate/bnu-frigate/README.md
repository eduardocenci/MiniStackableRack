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

- **Pipeline** ([`canteiro-mosaico.sh`](canteiro-mosaico.sh), rationale in its
  header): VAAPI decode of both mains → CPU `fps=15,scale=1280:720` +
  `hstack` → `h264_vaapi` → **H.264 2560×720, 15 fps, ~3 Mbps**, no audio;
  ~27 % of one core (measured). The all-GPU graph (`scale_vaapi` +
  `hstack_vaapi`) ran out of VAAPI surfaces on this Mesa 22.3. Timestamps
  are the streams' own clocks, not `-use_wallclock_as_timestamps` (ffmpeg 7
  choked the demuxer and one half fell 15–30 s behind) — the halves stay a
  constant ~1–3 s apart.
- **Output**: published over RTSP to `canteiro-hls` (mediamtx) on
  bnu-raspberrypi, `rtsp://10.1.1.123:8555/mosaico` — from there the `/live`
  page (`/hls/mosaico`), go2rtc `canteiro_mosaico` (TV cast, watchdog frame).
  See `scripts/raspberry-pi/bnu-raspberrypi/docker/canteiro-hls/README.md`.
- **Why a separate unit and not a go2rtc `exec:` source**: Frigate refuses
  `exec:` sources unless `GO2RTC_ALLOW_ARBITRARY_EXEC` is set (left alone),
  and a publisher that dies is a clean EOF for mediamtx's readers.
  **`config.yml` is unchanged** by the mosaic.
- **Failure mode**: any input EOF or 15 s stall ends ffmpeg; systemd restarts
  it 5 s later (`Restart=always`).

| File | Live copy (LXC 105) |
|---|---|
| [`canteiro-mosaico.sh`](canteiro-mosaico.sh) | `/usr/local/bin/canteiro-mosaico.sh` (755) |
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
