# starlink-names — router names → netoverview nicknames (auto)

Every 5 min, asks the Starlink router who is associated to the Wi-Fi
(local gRPC `192.168.1.1:9000`, `SpaceX.API.Device.Device/Handle` with
`{"wifi_get_clients":{}}` — the same names the Starlink app shows) and
nicknames in netoverview any device that has none yet. Manual renames in
the netoverview UI are never overwritten.

Why: phones on the canteiro LAN use randomized (per-SSID-stable) MACs and
answer no reverse DNS/mDNS, so netoverview shows them as bare IPs. The
router knows their DHCP hostnames ("Galaxy-A54-5G", "A23-de-Edy"…). With
this sync, a phone gets its name minutes after it first connects, and the
daily presence report (`bnu-raspberrypi/canteiro-presenca/`) shows real
names instead of MAC stubs. Names are display-only: the Frigate WhatsApp
gate (bnu HA `frigate_whatsapp.py`) suppresses on ANY non-fixed device
online, nicknamed or not — whoever has the Wi-Fi credentials is assumed
allowed at the obra (decisão Eduardo 26/08/2026).

Notes:
- `wifi_get_clients` lists only currently-associated clients (the app's
  full device list with disconnected entries lives in the Starlink cloud,
  not on the router — probed 2026-08-26: no local roster RPC).
- The `Controller` entry (the router itself) is skipped; router and camera
  were hand-nicknamed ("Roteador Starlink", "Câmera do canteiro (iM9)").
- `grpcurl` v1.9.1 (official fullstorydev release, linux_arm64) is baked
  into the container image (a host copy from 2026-08-26 remains at
  `/usr/local/bin/grpcurl`).

Install: Docker container `starlink-names` since 2026-08-29
([`../docker/starlink-names/`](../docker/starlink-names/) — own image,
deliberately separate from the camera stack; supercronic every 5 min; the
old service+timer stay on the Pi disabled, one wave, as rollback). Test:
`docker exec starlink-names python3 /app/starlink-names.py` (prints
`done: N nickname(s) set`; never overwrites, so re-runs are safe).
