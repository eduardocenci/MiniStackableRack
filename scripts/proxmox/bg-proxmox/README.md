# bg-proxmox — host-level config

Proxmox host of the bg rack (Bento Gonçalves). LAN `192.168.0.120/24`
(vmbr0, static, gateway Claro HUMAX `192.168.0.1`), tailnet `bg-proxmox`
(100.93.109.40). Guests: VM 100 `bg-homeassistant` (LAN `192.168.0.115`),
VM 101 ubuntu, VM 103 Win11, LXC 102 plex. Firewall: `pve-firewall` disabled.

## Tailnet forwards for the bg LAN (`systemd/`)

bg-homeassistant and the bg TV cannot originate connections to the tailnet,
so bg-proxmox — on both networks — re-serves the two bnu services they need
on its LAN address:

| Unit | Listens | Forwards to | Used by |
|---|---|---|---|
| `waha-proxy.service` (socat, always on) | `192.168.0.120:3001` | bnu-proxmox `100.113.204.100:3001` → WAHA | bg HA `rest_command.send_whatsapp_bg` (`packages/whatsapp.yaml`) |
| `canteiro-tv-proxy.socket` (socket-activated `systemd-socket-proxyd`) | `192.168.0.120:1984` | bnu-raspberrypi go2rtc `100.91.64.62:1984` | bg HA `script.canteiro_na_tv` → the TV fetches `http://192.168.0.120:1984/api/stream.mp4?src=canteiro` |

Deploy: copy the unit files to `/etc/systemd/system/`, `systemctl
daemon-reload`, then `systemctl enable --now waha-proxy.service
canteiro-tv-proxy.socket`. The canteiro proxy process exits after 5 min idle
and the socket re-spawns it on the next connection; `FreeBind=true` lets the
socket bind before vmbr0 has its address at boot.

The forward exposes bnu's whole go2rtc API on the bg LAN — unauthenticated,
the same LAN + tailnet trust it already has at bnu (and that mia-proxmox
grants bnu Frigate's go2rtc).

## Canteiro na TV (bg)

On-demand cast of the ARA canteiro camera to the living-room TV, twin of
bnu's — see `../homeassistant/bg-homeassistant/packages/canteiro_na_tv.yaml`
for the chain and the start/stop webhook bookmarks (`BG_TV_WEBHOOK_ON/OFF`
in the repo-root `.env`).

- **Never pull from ara here.** bg reads bnu's go2rtc, which already holds the
  single Starlink copy; while the bg TV plays, bnu's WAN upload carries one
  extra copy of the main stream (~2 Mbps, measured 2026-10-29: HTTP 200,
  first byte after ~6 s waiting for a keyframe). bnu→bg tailnet path is
  direct (no DERP).
- **The first cast needs a one-time "Permitir" on the TV.** The Samsung
  receiver answers HA's launch with `USER_PENDING_AUTHORIZATION` and shows an
  allow/deny prompt; until someone accepts it with the remote, every cast
  times out after 30 s in HA (seen 2026-10-29 — how to diagnose it is in
  `REMOTE_ACCESS.md` → *BG site specifics*).
- TV: Samsung QN50Q7FAAGXZD (2025, Tizen, **Wi-Fi**, MAC `28:07:08:02:db:c1`)
  at `192.168.0.111` (DHCP — reserve it on the HUMAX). HA entities:
  `media_player.50_qled` (Cast — the one to use), `media_player.50_qled_2`
  (DLNA — Samsung's DMR drops endless live streams, dead end),
  `media_player.tv_sala_qn50q7faagxzd` (samsungtv, power/source).
