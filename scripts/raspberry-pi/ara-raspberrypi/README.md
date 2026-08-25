# ara-raspberrypi — canteiro Pi (House Hangar, Araquari SC)

The "computadorzinho" installed in the site shed (barracão) at the ARA build,
powered up together with the Starlink kit and the Intelbras camera on
2026-08-18 (home-ara DOC-2026-197/200). **ara is a home build, not a rack
site** — this Pi is not in `globalnet/architecture.yaml` and runs no Docker;
services here are plain systemd units.

| Fact | Value |
|---|---|
| Hardware | Raspberry Pi 4 Model B Rev 1.5, 8 GB RAM, 58 GB SD |
| OS | Raspberry Pi OS (Debian 13 trixie), aarch64, desktop image (LightDM) |
| Network | Wi-Fi `wlan0` on the house LAN `192.168.1.0/24` (Starlink router `192.168.1.1`, DHCP) |
| Tailnet | `ara-raspberrypi` (100.66.255.82) |
| SSH | `ssh eduardocenci@ara-raspberrypi` — key auth (see `REMOTE_ACCESS.md`) |

## LAN devices at ara (reachable only through this Pi)

| Device | Address | Notes |
|---|---|---|
| Intelbras iM9+ Full Color ("iM9 M Full Color-9411", model iM9-M) | `192.168.1.56` | Dual-lens site camera: RTSP channel 1 = **PT lens** (motorized, auto-tracking — aim it with [`ptz/`](ptz/)), channel 2 = fixed lens. RTSP always on at `:554` (Digest, user `admin`, password = the **Device Password** set in the Mibo app, `.env` `ARA_CANTEIRO_CAM_KEY`). ONVIF on `:80`. DHCP lease — pin a reservation in the Starlink app if it drifts. |
| Starlink router | `192.168.1.1` | House LAN gateway |

## Services

| Service | What it does |
|---|---|
| [`mediamtx/`](mediamtx/) | RTSP relay: pulls the iM9 camera streams and re-serves them on the tailnet at `rtsp://ara-raspberrypi:8554/canteiro` (consumed by the bnu-raspberrypi screen) |
