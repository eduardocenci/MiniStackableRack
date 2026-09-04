# mia-proxmox — host-level config

Proxmox host of the mia rack. LAN `192.168.2.20/24` (vmbr0, static; subnet 192.168.2.0/24 since the 2026-09-04 IP-plan cutover), tailnet
`mia-proxmox` (100.86.13.113). Guests: VM 100 `mia-homeassistant`
(LAN `192.168.2.21`), VM 101 Win11.

## Tailnet forwards for mia HA (`systemd/`)

**mia HA cannot originate connections to the tailnet** (its Tailscale add-on
provides inbound only — outbound `curl http://100.x…` times out from inside
HA). mia-proxmox sits on both networks, so two socket-activated
`systemd-socket-proxyd` units forward the bnu Frigate go2rtc ports onto the
rack LAN:

| Unit | Listens | Forwards to | Used by |
|---|---|---|---|
| `frigate-birdseye-proxy.socket` | `192.168.2.20:8554` | `bnu-frigate:8554` (RTSP) | mia HA `camera.frigate_birdseye` (generic camera, `rtsp://192.168.2.20:8554/birdseye`) |
| `frigate-birdseye-hls-proxy.socket` | `192.168.2.20:1984` | `bnu-frigate:1984` (go2rtc API/HLS) | LAN clients fetching HLS/MP4 directly |

Deploy: copy both `.socket`/`.service` pairs to `/etc/systemd/system/`,
`systemctl daemon-reload && systemctl enable --now '*.socket'`. The proxy
process exits after 5 min idle; the socket re-spawns it on demand.

## Casting Frigate to the entertainment-room TV

The working path is **mia HA `script.cast_frigate_birdseye`** — it runs
`camera.play_stream` (`camera.frigate_birdseye` → `media_player.qn90f9745`,
the Samsung QN90F's built-in Google Cast receiver). Casting wakes the TV from
standby; stop by turning the TV off. Full chain:

```
bnu Frigate birdseye (restream, continuous) ─tailnet─▶ mia-proxmox :8554 forward
  ─▶ mia HA generic camera ─HLS :8123─▶ Samsung QN90F cast receiver (192.168.2.81)
```

`cast-birdseye.sh` (deployed at `/usr/local/bin/cast-birdseye`) is the
**AirPlay attempt at the Apple TV — currently non-functional**: pyatv 0.18
`play_url` is broken against tvOS 26.6 (AirPlay /play accepted, no playback
session, `/playback-info` → 500; reproduced with Apple's reference HLS
stream). pyatv lives in `/opt/pyatv-venv`. Revisit when pyatv gains AirPlay 2
video. The Apple TV ("Entertainment Room", 192.168.2.80, pairing mandatory)
remains paired with mia HA; its AirPlay credential lives in HA's
`core.config_entries` and is read at runtime by the script, never stored.
