#!/bin/sh
# ⚠ NON-FUNCTIONAL against the Entertainment Room Apple TV as of 2026-08-26:
# pyatv 0.18 `play_url` is broken vs tvOS 26.6 — the AirPlay /play is accepted
# but no playback session starts and /playback-info returns 500. Verified with
# Apple's own reference HLS stream, so it is not a URL/format problem. Kept for
# when pyatv ships AirPlay 2 video support; the credential-extraction and
# stream-warm mechanics below are correct and tested.
#
# THE WORKING CAST PATH is mia HA `script.cast_frigate_birdseye`
# (camera.frigate_birdseye -> Google Cast receiver of the Samsung QN90F,
# media_player.qn90f9745 — same screen the Apple TV is plugged into).
# See scripts/proxmox/mia-proxmox/README.md.
#
# What this would do: cast the bnu Frigate "birdseye" all-cameras view to the
# Entertainment Room Apple TV (192.168.0.247) via AirPlay from mia-proxmox
# (deployed at /usr/local/bin/cast-birdseye).
# - The Apple TV enforces AirPlay pairing, so this reuses mia HA's stored
#   AirPlay credential, read AT RUNTIME from the HA VM via the QEMU guest
#   agent (protocol key "3" = AirPlay in pyatv). It is never written to disk.
# - The stream URL goes through this host's frigate-birdseye-hls-proxy.socket
#   (:1984 -> bnu-frigate) because neither the Apple TV nor mia HA can reach
#   the tailnet directly.
set -e

ATV_ID="32:AD:6A:31:05:F9"
# Progressive fMP4 by default; pass an alternative URL as $1 to experiment.
URL="${1:-http://192.168.0.21:1984/api/stream.mp4?src=birdseye}"

CRED=$(qm guest exec 100 -- cat /mnt/data/supervisor/homeassistant/.storage/core.config_entries \
  | python3 -c 'import sys,json; o=json.load(sys.stdin); d=json.loads(o["out-data"]); print(next(e["data"]["credentials"]["3"] for e in d["data"]["entries"] if e["domain"]=="apple_tv"))')

# Wake the birdseye exec producer at bnu so the first HLS request isn't a cold
# start (frame fetch spawns it; tolerate failure).
curl -s -m 25 -o /dev/null "http://100.116.190.49:1984/api/frame.jpeg?src=birdseye" || true

exec /opt/pyatv-venv/bin/atvremote --scan-hosts 192.168.0.247 --id "$ATV_ID" \
  --airplay-credentials "$CRED" "play_url=$URL"
