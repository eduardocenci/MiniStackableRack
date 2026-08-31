# bnu-frigate — Frigate NVR (bnu LXC 105)

Frigate 0.17 running **natively** (no Docker) in LXC 105 on `bnu-proxmox`:
a source checkout at `/opt/frigate`, launched by systemd units that wrap the
s6 run scripts. LAN `10.1.1.160` (DHCP); **also a tailnet node**
(`bnu-frigate`). Web UI `http://10.1.1.160:5000` (auth disabled; nginx also
listens on the LXC).

| Fact | Value |
|---|---|
| Active config | **`/config/config.yml`** — mirrored by [`config.yml`](config.yml) here (`<BNU_CAMERA_PASSWORD>` placeholder, value in `.env`) |
| ⚠ Decoy config | `/opt/frigate/config/config.yml` is the dev-checkout 16-line example — **not** the active file (cost a session real time on 2026-08-26) |
| Services | `frigate.service`, `go2rtc.service` (Frigate's internal go2rtc — its config is generated from `config.yml`'s `go2rtc:` block), `nginx.service` |
| Logs | `/dev/shm/logs/frigate/current`, `/dev/shm/logs/go2rtc/current` |
| Media | `/media/frigate` on the 24 GB LXC disk (78 % used 2026-08-26) — recordings are event-gated (alerts/detections, 10 days, `mode: all`), no continuous retention |
| ffmpeg | bundled at `/usr/lib/ffmpeg/7.0/bin` (not in `PATH`) |
| Detector | CPU tflite `/models/cpu_model.tflite` — its labelmap has **no `truck`** (config warning if tracked; trucks come through as `car`); the OpenVINO CPU plugin fails to load in this LXC |
| GenAI review | Ollama at `mia-desktop:11434` (`qwen3-vl:8b`), Portuguese descriptions per event |
| MQTT | `10.1.1.124` (bnu HA VM) — events feed Home Assistant |

## Config change flow (from this repo)

The mia-desktop ssh key is not authorized on bnu, so everything goes through
`devtool.py` (password fallback), staged via the Proxmox host:

```bash
python scripts/devtool.py run bnu-proxmox "pct pull 105 /config/config.yml /tmp/f.yml"
python scripts/devtool.py pull bnu-proxmox /tmp/f.yml ./f.yml
# edit ./f.yml; keep this folder's config.yml mirror in sync (masked password)
python scripts/devtool.py push bnu-proxmox ./f.yml /tmp/f.yml
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
iM9+ camera (ARA LAN) ─▶ mediamtx on ara-raspberrypi ─tailnet/Starlink─▶ go2rtc on bnu-raspberrypi (10.1.1.123)
                                                     (1 copy per stream)      └─▶ this LXC: canteiro (HEVC main, record)
                                                                                  canteiro_sub (H264 640×480, detect)
```

**Never point this LXC at `ara-raspberrypi` directly** — the bnu-raspberrypi
go2rtc is the single shared Starlink pull; a second main-stream reader
saturates the canteiro uplink (incident 2026-08-26, see
`scripts/raspberry-pi/bnu-raspberrypi/go2rtc/README.md`). Canteiro events
(person/vehicle) ride the same MQTT → HA pipeline and GenAI review
descriptions as the house cameras. Recordings are HEVC — Frigate handles
them fine; some browsers without HEVC decode may not play them back in the
web UI (use Edge/Safari or export).
