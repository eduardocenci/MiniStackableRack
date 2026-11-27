# mia-proxmox — host-level config

Proxmox host of the mia rack. LAN `192.168.2.20/24` (vmbr0, static; subnet 192.168.2.0/24 since the 2026-09-04 IP-plan cutover), tailnet
`mia-proxmox` (100.86.13.113). PVE node name is still `ply-proxmox`.

| Guest | What | LAN | Notes |
|---|---|---|---|
| VM 100 | `mia-homeassistant` | `192.168.2.21` | 4 GB |
| VM 101 | Win11 (`mia-win11`) | `192.168.2.22` | 4 GB (keep ≥ 4 GB) |
| LXC 102 | `mia-docker` — Docker host for the `senior-*` containers, since 2026-10-29 | `192.168.2.23` (static, **LAN-only**, not on the tailnet) | 4 cores · 3 GB · 40 GB; reach with `devtool.py guest mia 102 "…"`. Folder: [`../docker/mia-docker/`](../docker/mia-docker/README.md) |

No vzdump backup job is configured on this host (checked 2026-10-29).

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

## Wake-on-LAN (power-off rule for the nobreak)

The MiniPC can be shut down cleanly and woken over the network — tested 26/11/2026
from mia-raspberrypi:

| | |
|---|---|
| BIOS (AMI Aptio) | Advanced → AMD PBS → **Wake on PME** `Enabled`; Advanced → AMD CBS → FCH Common Options → Ac Power Loss Options → **Ac Loss Control** `Always On` (both were already set) |
| Linux | the NIC (`nic0`, r8169, MAC `b0:41:6f:07:e3:42`) boots with `Wake-on: d`; `systemd/wol-nic0.service` (live at `/etc/systemd/system/`, enabled) sets `wol g` at every boot |
| Shutdown | `shutdown -h now` → guests 100/101/102 stopped in 31 s, host log closed 40 s after the command (guest timeout 180 s each) |
| Wake | magic packet to `192.168.2.255` ports 9/7 sent from the Pi with the host off for 45 s → answering ping 37 s later; the three guests (`onboot: 1`) came up by themselves |

Check: `ethtool nic0 | grep Wake-on` → `g`. Not built yet: the NUT client that shuts
the host down on low battery, and the Pi-side wake when the grid is back.

Seen on the same day: the host clock ran ~75 s behind the Pi and `chronyc sources`
listed no source (`System clock synchronized: no`) — not fixed.
