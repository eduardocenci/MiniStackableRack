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
| Media | `/media/frigate` on the LXC root disk — **64 GB since 2026-10-23** (`pct resize 105 rootfs +40G` on bnu-proxmox, online, no restart; was 24 GB and 80 % full). Recordings are event-gated (alerts/detections, 10 days, `mode: all`), no continuous retention — see *Recording & retention* below for what that really keeps |
| ffmpeg | bundled at `/usr/lib/ffmpeg/7.0/bin` (not in `PATH`) |
| Detector | CPU tflite `/models/cpu_model.tflite` — its labelmap has **no `truck`** (config warning if tracked; trucks come through as `car`); the OpenVINO CPU plugin fails to load in this LXC. **The truck TYPE on the canteiro (caçamba · munck · poliguindaste com caçamba · carroceria · prancha · betoneira · bomba) is not decided here** — see *Consumers* |
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

## Cameras

8 bnu house cameras (Hikvision NVR `192.168.0.22` + doorbell `10.1.1.65`)
**plus the ARA canteiro camera** (added 2026-08-26):

```
iM9+ camera (ARA LAN) ─▶ mediamtx on ara-raspberrypi2 ─tailnet/Starlink─▶ go2rtc on bnu-raspberrypi (10.1.1.123)
                                                     (1 copy per stream)      └─▶ this LXC: canteiro (HEVC main, record)
                                                                                  canteiro_sub (H264 640×480, detect)
```

**Never point this LXC at `ara-raspberrypi2` directly** — the bnu-raspberrypi
go2rtc is the single shared Starlink pull; a second main-stream reader
saturates the canteiro uplink (incident 2026-08-26, see
`scripts/raspberry-pi/bnu-raspberrypi/go2rtc/README.md`). Canteiro events
(person/vehicle) ride the same MQTT → HA pipeline and GenAI review
descriptions as the house cameras. Recordings are HEVC — Frigate handles
them fine; some browsers without HEVC decode may not play them back in the
web UI (use Edge/Safari or export).
