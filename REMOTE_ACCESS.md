# REMOTE_ACCESS.md — how to reach every device

**Single source of truth for reaching and managing anything in the fleet.**
If you are an LLM: read this file, then use `scripts/devtool.py`. Do not try
several methods until one works — every working method is recorded here, and
every method *not* listed here has already been tried and failed.

Last verified end-to-end: **2026-08-31** (post ply→mia rename, `.env` renamed)
— `python scripts/devtool.py test all` → **21/25**, identical to the
pre-migration baseline: the only fails are the 4 win11 nodes (known fleet-wide
key-auth breakage — use the guest agent). Full 25/25 baseline: 2026-07-30.

---

## 1. The one rule

```bash
python scripts/devtool.py test all
```

`scripts/devtool.py` encodes every credential lookup, username, and per-device
quirk below. Prefer it over hand-rolled `ssh`/`curl`. It is safe to run from any
directory.

| Need | Command |
|---|---|
| Check everything is reachable | `devtool.py test all` (or `test bnu`, `test bg-win11`) |
| Run a command on a device | `devtool.py run bnu-proxmox "qm list"` |
| Copy a file to a device | `devtool.py push bg-win11 ./x.ps1 C:\temp\x.ps1` |
| Copy a file from a device | `devtool.py pull mia-raspberrypi /etc/hostname ./h.txt` |
| Home Assistant REST call | `devtool.py ha bnu GET /api/states/sun.sun` |
| Inventory a rack's VMs/LXCs/containers | `devtool.py list bnu` |
| Run a command **inside** a VM or LXC | `devtool.py guest bnu 101 "docker ps"` |
| Reach a **LAN-only** device (§2) | `devtool.py lan bnu 10.1.1.132 "curl -sS http://10.1.1.132/"` |

## 2. Network layer — and what is *not* on it

Tailscale is the primary network. Tailnet nodes are named exactly
`<region>-<component>`, and this machine is itself a node, so **those devices are
reachable by bare name** (MagicDNS) with no VPN or port forwarding.

- Rack sites: **bnu, mia, bg, fln** (four — docs listing only three are stale).
  Home builds use the same naming (`ara-raspberrypi`) but are not rack sites.
  Personal clients (`cenci-surface9`, `cenci-macbook`, `iphone-…`) don't follow
  the pattern.
- `tailscale status` lists nodes and their `100.x` IPs. **A name resolving does
  not mean the device is up** — offline nodes still resolve.
- **Docker containers on the Pis do NOT automatically get MagicDNS.** Docker
  snapshots the host's `/etc/resolv.conf` when a container is *created*; after
  a Pi reboot `docker.service` starts the containers ~2 s **before** `tailscaled`
  rewrites resolv.conf, so a container that came up at boot inherits the
  router (`10.1.1.2` at bnu) and tailnet names fail with
  `Name or service not known` while `google.com` still resolves. Seen on
  bnu-raspberrypi after the 2026-09-03 reboot: globalnet showed every
  tailnet-probed node offline and the WAN panel null for all sites while the
  LAN-IP-probed bnu nodes stayed green. Fix: pin `dns: [100.100.100.100]` +
  `dns_search: [woodpecker-shark.ts.net]` in the compose file (done for
  globalnet, 2026-09-04) and `docker compose up -d` to recreate. Diagnose with
  `docker exec <c> cat /etc/resolv.conf` — look at the `ExtServers:` line.

### ⚠ Not every device is on the tailnet

This is the single most common cause of wasted attempts. **27 of 37 registered
devices are tailnet-addressed; 10 are LAN-only and their bare names do not
resolve at all.** Do not try `ssh bnu-docker` or `curl http://bnu-zigbee` —
those names do not exist.

| LAN-only device | Address | What it is |
|---|---|---|
| `bnu_docker` | `10.1.1.126` | **LXC 101** — hosts waha, waha-listener, condfy-bridge, netoverview-agent, psvis-tracker, weather-fusion (`:8791`) |
| `bnu_waha` / `bnu_listener` / `bnu_nta` | `10.1.1.126:3000/:8788/:5005` | the containers inside LXC 101 |
| `bnu_ollama` | `10.1.1.50:11434` | Ollama LXC 106 |
| `bnu_frigate` | `10.1.1.160` | Frigate LXC 105 — *also* a tailnet node (`bnu-frigate`), so either route works. **REST API without auth at `http://bnu-frigate:5000/api`** (Frigate 0.17.1, used 2026-09-08 to find who held a tablet at the ARA canteiro): `/api/events?camera=canteiro&after=<epoch>&before=<epoch>&label=person` → `/api/events/<id>/snapshot.jpg?bbox=1&crop=0` (640×480, the *detect* substream); `/api/canteiro/recordings/<epoch, fractional ok>/snapshot.jpg` → **full-res 2304×1296** frame from the *record* stream (404 where nothing was recorded — recordings only exist around motion/alerts; `/api/canteiro/recordings?after&before` lists the 10 s segments); `/api/review?cameras=canteiro&after&before` (GenAI `metadata` was null). Fetch frames every 2–15 s in a 4-thread loop, tile them with PIL and read the sheet — the 15-min Drive timelapse almost never catches the moment |
| `bnu_zb` | `10.1.1.132` | SLZB-06 Zigbee gateway (same pattern at other sites) |
| `bnu_nvr` | `192.168.0.22` | Hikvision NVR + 8 cameras |
| `bnu_doorbell` | `10.1.1.65` | Hikvision doorbell (ISAPI) |
| `bnu_gw` | `10.1.1.2` | EdgeRouter |

Reach them by hopping through the site's Proxmox host, which sits on both
networks — `devtool.py lan` does exactly that:

```bash
python scripts/devtool.py lan bnu 10.1.1.132 "curl -sS --max-time 10 http://10.1.1.132/"
python scripts/devtool.py lan bnu 10.1.1.126 "curl -sS http://10.1.1.126:8788/health"
```

Because these are LAN-addressed, their IPs are load-bearing and *do* appear in
config. Everywhere else, prefer tailnet names over IPs — names are stable, LAN
IPs are not.

**LAN-only devices cannot use tailnet names to reach each other.** MagicDNS
resolves only on tailnet nodes, so inside LXC 101 `getent hosts bnu-win11`
fails. When configuring one LAN-only service to call another host, use the LAN
IP and add a DHCP reservation — do not "improve" it to a tailnet name.

**Reaching LXC 101's HTTP services off-LAN (finance-hangar wa-sweep off-site).**
`devtool.py lan` is one-shot; a client that makes MANY HTTP calls
(`finance.listener_client` pulling a WhatsApp archive + media) needs a real
port-forward through `bnu-proxmox`. Plain `ssh -L` may fail (key auth — see
Tooling constraints), so use a **paramiko forwarder** (SSHClient with
`PROXMOX_PW` fallback + `transport.open_channel("direct-tcpip", …)` behind a
ThreadingTCPServer) forwarding BOTH ports — `18788 → 10.1.1.126:8788`
(waha-listener) **and** `13000 → 10.1.1.126:3000` (WAHA gateway: media-rescue
fallback AND `waha_send` group replies both hit it) — then run the pipeline
with `BNU_WAHA_LISTENER_URL=http://127.0.0.1:18788
BNU_WAHA_API_URL=http://127.0.0.1:13000` (`finance.config.cfg` lets env vars
override `.env`). Forwarding only 8788 makes every media fetch hang ~2 min in
the WAHA fallback before failing (seen 2026-08-26 from mia-desktop). Google
Sheets/Drive APIs need no tunnel. Note the Drive-for-Desktop mount and the
ms365 MCP are NOT available on every machine — a run without them files
sheet+local archive and leaves Drive uploads/share links as checklist items
(see the finance-hangar/ingest skills' degraded modes).

**Ready-made forwarder (2026-09-07): `python scripts/bnu_lan_forward.py`** — opens both
forwards (18788 + 13000) through `bnu-proxmox` and keeps them alive; start it in a
background shell (`nohup … &`), export the two `BNU_WAHA_*_URL` env vars and run the
pipeline as usual. Verified end-to-end on 2026-09-07 (wa-sweep off the bnu LAN: 45
messages + 18 media pulled, group send path reachable). Note: paramiko key auth to
`root@bnu-proxmox` is refused from this machine (`Authentication failed`) — the script
falls back to `PROXMOX_PW` automatically; plain `ssh` in Git Bash is unaffected.

### ARA (home build — dashboard site `home: true`, still not in devtool.py)

`ara-raspberrypi` is a tailnet node (the "computadorzinho" in the canteiro
shed): reach it with plain `ssh eduardocenci@ara-raspberrypi` (key auth since
2026-08-24; COMMON `RASPBERRYPI_LOGIN/PW` is the password fallback via
paramiko). `devtool.py` does **not** know ara — `devtool.py lan` cannot hop
here; hop manually through the Pi for the ara LAN-only devices. The Pi runs
the standard netoverview container (since 2026-08-26): what is on the house
LAN is visible at `http://ara-raspberrypi:5000` without SSH. Same day, ara
became a **dashboard site** in `globalnet/architecture.yaml` (`home: true` —
nodes `ara_rpi`/`ara_nto`, camera + Starlink router via `netoverview_probe`;
audited by `make fleet`), and the ara netoverview's `/api/presence` feeds
the daily 20:00 obra-presence WhatsApp report (`canteiro-presenca` container
on bnu-raspberrypi). `/api/presence?from=<ISO UTC>&to=<ISO UTC>` accepts **short windows** (5 min) — slice the day to pin a device's join/leave minute; `/api/events` only returns the last 10 rows, the full `device_events` table is in the container's SQLite (`docker exec -i netoverview python3 -`). **Plain `ssh eduardocenci@ara-raspberrypi` hung twice on 2026-09-08** (BatchMode, no banner within 90 s) while HTTP `:5000` answered instantly — prefer the APIs, and treat a hang as the Starlink path, not a key problem (plain `ssh` with `-o BatchMode=yes -o ConnectTimeout=25` answered in ~1 s all through 2026-09-10). **`docker logs` on `canteiro-relay` is half-broken (seen 2026-09-10)**: its json-file log (`/var/lib/docker/containers/<id>/<id>-json.log`, ~6.7 MB) carries a NUL byte from a shed power cut, so `docker logs --since …` silently returns **nothing** and a full/large-`--tail` read stops at 2026-08-30 15:45 — only a small `--tail N` (≤ a few hundred) reads the current end. For history, grep the raw file as root: `ID=$(docker inspect -f '{{.Id}}' canteiro-relay); sudo grep -a "no route to host" /var/lib/docker/containers/$ID/$ID-json.log` (`sudo -n` is passwordless on ara; timestamps are UTC). That is also where camera dropouts show: `[path canteiro] [RTSP source] dial tcp 192.168.1.56:554: connect: no route to host` = the camera left the LAN (ARP failure, ~40 s at a time, several times a day since 2026-08-31) — a `canteiro-timelapse` grab that lands on one fails with `DESCRIBE failed: 404` (relay has no source); the ffmpeg `Non full-range YUV` / `Could not open encoder before EOF` lines that can precede it are a side effect of the stream ending before a keyframe, not an encoder problem. `docker logs --since` works normally on the other ara containers. Who-was-there questions: cross netoverview presence with the condfy-bridge gate tags (LXC 101, `/data/condfy.db`) and the Frigate recordings above (visitor devices are the ones never seen before; Ênio Faqueti = `192.168.1.109`, own gate tag):

| ARA LAN-only device | Address | What it is |
|---|---|---|
| Intelbras iM9+ Full Color camera | `192.168.1.56` | dual-lens canteiro camera — RTSP `:554` (Digest, `admin` + `ARA_CANTEIRO_CAM_KEY`), ONVIF/CGI `:80`, relayed to the tailnet by the `canteiro-relay` container on the Pi (`rtsp://ara-raspberrypi:8554/canteiro`). Each lens also has a 640×480 H264 substream (`subtype=1`; channel 1's is relayed as `canteiro-sub` — the bnu Frigate detect feed since 2026-08-26). ONVIF **events work** (PullPoint, probed 2026-08-26): topics are motion/tamper/scene-change only — **no person/vehicle classification locally** (that stays in the Imou cloud/Mibo app; CGI remains 401) |
| Starlink router | `192.168.1.1` | house LAN gateway (DHCP for the whole `192.168.1.0/24`). **Local gRPC API works** (`192.168.1.1:9000`, reflection on): `grpcurl -plaintext -d '{"wifi_get_clients":{}}' 192.168.1.1:9000 SpaceX.API.Device.Device/Handle` → associated clients with **name+MAC+IP** (what the app shows; `wifi_set_client_given_name` also exists). No local roster of DISCONNECTED clients (that list lives in the Starlink cloud — probed 2026-08-26). `grpcurl` v1.9.1 installed at `/usr/local/bin` on the Pi; the `starlink-names` container syncs these names into netoverview nicknames every 5 min |
| Starlink dish | `192.168.100.1` | behind the router (any LAN client reaches it). **Local gRPC API works** (`192.168.100.1:9200`, plaintext, no auth, reflection on — probed 2026-08-30): `get_status` = instantaneous down/uplink throughput, pop latency, obstruction, alerts; `get_history` = 900 s of 1 Hz ring buffers (throughput, latency, drop rate, **`powerIn` watts**) + outage event log. **Relayed onto the tailnet as `ara-raspberrypi:9200`** by the `starlink-proxy` socat container (since 2026-08-30) — globalnet reads the ARA live WAN ▼▲ + dish ⚡ from it. Same `Device/Handle` service as the router, different RPCs |

> **Presence-report phantom (learned 2026-08-31, FIXED same day):** a
> `docker build`/first run on the Pi briefly attaches a container to Docker's
> default bridge — docker0 comes UP and netoverview logged `172.17.0.1` +
> `172.17.0.2` (random MAC → the 20:00 WhatsApp report rendered it
> `aparelho …<mac-suffix>`, e.g. `…c4:b4:79` on 31/08) for one 5-min scan
> cycle, then both vanished. Any `172.17.0.x` row is the Pi's own Docker,
> never a person on site — all production containers are `network_mode: host`.
> Fixed at the root in netoverview `d7f312a` (IPv4 scan now skips
> docker0/veth/br-*/VPN interfaces, all sites) plus a `LAN_CIDR`
> (192.168.1.0/24) filter in canteiro-presenca.py as defense in depth; the
> ghost rows were deleted from the ara netoverview DB (`devices` +
> `device_events`, via `docker exec -i netoverview python3 -` + sqlite3 —
> the `DELETE /api/device/<ip>` endpoint leaves events behind). Same pair
> wiped from the bnu DB (29/08 canteiro-jobs build blip); bg/fln were
> clean; mia was offline 31/08 — if its dashboard ever shows `172.17.0.x`,
> delete the rows the same way.

### MIA site specifics (learned 2026-08-26 as PLY; site renamed 2026-08-31)

- **mia is the former ply** (rack migrated Plymouth → Miami, renamed 2026-08-31).
  The `<site>-*` tailnet names at this site are **admin-console name pins**, not
  OS hostnames (OS hostnames are generic: `desktop`, `raspberrypi`,
  `NAS_DS918plus`, `glkvm`, `DESKTOP-82PM8U0`) — renaming was done in the
  Tailscale admin console, plus `tailscale set --hostname mia-*` on each device
  so the advertised hostname matches. **Exception: mia-proxmox's OS/PVE node
  name is still `ply-proxmox`** (its tailnet name was never pinned, so the CLI
  rename flipped MagicDNS instantly; the /etc/pve node rename was deliberately
  deferred — cosmetic in the PVE UI only, procedure in
  `updateCycles/20260831_ply-to-mia-rename.md`).

- **mia LAN is `192.168.2.0/24` since the 2026-09-04 IP-plan cutover** (was
  `192.168.0.0/24`): UCG Max `.1`, mia-proxmox `.20` (static), HA VM `.21`,
  win11 `.22`, Pi `.10`, glkvm `.11`, SLZB-06 `.12`, NAS `.15`, mia-desktop
  wired `.30` — every permanent device has a UCG DHCP reservation in its block,
  see *Fleet IP plan* below. The desktop reaches every rack device directly.
  The ISP side behind the UCG is still `192.168.1.0/24` (`192.168.1.1` answers
  HTTP; routed via `.2.1`).
- **Presence at mia** (2026-09-05): HACS **`mudape/iphonedetect` 2.5.0** ("iPhone
  Device Tracker", the same integration bnu/bg/fln use — polls a fixed LAN IP with
  UDP-5353 probes + the ARP table) with one entry **Duda** = Eduardo's iPhone 17 Pro
  Max at its UCG reservation `192.168.2.40` (`consider_home: 300`, entity
  `device_tracker.duda`, `source_type: router` — went `home` within 5 s of the
  entry being created while the phone sat on SSID `Cenci`). `person.eduardo_cenci`
  = `device_tracker.duda` + the Companion-app tracker
  `device_tracker.cencis_iphone_15_pro_2` (that `mobile_app` registration is model
  `iPhone18,2` = the 17 Pro Max; its device and config-entry title were renamed
  "Cenci's iPhone 17 Pro Max" in HA, entity ids left as they were). The older
  `…_15_pro` registration (model iPhone16,1, idle since 2026-04-21) was dropped
  from the person but its entry was not deleted. The `mobile_app` trackers report
  `unknown` on every site (the app never sends a location) — LAN presence via
  iphonedetect is the one that actually works.
- **mia-proxmox cannot resolve public DNS names** (2026-09-05): its
  `/etc/resolv.conf` is Tailscale-managed and lists only `100.100.100.100`
  (MagicDNS); `getent hosts updates.smlight.tech` fails while `ping 8.8.8.8`
  works. Any `curl https://…` from the host times out on "Resolving". Until
  fixed (`tailscale set --accept-dns=false` + a real nameserver, or a global
  nameserver in the tailnet DNS settings), download on **mia-raspberrypi**
  (same LAN, DNS fine) and push from there.
- **mia HA cannot originate connections to the tailnet** (Tailscale add-on is
  inbound-only): `curl http://100.x…` from inside HA times out. When mia HA
  must consume a tailnet service, forward it onto the rack LAN from
  mia-proxmox — pattern: socket-activated `systemd-socket-proxyd` units, see
  `scripts/proxmox/mia-proxmox/` (`192.168.2.20:8554/:1984` → `bnu-frigate`,
  feeds `camera.frigate_birdseye`).
- **mia Kasa HS300 power strips** — TWO, both on SSID `Cenci-IoT` (= the
  rack `192.168.2.0/24`), both **cloud-free since 2026-09-01** (factory reset
  + local provisioning, no Kasa account; discovery `owner` empty, default
  creds `kasa@tp-link.net`/`kasaSetup`): **Power Strip Desk** `192.168.2.50`
  (reserved, UCG client still named `hs300-rack`; MAC `E0-D3-62-D0-F8-45`,
  fw 1.1.2 → **KLAP v2** on :80, port 9999 closed; it feeds the WORK DESK,
  not the rack — outlets Fully Stand-Up Desk / Anker USB Charger / Desktop /
  Sonos Era 100 / LED Strip / Monitor, renamed on the strip 2026-09-04 via
  `hs300_local.py names`; entity ids still carry the old `minirack_*`
  suffixes; HA entry `01KMXYA5Z7D3JHRXFN52RQ177P` "Power Strip Desk HS300") and
  **Power Strip Living Room** `192.168.2.51` (reserved, UCG client `hs300-2`;
  MAC `…FD-B8`, fw 1.0.11 → legacy :9999; TV cabinet — outlets LED Strip
  Bottom Cabinet / Empty / LED Strip Ambient Light TV / Apple TV / Sonos Arc
  Ultra / TV 75, named 2026-09-04; HA entry `01M1FS8A0MBCHZPN7M8MPES6FN`). **python-kasa 0.10.2 (= what HA 2026.8 pins)
  cannot talk to fw 1.1.2**: it maps `IOT.KLAP` to the v1 transport and
  builds an `IotPlug` (no outlets) — upstream python-kasa#1604 open. mia HA
  runs the `klap2_patch` custom integration (repo
  `scripts/proxmox/homeassistant/mia-homeassistant/custom_components/klap2_patch/`)
  that fixes both in-process; desktop tooling with the same workaround is
  `scripts/kasa/mia-hs300/` (`hs300_local.py probe|verify|names`, from any
  tailnet machine). Provisioning a reset strip runs from **mia-raspberrypi's
  wlan0** (`~/pi_ap.sh up <suffix>` / `down`, then python-kasa 0.10.2 inside
  an arm64 container: `docker run --rm --network host --security-opt
  seccomp=unconfined --platform linux/arm64 -v ~/kasa310:/venv
  python:3.12-slim-bookworm /venv/bin/python /venv/join_v2.py SSID PSK 4`) —
  the Pi is 64-bit kernel / 32-bit OS (Python 3.9), plain `pip` there gets
  python-kasa 0.7.7 at most, and Docker there silently reuses an arm/v7
  image unless the tag is new (+ needs `seccomp=unconfined` or SIGSYS 159).
  The strip sits at `192.168.0.1` on its setup AP; since the rack LAN moved
  to `192.168.2.0/24` that no longer collides with the Pi's gateway/DNS.
  DHCP reservations done 2026-09-04 (`.50`/`.51`); still open: block both
  MACs from WAN on the UCG.
- **mia HA runs the Matter Server add-on** (`core_matter_server` 9.2.0,
  installed 2026-09-04 via `ha addons install/start`; HA `matter` entry
  created through the config-flow REST API — `POST
  /api/config/config_entries/flow {"handler":"matter"}` then
  `{"use_addon": true}`). HA host IPv6 is on (ULA + link-local on
  `enp6s18`), which Matter needs. First Matter node: **Tapo P316M** 6-outlet
  strip "Power Strip Rack" at **`192.168.2.52`** (UCG reservation, IoT power
  block; MAC `58-D8-12-37-0C-DA`, fw 1.3.0, serial `58D812370CDA`), per-outlet
  power/energy — entity map in `scripts/kasa/mia-hs300/README.md`. Tapo
  Matter devices pair over **Bluetooth** (BLE name `P316M_xxxxxxxx`, seen from
  mia-raspberrypi's `bluetoothctl scan on`), they do NOT open a Wi-Fi setup
  AP, so the python-kasa/`pi_ap.sh` route does not apply — commissioned from
  the HA Companion app on the iPhone (phone BLE); the HA VM has no Bluetooth
  adapter. Never touched by the Tapo app → no TP-Link account (`owner=no`).
  Moving a Wi-Fi client into its block after the fact: set the reservation
  (`PUT /rest/user/<_id>`), then `POST /cmd/stamgr {"cmd":"kick-sta","mac":…}`
  — the P316M renewed to `.52` in ~25 s and HA's Matter link followed via
  mDNS without any reload. The UCG login file `/root/.ipcut.env` on
  mia-proxmox is **not kept** — stage it from `.env` (`MIA_UNIFI_USER/PASSWORD`
  as `USER=`/`PASSWORD=`) for the run and delete it after; run API scripts
  there detached (`setsid nohup … &` + log), the SSH channel times out on
  anything that polls for more than ~60 s.
- **Pi Wi-Fi radios as scanners** (seen 2026-09-04, `iw dev wlan0 scan`):
  mia and bnu Pis scan fine (wlan0 down by default, `sudo ip link set wlan0
  up` first). **bg-raspberrypi wlan0 is RF-killed** (`Operation not possible
  due to RF-kill` — needs `sudo rfkill unblock wifi`, not done yet).
  **fln-raspberrypi has no passwordless sudo** for `eduardocenci` (`sudo: a
  password is required`) — use `RASPBERRYPI_PW` via `sudo -S`, or fix
  sudoers; every other Pi is NOPASSWD.
- **Rack-LAN media devices** (not in HA `.env`, discovered via pyatv scan +
  HA): Apple TV 4K "Entertainment Room" `192.168.2.80` (tvOS 26.6, AirPlay
  pairing mandatory; paired with mia HA — credential lives in HA
  `core.config_entries`, pyatv protocol key `3`); Samsung QN90F 75"
  `192.168.2.81` (**has a Google Cast receiver**, HA
  `media_player.qn90f9745` — casting to it wakes the TV from standby); Sonos
  Arc Ultra `.212` + Era 100 `.236` (AirPlay, no pairing needed, audio only).
- **Venstar Explorer Mini T2000 thermostat** `192.168.2.70` (hostname
  `THERMOSTAT`, MAC `1c:63:49:6d:a3:69`, TI Wi-Fi module; DHCP — reserve it on
  the UCG). Local API = plain HTTP, no auth, port 80, cloud-free:
  `devtool.py lan mia 192.168.2.70 http://192.168.2.70/query/info`. The API
  is **off by default** and the switch is *Setup step 27* on the unit (hold
  MODE+FAN 5 s, MODE ×26, WARMER → ON, MODE+FAN 5 s to exit); the "API
  STATUS" line under FAN-5 s → MODE is read-only and fooled us on 2026-09-04
  (port 80 "connection refused" until step 27 was set). In HA as
  `climate.thermostat` (integration `venstar`, added 2026-09-04); package
  `packages/thermostat_sen55.yaml` makes it regulate on the Apollo AIR-1 SEN55
  via `input_number.room_target_temperature` — set the target there, not on
  the climate card. Not in `architecture.yaml` (end device, like the HS300s).
- **HA home location was still Plymouth, MI (42.431, −83.471, "Plymouth") until
  2026-09-06** — sun, zones and the Met.no `weather.forecast_casa` were computed
  for Michigan. Moved to Brickell 33130 (25.7617, −80.1918, elev 2 m, "Casa MIA")
  over the websocket (`config/core/update`; REST has no endpoint for it). Met.no
  tracks home and followed automatically; `zone.home` state = number of persons
  home. Check `GET /api/config` → `latitude/longitude` before trusting any
  sun-angle or weather-based logic at a relocated site.
- **InfluxDB 2 on mia-nas** (`influxdb:2.7`, deployed 2026-09-07) is the
  never-purged thermal-model store — org `casa-mia`, bucket `house`, infinite
  retention, admin token `MIA_INFLUXDB_TOKEN`. It **binds the NAS rack-LAN IP
  `192.168.2.15:8086`, not the tailnet**: verified the HA Core container reaches
  `192.168.2.15` (LAN) but NOT the NAS tailnet IP `100.110.80.51` nor a
  `127.0.0.1` bind (copyparty's loopback bind is NAS-local only). Reach it via
  the site Pi/Proxmox hop — `devtool.py lan mia 192.168.2.15
  "curl -s http://192.168.2.15:8086/health"` — or on the NAS itself
  `docker exec influxdb influx query '...' --org casa-mia` (admin token baked
  into the container's active influx CLI config, named `thermo`). The admin token was ROTATED 2026-09-07 (the first one leaked into a process listing); it is all-access, the sole token, and lives in `.env`/`secrets.yaml`/the NAS compose `.env`. `influx config set` needs `--config-name`; to repoint the CLI use `influx config create --config-name thermo --host-url http://localhost:8086 --org casa-mia --token <tok> --active`. **CRITICAL rotation gotcha (2026-09-07):** HA 2026.8's `influxdb:` integration is imported into a CONFIG ENTRY (`.storage/core.config_entries`, domain influxdb, source import) that CACHES the token — editing the YAML/`secrets.yaml` does NOT update it (imported entries keep their token), so HA kept sending the deleted token and every write 401'd silently (InfluxDB `docker logs` showed `Unauthorized: authorization not found`; HA logged nothing). Fix: `DELETE /api/config/config_entries/entry/<id>` then restart so HA re-imports the YAML with the new token. Quote the token in secrets.yaml (base64 with `=`/`_`). Diagnose a dead HA→influx write path with `docker logs influxdb | grep Unauthorized` and a tcpdump of `:8086` to see the actual token sent. HA streams an allow-list to it via
  `packages/influxdb_stream.yaml` (host/token in `secrets.yaml`:
  `influxdb_host`/`influxdb_token`). Source + deploy notes:
  `scripts/synology/mia-synology/docker/influxdb/`. Like copyparty, it is a NAS
  Docker service documented in its folder, **not** an `architecture.yaml` node
  (central GlobalNet on bnu can't reach a MIA LAN IP, so a `check_url` would
  read red). The recorder on HA is now a 60-day cache with per-outlet
  voltage/current and AIR-1 housekeeping excluded (`packages/recorder_prune.yaml`).
- **Roborock robot vacuum at mia is a Saros 10R** (`roborock.vacuum.a144`,
  fw 02.52.32, MAC `24:9e:7d:47:5c:f7`) at **`192.168.2.73`** — UCG client
  `roborock-saros-10r` (renamed from `roborock-s8-pro-ultra` 2026-09-05 via
  `PUT /rest/user/<_id>` run from mia-raspberrypi, which reaches
  `https://192.168.2.1` fine when mia-proxmox SSH is flaky).
  Local API = Roborock V1 protocol, TCP `58867` (+ UDP `58866`), AES with the
  per-device `localKey` that only the Roborock cloud hands out — no HTTP, no
  auth-less endpoint. HA 2026.8.2 (`roborock` entry
  `01KMXE08Y11YZTVE9N50DYF8WS`, python-roborock) holds an ESTABLISHED session
  to `.73:58867` for commands/status AND an MQTT session to
  `mqtt-us-3.roborock.com:8883` — maps, routines and the initial
  login/`localKey` fetch stay cloud-side; the robot blocks its local API when
  it cannot reach Roborock, so do not WAN-block it. Reachability check:
  `devtool.py run mia-proxmox "bash -c '</dev/tcp/192.168.2.73/58867'"`.
  Fully cloud-free options: Valetudo does NOT support any S8/Saros (list is
  exhaustive); `Python-roborock/local_roborock_server` (HA add-on that
  impersonates the Roborock cloud after a one-time account snapshot) lists
  Saros 10R fw 02.52.32 as confirmed working — not deployed. **Known stall:**
  all Saros entities flip to `unavailable` while the entry stays `loaded` and
  the `.73:58867` session stays ESTABLISHED (2026-09-06 01:03 UTC, mid-clean;
  home-assistant/core#152159) — reload the entry, back in <10 s:
  `devtool.py ha mia POST /api/config/config_entries/entry/01KMXE08Y11YZTVE9N50DYF8WS/reload`.
  Room clean = `vacuum.send_command` `app_segment_clean` `[{"segments":[8],"repeat":1}]`
  (segment ids via `roborock.get_maps`; map "MIA" = 1 Living room · 2 Kitchen · 3 Hall ·
  4 Master bedroom · 5 Bathroom_Master · 6 Guest bedroom · 7 Bathroom_Guest · 8 Study —
  Room 9 merged into 6 on 2026-09-06). Map edits are raw commands too: `merge_segment`
  `[6, 9]`, then `name_segment` `[{"miRoomId":"<cloud iot id>","robotRoomId":<seg>},…]`
  for every segment (a merge blanks the survivor's name); cloud room ids from
  `POST /api/services/shell_command/roborock_rooms?return_response` (package
  `roborock_tools.yaml` + `/config/scripts/roborock_rooms.py`, python-roborock inside
  the core container — first package needing `shell_command` required an HA restart,
  ~30 s). `get_maps` is cached: reload the entry to see edits.
  **Read-only robot/dock settings and clean records** (2026-09-06):
  `POST /api/services/shell_command/roborock_query?return_response`
  `{"args": "get_status get_smart_wash_params get_wash_towel_mode app_get_dryer_setting get_clean_record:5"}`
  — `/config/scripts/roborock_query.py` (same package) runs python-roborock **5.31.1**
  (HA 2026.8.2; the `roborock.devices.*` trait/channel layout — no `version_1_apis`,
  no `cloud_api`) inside the core container over the **local L01 protocol only**
  (`LocalChannel` + `RpcChannel`, localKey from the cloud home data), so it never opens a
  second cloud MQTT session with HA's client id. Any `get_*` RPC works
  (`method=<json params>` for params; `get_clean_record:N` = last N records with
  `wash_count`/`extra_time`). Zero-connection alternative for what HA already holds
  (full status, clean summary, rooms, device features, but not per-clean records):
  `GET /api/diagnostics/config_entry/01KMXE08Y11YZTVE9N50DYF8WS`. `shell_command`
  entries reload without a restart: `POST /api/services/shell_command/reload`.
  Map PNG = `GET /api/image_proxy/image.living_room_saros_10r_mia`
  with the HA bearer token. Dashboard: sidebar *Vacuum* (`robot-vacuum`),
  source `scripts/proxmox/homeassistant/mia-homeassistant/dashboards/dashboard-vacuum.yaml`.
- **Identifying a Samsung Tizen device — ask the TV, not the router.** Any
  Tizen set answers an unauthenticated `GET http://<ip>:8001/api/v2/` with
  `modelName`, `name`, `resolution`, `networkType`, `wifiMac`, `PowerState`.
  For `.228` (verified 2026-09-01): `modelName QN75QN90FAFXZA`, `25_RSM_QTV`,
  `75" Neo QLED`, `3840x2160`, `networkType wireless`, wifiMac
  `80:0D:3F:8C:2F:1C`. `curl` to LAN IPs may be permission-blocked on the Mac
  — `python3 -c "import urllib.request;…"` goes through.
  ⚠ **UniFi mislabels this TV as a monitor.** Its fingerprint DB matches MAC
  `80:0d:3f:8c:2f:1c` to `dev_id 5424` = *"Samsung Odyssey G7 Monitor"*
  (confidence 89), so the UniFi app/console shows an Odyssey **monitor** that
  does not exist on this network. There has never been an Odyssey attached to
  mia — 60 known clients, exactly one Samsung. Trust `:8001/api/v2/`.
  The real **57" Odyssey Neo G9 is mia-desktop's monitor**, driven over
  DisplayPort — it is not a network client and never will be. (It is why
  `updateCycles/fleet-update.SKILL.md` builds a 32:9 / 7680×2160 wallboard
  artifact.) `.228` is confirmed the QN90F independently by
  `scripts/proxmox/mia-proxmox/README.md`.
- **mia UniFi controller API works from any tailnet/LAN machine** (`MIA_UNIFI_URL`
  `/USER`/`/PASSWORD`, self-signed → `verify=False`): `POST /api/auth/login`,
  reuse the session cookie + `X-CSRF-Token` from the login response, then
  `GET /proxy/network/api/s/default/stat/sta` (active clients: `is_wired`,
  `essid`, `ap_mac`, `signal`, `radio`), `/rest/user` (all 60 ever seen,
  `first_seen`/`last_seen`), `/stat/device` (APs + `vap_table` BSSID→SSID map),
  and `/proxy/network/v2/api/fingerprint_devices/0` (resolves `dev_id`/
  `vendor_id`/`family_id` to names). This is the only source that answers
  wired-vs-wireless, which SSID, and which AP. APs: **U7 Pro**
  `a8:9c:6c:72:f7:50` is the only radio — SSIDs `Cenci` (ng `…f7:51`, na
  `…f7:52`, 6e `…f7:53`), `Cenci-IoT` (`b6:9c:6c:72:f7:51`), `Cenci-guest`
  (`b2:9c:6c:72:f7:51`); switches USW Flex 2.5G 8 PoE + USW Ultra; UCG Max `.1`.
  Note the QN90F and the Apple TV sit on **`Cenci`**, not `Cenci-IoT` (where
  Sonos + both HS300 strips live), and the QN90F associates at **2.4 GHz
  802.11n** (`radio ng`, −49 dBm) despite 5/6 GHz being available on the same
  SSID — worth pinning to `na` before casting 4K to it.
- **pyatv `play_url` is broken vs tvOS 26.6** (AirPlay /play accepted, no
  playback session, `/playback-info` → 500 — fails even with Apple's reference
  HLS). HA's `apple_tv` integration uses the same library AND hard-routes any
  `media-source://` id down the RAOP *audio* path (`media_type = MUSIC` in
  `async_play_media`), so **video to the Apple TV is currently impossible**;
  cast video to the QN90F receiver instead (`script.cast_frigate_birdseye`).

- **Bambu Lab P2S 3D printer at mia** (2026-09-05): `192.168.2.72` (UCG
  reservation renamed `bambu-x1c` → **`bambu-p2s`**, MAC `60:32:3b:9f:fc:f8`, SSID
  Cenci-IoT). The UniFi fingerprint and the older docs said "X1C"; SSDP says
  **P2S**: model code `N7`, serial `22E8AJ581301903`, fw 01.02.00.00, hw AP02,
  one **AMS 2 Pro** (hw N3F05). Identify any Bambu printer from **mia-proxmox**
  with SSDP: bind UDP 2021 (join `239.255.255.250`), send `M-SEARCH … ST:
  urn:bambulab-com:device:3dprinter:1` to `239.255.255.250:1990` — the reply
  carries `USN` (= serial), `DevModel.bambu.com`, `DevName`, `DevConnect`
  (`cloud`/`lan`) and `DevVersion` (ship the script as `echo <base64> | base64 -d
  > /tmp/x.py && python3 /tmp/x.py` through `devtool.py run`). Open ports:
  **8883** MQTT/TLS, **990** FTPS, **6000** chamber camera; **322 (RTSPS) closed**
  = *LAN Mode Liveview* off on the printer. **Local MQTT works with the LAN access
  code while the printer stays cloud-bound and Developer Mode is OFF** — user
  `bblp`, password `MIA_BAMBU_P2S_ACCESS_CODE` (root `.env`, serial in
  `MIA_BAMBU_P2S_SERIAL`), topics `device/<serial>/report|request`, self-signed
  cert (`CERT_NONE`); `{"pushing":{"command":"pushall","sequence_id":"0"}}`
  returns the full state. HA: HACS **`greghesp/ha-bambulab` 2.2.25**
  (`bambu_lab`, entry `01M1T7HD30E09JP6FGS1H2FR2M`; entry title and device names
  `P2S` / `P2S External Spool` / `P2S AMS 2 Pro`, entity ids shortened to
  `*.p2s_*`) added over REST: `{"printer_mode":"lan"}`, then the `Lan` step with
  `host`, `serial`, `access_code`, the numeric fields **as strings**
  (`"print_cache_count":"100","timelapse_cache_count":"1","usage_hours":"0"` —
  ints are rejected `expected str`) and the expandable section as a dict
  (`"advanced":{"disable_ssl_verify":false,"enable_firmware_update":false}`).
  The first setup created only the printer + External Spool devices (the full
  MQTT state was not in yet); **reloading the entry created the AMS 2 Pro device**
  (13 entities). Gaps: (1) **no pause/resume/stop buttons, fan or speed
  controls** — this firmware wants *signed* MQTT commands
  (`print_fun.mqtt_signature_required`, `binary_sensor.p2s_developer_lan_mode`
  off); only **Developer Mode** on the printer screen unlocks control entities,
  monitoring needs nothing. (2) **`camera.p2s_camera` fails to load while *LAN Mode Liveview* is off**
  (`camera.py:115` `urlparse(rtsp_url).netloc.split(':')` → `TypeError bytes/str`
  because the printer reports `ipcam.rtsp_url = "disable"`/None — upstream bug).
  Liveview was switched on at the printer screen on 2026-09-05: it then reports
  `rtsps://192.168.2.72:322/streaming/live/1`, port 322 opens, and after
  `POST /api/config/config_entries/entry/<id>/reload` the camera loads —
  `GET /api/camera_proxy/camera.p2s_camera` returns a 1080p JPEG (~110 KB in 2 s);
  HA's MJPEG proxy (`/api/camera_proxy_stream/…`, what the bundled minimal card's
  camera pane uses) returns the integration's black "!" placeholder (`camera_image()`
  is hard-coded to it) — use stills (`picture-entity` `camera_view: auto`, ~1 s 1080p frames
  through go2rtc) or the WebRTC live view (`camera_view: live`, go2rtc). **The printer accepts ONE RTSPS client**: when
  the frontend falls back to HLS (legacy `stream` worker) it and go2rtc alternate
  `i/o timeout` / `Error demuxing stream` log lines until the HLS player is gone — a `live`
  dashboard card re-triggered that on every reload, so the mia dashboard uses `auto`;
  Bambu Studio's LAN liveview competes for the same slot. **Bundled Lovelace cards** (resource `/bambu_lab/ha-bambulab-cards.js?v=0.6.53`,
  auto-registered by the integration; the docs only say "use the card editor", the
  keys below were read from the JS): `custom:ha-bambulab-print_status-card`
  (`printer:` device id, `style: simple|minimal|graphic`; minimal adds
  `show_printer_name`, `show_cover`, `cover_position`, `show_camera_feed`,
  `camera_position`; simple adds `custom_camera`; all take
  `custom_humidity/temperature/light/power`), `custom:ha-bambulab-ams-card` (`ams:`
  device id, `style: vector|graphic`, `show_info_bar`, `subtitle`, `show_type`,
  `spool_anim_reflection/wiggle`), `custom:ha-bambulab-spool-card` (`spool:` device
  id, `tray` 1–4, `show_info_bar`, `show_type`, `subtitle`),
  `custom:ha-bambulab-print_control-card` and `custom:ha-bambulab-skipobject-card`
  (`printer:`) — the control card drives the pause/resume/stop buttons, i.e. needs
  Developer Mode, so it is not on the dashboard. HACS has exactly one Bambu card, `drkpxl/printwatch-card`
  1.2.0 (2025-02-03, P1S-era entity names) — installed on mia 2026-09-05 for
  comparison, verdict in the dashboards README. Dashboard: sidebar **Printer** (`3d-printer`),
  source `scripts/proxmox/homeassistant/mia-homeassistant/dashboards/dashboard-printer.yaml`.

### Fleet IP plan (decided 2026-09-04)

Every site keeps its own /24 (no two sites overlap, so subnet routes can be
advertised on the tailnet later), and **the last octet means the same thing
at every site** — `.20` is always the Proxmox host, `.21` always the HA VM.

| Site | LAN | Gateway | Status |
|---|---|---|---|
| bnu | `10.1.1.0/24` | `.2` EdgeRouter (`.1` unused) | stays; hosts not yet in the blocks (bnu last) |
| mia | `192.168.2.0/24` | `.1` UCG Max | **cut over 2026-09-04** — reservations on the UCG |
| bg | `192.168.0.0/24` | `.1` Claro HUMAX | stays; blocks not applied yet |
| fln | `192.168.0.0/24` → `192.168.3.0/24` | `.1` Claro HUMAX | to do — check the modem can hold reservations, else a router behind it in bridge mode |
| ara | `192.168.1.0/24` | `.1` Starlink | fixed (Starlink subnet not configurable) |

| Block | Category | Fixed slots |
|---|---|---|
| `.1` | Gateway | |
| `.2–.9` | Network gear | switches, APs (static in the device config) |
| `.10–.19` | Rack | `.10` Pi · `.11` GL KVM · `.12` Zigbee gateway · `.15` NAS · `.16` NAS VM |
| `.20–.29` | Proxmox | `.20` host (static) · `.21` HA VM · `.22` win11 VM · `.23+` LXCs |
| `.30–.39` | Computers | desktops, laptops (`.30` desktop wired, `.31` its Wi-Fi NIC) |
| `.40–.49` | Cellphones | phones, watches, e-readers — reserve the SSID's *private* (per-network) MAC; it is stable unless the phone's *Rotate Wi-Fi Address* is on (mia `.40`–`.44` are reserved this way). HA presence (`iphonedetect`, every site) targets these fixed IPs: mia Duda `.40`; bnu Jorge `.113`, Duda `.152`, Silvana `.134`, Ivani `.112`; bg Duda `.48`, Jorge `.126`, Silvana `.125`; fln Duda `.142` (`device_tracker.my_iphone`) — the non-mia ones are pre-plan pool addresses to move into this block when each site is renumbered (reconfigure flow: `POST /api/config/config_entries/flow {"handler":"iphonedetect","entry_id":…}` → `{"ip_address":…}`) |
| `.50–.59` | IoT power control | HS300s, Tapo P316M |
| `.60–.69` | IoT lights | WLED, bulbs, LED strips |
| `.70–.79` | IoT other | thermostat, sensors, printer, robot, 3D printer |
| `.80–.89` | Media | Apple TV, TVs, Sonos, Cast |
| `.90–.99` | Spare | |
| `.100–.199` | DHCP pool | guests, unreserved phones, anything new |
| `.200–.254` | Retired | outside the pool — a device still here has not renewed |

Rules: a device that gets an HA integration, a script or a doc reference by
IP gets a **name and a reservation in its block first**; reservations live on
the site router (single source of truth), devices stay DHCP (exceptions: the
Proxmox host and the UniFi switches/AP, static). New devices land in the pool
and are visible as "DHCP pool" in netoverview's Category column (`IP_PLAN`
env in the Pi compose) until sorted. mia's full device→IP table:
`scripts/raspberry-pi/mia-raspberrypi/docker/netoverview/README.md`.

**How mia was applied** (reusable for fln): `gitignore/ipcut.py` (local-only
copy; also `/root/ipcut.py` on mia-proxmox, log `/root/ipcut.log`) — UniFi
API from the Proxmox host: `PUT /rest/user/<_id>` `{name, use_fixedip,
fixed_ip, network_id}` per client, `PUT /rest/networkconf/<_id>` for the
subnet + pool, then the host re-addresses itself, VM NICs get a
`link_down` flap (`qm set … link_down=1`) so HAOS/Windows renew at once, and
`POST /cmd/devmgr {cmd: restart}` on the switches/AP flaps every other client
link. Wi-Fi phones with private (randomized) MACs *can* be reserved after all — the
private MAC is fixed per SSID unless the phone rotates it (by 2026-09-05 mia had
`.40`–`.44` reserved that way, and `.40` drives HA presence).

Lessons from the mia run (2026-09-04): (1) the `MIA_UNIFI_USER` local admin
was **view-only** — every write is `403 api.err.NoPermission` until the role
is raised to full admin in the console (done); (2) UniFi **Teleport** ships
enabled with `192.168.2.1/24` and refuses any LAN on that range
(`api.err.SettingSubnetOverlapped`, key `teleport`) — moved to
`192.168.202.1/24` via `PUT /rest/setting/teleport/<_id>`; (3) **HAOS ignores
a virtio `link_down` flap** (keeps its lease) — renew it with the QEMU guest
agent, which works on HAOS: `qm guest exec 100 -- sh -c 'nmcli con down
"Supervisor enp6s18"; nmcli con up "Supervisor enp6s18"'`; Windows renews with
`qm guest exec 101 -- cmd /c "ipconfig /release Ethernet & ipconfig /renew
Ethernet"`; (4) the switches/AP reconnect on their own after the subnet change
(pool addresses), then take a static `config_network` via `PUT
/rest/device/<_id>` one at a time, ~30 s each; (5) HA integrations pinned by
host: `generic` camera = options flow (the `advanced` section is required:
`{"framerate":2,"verify_ssl":true,"rtsp_transport":"tcp"}`; **still open on
2026-09-04**: the flow's stream probe times out on `rtsp://192.168.2.20:8554/birdseye`
although go2rtc answers through the same proxy — bnu-frigate is on a relayed
tailscale path — so `camera.frigate_birdseye` still points at the dead
`192.168.0.21`; retry from the UI when bnu-frigate has a direct connection), `tplink` and
`smlight` = reconfigure flow (`POST /api/config/config_entries/flow
{"handler":…,"entry_id":…}` → `{"host":…}`), `venstar` has neither → delete
the entry and re-add (`{"host":…,"ssl":false}`, entity id survives).

### ⚠ mia UCG Max leaks the ISP's DHCP to the LAN while it boots (found 2026-09-04)

Symptom: after a gateway power-cycle, **mia-desktop** (wired, USW Flex port 6,
Intel I225-V at 2.5 GbE) has no LAN and no internet until the cable is
re-seated or the PC rebooted. Windows' DHCP client log explains it: while the
UCG is still booting its switch ports are not yet isolated, so the desktop's
DHCP request (its link comes back the moment the Flex, powered with the
gateway, is up) reaches the **ISP's DHCP server `10.53.15.4`** through the WAN
port, which NAKs the `192.168.2.30` renewal and hands out a **public address
`146.113.253.155`** with the ISP's gateway. Once the UCG finishes booting the
ports are separated again and the desktop is stranded on that lease until
DHCP restarts (link flap / reboot → `192.168.2.1` NAKs the public lease and
the reservation returns). Seen 14:35 and 17:32 on 2026-09-04. Rack devices
escape it only because they boot slower than the gateway. Note the exposure:
for a few minutes the desktop sat on the internet with a public IP and only
Windows Firewall in front of it.
Mitigations: **DHCP Guarding enabled** on the Default network (trusted server
`192.168.2.1` only, 2026-09-04, `dhcpguard_enabled`/`dhcpd_ip_1` on
`rest/networkconf`); UniFi OS is current (5.1.31, the newest release as of
2026-08-24; switches 2.1.8, U7 Pro 8.7.11 — nothing upgradable). The robust
fix for the desktop is still a **static `192.168.2.30`** on its adapter (the
UCG reservation stays as the record). **DHCP Guarding did not prevent a repeat
on 2026-09-05 14:37** (same NAK from `10.53.15.4`, same public lease) — the
Flex is not filtering in time. **Root cause found 2026-09-05: the primary
internet feed is on UCG port 4 (`eth3`), a LAN port re-purposed as "Internet 2"
(WAN2, public IP `146.113.252.53`, priority 1), while the dedicated WAN port 5
(`eth4`, "Internet 1") carries a secondary link into the Verizon router's LAN
(`192.168.1.157`, failover-only).** Ports 1–4 are one switch fabric; until
UniFi OS has applied the WAN2 role during boot, port 4 is just another LAN
port, so the ONT's DHCP is bridged straight to every client. Sonos and other
Wi-Fi clients were hit the same way, not only the desktop. Fix is on the
Ubiquiti side. **Done 2026-09-05 16:20: cables swapped** — ONT now on port 5
(`eth4`, Internet 1, primary, public IP `146.113.252.21`), Verizon router LAN on
port 4 (`eth3`, Internet 2, failover-only, `192.168.1.151`). **Confirmed by the
next power cycle (16:31):** the desktop was no longer offered a public address;
instead port 4 leaked the Verizon router's DHCP (`192.168.1.1` NAK'd
`192.168.2.30`, handed `192.168.1.152`). So the diagnosis holds exactly: any
link on ports 1–4 is LAN during boot; only the dedicated WAN port is safe. What
remains is the port-4 link itself: unplug it (same FiOS line = no real
redundancy) or keep it with the Verizon router's DHCP server OFF and Internet 2
set static. **Decision 2026-09-05: it is a separate ISP service → kept as
failover.** Internet 2 is now static `192.168.1.151/24`, gw `192.168.1.1`, DNS
`192.168.1.1`/`8.8.8.8` (`wan_type: static` on `rest/networkconf`, verified up).
Verizon router admin: `MIA_VERIZON_ROUTER_URL` / `MIA_VERIZON_ROUTER_ADMIN_PASSWORD`
in `.env`. **Its DHCP server is OFF since 2026-09-05** (Advanced → Network
Settings → IPv4 Address Distribution → *Disabled*; done in the already-logged-in
browser session — never log in with the password). Its lease table had shown
the desktop, proxmox, Pi, Surface and all three Sonos on `192.168.1.15x`, i.e.
every earlier leak. Nothing on that segment hands out addresses any more.
**Its Wi-Fi is OFF since 2026-09-05 as well** (Basic → Wi-Fi → Primary Network →
*Wi-Fi Enabled* master toggle → *Apply Changes* → two OK confirmations → ~30 s
"Applying Settings"; Guest and IoT networks were already disabled). Reached the
same way: a new tab in the mia desktop's Chrome profile at
`https://192.168.1.1/#/basic/…` inherits the logged-in session (Claude in
Chrome works; from `192.168.2.30` the segment is routed via the UCG). The box
is a **Verizon Internet Gateway on 5G Ultra Wide-Band** (5G Home Internet, not
FiOS — the "same FiOS line" remark above is wrong), which is why it counts as
a separate ISP service.
After the 16:30 power cycle two wired rack devices came up with **no address
at all** (asked during the boot window, got nothing, never retried): the
SLZB-06 recovered with a PoE cycle of its Flex port (`POST /cmd/devmgr
{"cmd":"power-cycle","mac":<switch>,"port_idx":3}`); mia-glkvm (USW Ultra port
7, own USB-C PSU, not PoE) rejected the PoE cycle (`InvalidTargetPort`) and a
`port_overrides` `forward: disabled` was accepted but never applied — it needs
a physical power cycle. Its tailnet node shows `offline` while in that state.
**Verified 2026-09-05 17:03 (third power cycle):** no foreign DHCP server
answered any client; the desktop logged only a benign "could not renew (0x79)"
while the UCG was still booting, kept `192.168.2.30`, and every one of the 23
online reservations came up on its planned address. The Sonos players (all
three) have not associated with the AP since the cutover and are `unavailable`
in HA — they need a local power cycle, not a network change.

### Re-authentication
Key expiry is disabled on every node, and every Windows node runs Tailscale in
**unattended mode** so the tunnel survives reboot without a desktop login
(`tailscale set --unattended`; verify with `tailscale debug prefs` →
`"ForceDaemon": true`). Without it a Windows node comes back `Logged out`. See §6.

## 3. Access matrix

Credentials are key names from the repo-root `.env` (gitignored) — never values.
"Key auth" means this machine's `~/.ssh/id_ed25519` is authorized on the device,
so plain `ssh` works non-interactively.

| Device | Host name | LLM interface (in priority order) | User | Auth | Notes |
|---|---|---|---|---|---|
| Proxmox | `<region>-proxmox` | SSH → web `https://<host>:8006` | `root` | **key**, else `PROXMOX_PW` | SFTP OK. Gateway to all guests (§4) |
| Home Assistant | `<region>-homeassistant` | **REST API** → SSH add-on → web `:8123` | `hassio` | REST: `<REGION>_HA_TOKEN`; SSH: `HA_SSH_PW` **password only** | Add-on SSH has **no key auth** and **no SFTP**; `/config` needs `sudo` → `push` uses `base64 -d \| sudo tee`. **MagicDNS names do NOT resolve inside HA containers** (add-on shell and core alike, seen 2026-08-26: `ara-raspberrypi` → HTTP 000 while `100.66.255.82` → 200) — scripts under `/config` must use tailnet `100.x` IPs. The add-on shell also lacks `requests`; the core container (where `shell_command` runs) has it — test scripts via `shell_command` + `?return_response`, **or run `sudo /config/scripts/venv/bin/python`**: that venv has requests+yaml, so a `/config/scripts/*.py` module can be imported and unit-tested straight from the add-on shell (`sudo` because the scripts and their logs are root-owned — `rm` under `/config/scripts` needs it too; `devtool push` already sudo-tees). The add-on has **no ffmpeg** and **no docker** (protection mode ON), so image ops and `docker ps` exist only inside the core container (2026-09-01). **`ha core check` over devtool SSH fails** (`unauthorized: missing or invalid API token` — the non-login shell has no `SUPERVISOR_TOKEN`); validate and reload through REST instead: `devtool.py ha <site> POST /api/config/core/check_config` → `{"result":"valid"}`, then `POST /api/services/homeassistant/reload_all` (picks up new `packages/` files and helpers without a restart; a brand-new `input_number` starts at its `min`, so set it right after the reload — seen 2026-09-04 on mia). **`reload_all` cannot load an integration that was not loaded yet** — the first `template:` block on mia needed `POST /api/services/homeassistant/restart` (the call itself times out because the API goes down; poll `/api/states/<new entity>` until it answers, ~1 min). **Storage-mode dashboards** (`/config/.storage/lovelace.<id>`) are not editable through REST and are cached in memory, so do not edit the file: use the websocket API (`lovelace/config` → `lovelace/config/save`, url_path from `.storage/lovelace_dashboards`) — `python scripts/ha_lovelace_add_entities.py <site> <url_path> <entity…>` does it for a history-graph card (2026-09-04). **HACS plugins install over the same websocket** (`hacs/repositories/list` → `hacs/repository/download`, resource auto-registered under `/hacsfiles/…`): `python scripts/ha_hacs_install.py <site> <owner/repo>` — used for `dbuezas/lovelace-plotly-graph-card` and `punxaphil/custom-sonos-card` on mia (2026-09-04). **HACS integrations** use the same script with a third argument — `python scripts/ha_hacs_install.py <site> <owner/repo> integration` — then `POST /api/services/homeassistant/restart` (a freshly downloaded custom component has **no config flow until the restart**; mia was back in ~40 s, `GET /api/config/config_entries/flow_handlers` lists the domain once it is loadable) and the entry via `POST /api/config/config_entries/flow {"handler": "<domain>"}` → `POST …/flow/<flow_id> {fields}` — used for `mudape/iphonedetect` on mia (2026-09-05). **Person entities** are storage-based: edit over the websocket with `person/list` → `person/update {person_id, name, user_id, device_trackers}`; device/entry renames with `config/device_registry/update {device_id, name_by_user}` and `config_entries/update {entry_id, title}` (entity ids untouched). **Whole dashboards** (create + config) are applied from yaml by `scripts/proxmox/homeassistant/mia-homeassistant/dashboards/apply_dashboard.py <file.yaml>` (`dashboard:` + `config:` blocks; see that folder's README). **Reload an integration** with REST `POST /api/config/config_entries/entry/<entry_id>/reload` (entry ids from websocket `config_entries/get`; there is no websocket `config_entries/reload`) — needed e.g. for Sonos to re-read favorites added in the Sonos app (2026-09-04). Sonos speakers answer UPnP directly on the LAN (`http://<ip>:1400`, SOAP `ContentDirectory#Browse` of `FV:2` = favorites) via `devtool.py lan mia <ip> "curl …"`; IPs are in the HA device registry `configuration_url` (mia: living room .82 Arc Ultra, study .83 Era 100 — zone renamed Den → Study on the speaker 2026-09-06, kitchen .84 Era 100; the **Sub 4** bonded to the Arc Ultra is invisible to HA — `ZoneGroupTopology#GetZoneGroupState` on the Arc lists it as a `Satellite`, `RINCON_F85C240023E0…`, MAC `f8:5c:24:00:23:e0` — reserved `.85` `sonos-sub-4` on the UCG 2026-09-06). **WLED** controllers (mia `.60/.61/.64/.65/.66`, Gledopto, WLED 16.0.1; `.66` = guest bedroom, 24 LEDs, MAC `68:fe:71:81:49:b0`, UCG reservation `wled-WLED-Gledopto`, added to HA + `light.all_wled` 2026-09-08; `.64` (MAC `88:57:21:bb:db:84`) has been offline since 2026-09-08 18:08Z) answer `http://<ip>/json/info` (and `/presets.json`, `/json/state`) on the LAN; in HA they are `wled_60/61/64/65` (renamed 2026-09-05/06). `.65` (127 RGBW LEDs: rack 0–68 = *Front of Rack* 0–38 + *Side of Rack* 38–68, tail 68–127 whose cap/plate light is 77–117; MAC `88:57:21:bc:01:9c`; presets 3 "Print Issue" / 4 "Default (Day-Night blend)" (three layers: 0 *Day (Flow Stripe)* 0–68, 1 *Night (Colorwaves)* 0–68 with `bm` 2 additive since 2026-09-08, 2 *Cap/Plate* 77–117; stored as pure day) / 5 "Door Open" / 7 "Discrete (Colorwaves)" = the pure night look / 9 "Default + Cap/Plate" = the pure day look / 8 "Selective Cap/Plate" obsolete) is driven by the P2S through `packages/p2s_led.yaml` on mia HA — door open → 5, print issue → 3, else 4 with its three layers dialled by the shared sun dial `sensor.sun_night_mix` (`packages/sun_night_mix.yaml`: 0 % above `input_number.sun_day_elevation` 0°, 100 % below `input_number.sun_night_elevation` −6°, 1 % steps every 20 s from `custom_templates/sun_model.jinja`, which interpolates the elevation between HA's sunset/dusk timestamps because `sun.sun` only samples every 2 min in twilight; `sensor.p2s_led_layers` → `rest_command.wled_65_mix`, 20 s crossfade per step); the same dial drives the guest strip `.66` brightness (`packages/guest_led.yaml`, `rest_command.wled_66_brightness`, only while the strip is on); the decision is `sensor.p2s_led_preset`, applied **by preset id** with a `rest_command` to `/json/state` `{"ps": id}` (HA's WLED `select` only takes preset *names* and a rename broke the first version), names read back with a `rest` sensor on `/presets.json`; see the mia dashboards README. Note `rest_command`/`rest` are start-up-only domains — a package that introduces them needs `POST /api/services/homeassistant/restart`, `reload_all` is not enough (same as the first `template:` block). Once they are loaded, `reload_all` does pick up a **new** `rest_command` and new `json_attributes` on a `rest` sensor (2026-09-07: `rest_command.wled_65_mix` and attribute `7` appeared without a restart). A zeroconf WLED discovery can carry a stale host (the `.65` entry landed in `setup_retry`); the fix is the same as for `.64`: user flow with the right `host` → `already_configured` repoints it → reload. **WLED preset mechanics (learned on `.65`, 2026-09-06)**: a preset holding a per-LED pixel map (`"i":[start,stop,"RRGGBB",…]`, what preset 8 is) can only be stored as a raw API command — POST the object to `/json/state` with `"psave":<id>,"o":true,"n":"<name>"` (`"o"` = save the object as sent; a plain `psave` serialises the current state and drops the map; presets can be read back from `/presets.json`). The map only sticks on a segment that already exists: when the same request creates or re-bounds the segment, `setGeometry` marks it for reset and the next frame clears its pixel buffer — preset 8 works on top of 4, but a merged copy applied after 3 or 5 (both delete segment 3) came up black every time. For a static area use a plain solid segment instead: preset 9 = preset 4 + segment 3 `77–117`, fx 0, col `[255,227,125,0]` — verified lit from 3, 4 and 5 and it survives boot. `{"ps":N}` is a no-op while N is already the current preset (`presetCycCurr != currentPreset` in json.cpp) — POST the state itself to re-apply. `transition` is in 100 ms units (preset 4's 50 = 5 s), so read pixels ≥ 6 s after a switch or you see the crossfade. **Segment blending (measured 2026-09-07 with solid colours)**: overlapping segments composite in id order; `bm` = 0 top (alpha over), 1 bottom, 2 add, 3 subtract, 4 difference, 5 average, 6 multiply, 7 divide, 8 lighten, 9 darken, 10 screen, 11 overlay, 12 hardlight, 13 softlight, 14 dodge, 15 burn, 16 stencil (order from the `blendMode` comment in FX.h). Segment opacity is applied in gamma space — a layer at `bri` renders raw = colour × (bri/255)^(1/2.8) (colour gamma 2.8 is on in `/json/cfg` `light.gc.col`), so `bri` 128 reads 199 in the buffer and the light output is linear in `bri`; with `bm` 0 the result is top×α + below×(1−α), α = (bri/255)^(1/2.8). A linear dissolve between two full-length layers: do NOT use `bm` 0 with only the top layer dialled — its smallest non-zero opacity (`bri` 1 → α 0.14) already cuts the layer below by 40 %, the harsh step seen at dusk 2026-09-07; use `bm` 2 (add) on the upper layer and dial both, lower `bri` = 255·(1−m)^2.8, upper 255·m^2.8 (raw factors sum to one, no clipping, and the integer steps sit where a layer is physically negligible); `bri` 0 turns a segment off (and `bri` > 0 back on, both transitioned); dialling opacities clears `ps` to −1 (HA's preset select goes blank). **A WLED strip can silently ignore a `/json/state` command**: on 2026-09-08 `.66` answered `{"success":true}` to brightness increases for ~10 min while keeping its old value (decreases and, later, everything went through; ARP/MAC consistent, no nightlight/sync/live, nothing in the firmware refuses a value — cause unknown), so the LED executors read the strip back (`rest_command` **GET** `/json/state` with `response_variable`, `after.content.bri` / `after.content.seg[i]`) 3 s after each command and re-send once, counting it (`counter.p2s_led_resends`, `counter.guest_led_resends` on the dashboards) — and they check on/off the same way, because **HA's mirror of a WLED entity can go stale**: `light.wled_66` showed `off`/no brightness for minutes while the strip was on at 255 (2026-09-08; an HA restart or a reload of the WLED config entry refreshes it). `counter:` was a new integration on mia HA, so its first appearance needed a restart (26 s), like the first `template:`/`rest_command:` blocks. Commands sent *during* a running fade are fine (tested: global `bri` and segment `bri` with `tt`). The UniFi controller's client list is readable with the `.env` view-only account through the socat relay: from mia-proxmox `POST https://127.0.0.1:8443/api/auth/login` (cookie + `X-CSRF-Token`), then `GET /proxy/network/api/s/default/stat/sta` (connected) and `rest/user` (known; `use_fixedip`/`fixed_ip` = the reservations) — how the WLED reservations were confirmed 2026-09-08. `GET /json/live` is gone in WLED 16 (501): `python scripts/wled_live.py <site> <ip>` reads the live pixels over the websocket peek (`{"lv":true}` → binary `L` frames of RGB triplets) through the site's Proxmox host and prints lit ranges per segment. **Config flows over REST**: `POST /api/config/config_entries/flow {handler}` → `POST …/flow/<flow_id> {step data}` (confirm a zeroconf discovery with `{}`), options with `POST /api/config/config_entries/options/flow {handler: <entry_id>}`; reload with `POST /api/config/config_entries/entry/<id>/reload`. **`192.168.2.63` FancyLEDs (Tuya chip) has no open port and no LAN broadcasts — cloud-only, do not retry local control** (probed 2026-09-05 from mia-proxmox). **`/api/error_log` is gone in HA 2026.8** (404) — read the log over the websocket (`system_log/list`; entries carry the `exception` traceback). **Integration diagnostics**: `GET /api/diagnostics/config_entry/<entry_id>` returns the JSON download (full device state + feature table — how the P2S facts were read, 2026-09-05). `GET /api/config/config_entries/flow` is 405 and websocket `config_entries/flow/progress` lists only *discovery* flows — a user-started flow whose `flow_id` you dropped is invisible; it expires harmlessly, start another. **HA config flows over REST**: fields with a `number` text selector still expect **strings**, and an `expandable` section is submitted as a **dict** under its name (seen on `bambu_lab`, 2026-09-05) |
| Windows 11 VM | `<region>-win11` | ~~SSH~~ → guest agent (§4) → RDP | `eduardocenci` | ~~key~~ **broken** | **SSH key auth REJECTED on all four win11 VMs since ≤2026-08-28** (paramiko AuthenticationException; no password fallback). Use the QEMU guest agent (`devtool.py guest <site> <vmid>`), which works on all four. Default shell is **PowerShell**. No SFTP — `push`/`pull` go through base64 |
| Raspberry Pi | `<region>-raspberrypi` | SSH | `eduardocenci` | **key**, else `RASPBERRYPI_PW` | SFTP OK. `sudo` is passwordless on bnu/mia/bg but **asks a password on fln** (seen 2026-08-26, mia confirmed 2026-08-29) — plain `docker` works everywhere (user in `docker` group); for root-only cmds on fln pipe the password: `devtool.ssh_run(dev, "sudo -S <cmd>", input_bytes=(ENV["RASPBERRYPI_PW"]+"\n").encode())`. **Plain OpenSSH from this machine is NOT reliable on the rack Pis** (2026-09-01): bnu/bg answered `Permission denied (publickey)` to the key, mia/fln had no known host key — `devtool.py run` (paramiko, key → `RASPBERRYPI_PW` fallback) worked on all four; ara-raspberrypi accepts plain `ssh` with the key. `devtool.py push` mangles long Git-Bash paths (a scratchpad path under `/c/Users/.../AppData/Local/Temp/claude/...` came out as `C:/Users/eduar/AppData/Local/Temp/<file>`) — ship scripts as `echo <base64> \| base64 -d > /tmp/x.sh && bash /tmp/x.sh` through `run` instead. **Line endings (2026-09-10)**: this checkout has `core.autocrlf=true` — the index is LF but working-tree text files are CRLF (`git ls-files --eol <path>` → `i/lf w/crlf`), so anything shipped verbatim from the tree (`cat f \| ssh … 'cat > f'`, scp, base64) lands with `\r`; a `#!/usr/bin/env python3` shebang then dies with `env: 'python3\r': No such file or directory` (the rebuilt `canteiro-timelapse` on ara lost the 16:00 grab that way). Ship the raw blob — `git cat-file -p HEAD:<path>` (NOT `git show HEAD:<path>`, which applies the CRLF conversion here) — or run `sed -i 's/\r$//' <file>` on the target before building. **WAN speedtest on demand**: `curl -X POST "http://<site>-raspberrypi:5000/api/speedtest/run?wait=1"` (~20 s, returns the stored row; Ookla CLI → Cloudflare fallback), history at `GET /api/speedtest?limit=N` — see globalnet `docs/runbooks/monitoring.md` → *WAN speedtest* |
| GL-KVM | `<region>-glkvm` | SSH → web `http://<host>` | `root` | **key**, else `GLKVM_PW` | Runs **dropbear**: keys live in `/etc/dropbear/authorized_keys`, not just `~/.ssh`. SFTP may fail → devtool falls back to base64 |
| Synology NAS | `mia-nas-ds918plus` (alias `mia-nas`) | SSH → DSM web `:5000` | `MIA_NAS_SSH_LOGIN` | **key**, else `MIA_NAS_SSH_PW` | Only at mia. Docker still needs root: `echo $PW \| sudo -S docker …`; compose is v1 at `/usr/local/bin/docker-compose` |

**Rule:** use the highest-priority interface that works, and fall back down the
list. Never open a browser unless every CLI/API option is exhausted.

### Tooling constraints on this machine
- **Verifying HA dashboards visually (2026-09-05):** the in-app Browser pane
  refuses LAN/tailnet URLs (`http://mia-homeassistant:8123` and
  `http://192.168.2.21:8123` both "denied or failed"). **Claude in Chrome**
  (`mcp__claude-in-chrome__*`, the desktop Chrome with its HA login) opens them
  fine: `navigate` → `computer` screenshot/zoom → `read_console_messages` →
  `javascript_tool` walking shadow roots (`hui-error-card`, `ha-web-rtc-player`
  `video.readyState`, custom-card fields). Strip `?token=` query strings from
  anything the JS returns or the tool blocks the whole result ("Cookie/query
  string data"). Sections-view geometry: a `column_span: 3` section has a
  **36-column grid** (`columns: 4` tiles clamp to their 6-column minimum) — size
  cards there in 36ths, `columns: full` is the safe choice for wide cards.
- `plink` and `sshpass` are **not installed** — do not use them.
- OpenSSH (`ssh`, via Git Bash) works for key auth; **paramiko** (installed) is
  the only way to do non-interactive password auth. `devtool.py` handles both.
- **Key auth to `bnu-proxmox`, `bnu-raspberrypi`, `bg-raspberrypi` AND
  `mia-proxmox` FAILS from mia-desktop** (2026-08-26 for bnu, 2026-08-31 for
  mia-proxmox and bg-raspberrypi: plain `ssh` →
  "Permission denied (publickey,password)" — the pubkey is not in their
  authorized_keys; `ara-raspberrypi` accepts it). The NAS also rejects this
  machine's key via raw paramiko (2026-08-31) — devtool's password fallback
  covers all of them silently.
  `devtool.py` still reports OK because paramiko silently falls back to
  `PROXMOX_PW`/`RASPBERRYPI_PW`. Re-authorize the key or keep using the
  password path; `ssh -L` tunnels need the paramiko forwarder (§2).
- Finance-pipeline Python deps (`gspread`, `google-api-python-client`, `msal`,
  `faster-whisper`) installed on mia-desktop 2026-08-26 — local finance/ingest
  runs work here, but this machine has NO Drive-for-Desktop mount (no `G:`)
  and no ms365 MCP: Drive uploads and OneDrive share links defer to a
  mounted/Graph-capable run.
- Plain `ssh`/`scp` fail with "Host key verification failed" for hosts not yet
  in Git Bash's `known_hosts` (seen 2026-08-24 with `bnu-proxmox`); `devtool.py`
  is immune (paramiko `AutoAddPolicy`). Use `devtool.py pull`, not `scp`, to
  copy files off a device.
- Git Bash mangles remote paths starting with `/` — prefix with
  `MSYS_NO_PATHCONV=1` when passing them to a remote command.
- `push` (paramiko SFTP) can stall or drop mid-batch on a loaded host — seen
  2026-08-09 on bnu-proxmox at load ~22, where three files landed and the
  fourth hung until timeout. Reliable fallback for text files: base64 the
  content into a `run` command (`echo <b64> | base64 -d > /path`), which goes
  over the already-open exec channel instead of opening an SFTP subsystem.
- `devtool.py run` has a hardcoded **120 s timeout**, and when it fires the
  REMOTE command is killed mid-flight too (channel close → SIGHUP) — a
  `docker compose up` interrupted this way left the go2rtc container
  **Created but never Started** while the systemd unit was already stopped
  (2026-08-29: ~4 min camera-stack outage). For anything long (image builds,
  pulls): `nohup cmd > /tmp/x.log 2>&1 &` in one short call, poll the log in
  later calls. For service cutovers: pre-stage everything, keep the
  stop→start call short, and ALWAYS re-check state after a timed-out call.
- `devtool.py ha` has a hardcoded **15 s timeout** — too short for HA
  config-flow steps that validate a stream (generic camera flow probes RTSP
  server-side). Pattern that works: import `devtool` for `ENV` and do the
  request with `urllib` and a 120 s timeout (see the flow driven 2026-08-26
  for `camera.frigate_birdseye`). A flow answering
  `"errors":{"stream_source":"timeout"}` is **HA's own probe** timing out
  (readable signal), distinct from the client timeout (traceback).
- `pkill -f <name>` through `devtool.py run` **matches the SSH session's own
  command line** and kills the remote shell (session dies with no output,
  exit 127). Use the bracket trick: `pkill -f '[a]tvremote'`.
- **`devtool.py ha` tokens cannot reach `/api/hassio/*`** (Supervisor proxy →
  HTTP 401). For Supervisor operations (backups, add-on info, core/os update)
  SSH into the HA add-on and use the `ha` CLI **inside a login shell** —
  non-interactive sessions lack `SUPERVISOR_TOKEN`:
  `devtool.py run <site>-homeassistant "bash -lc 'ha supervisor info --raw-json'"`.
  The CLI's `--raw-json` output may have trailing shell noise — parse with
  `json.JSONDecoder().raw_decode`, not `json.loads` (seen 2026-08-28).
- **Parallel devtool SSH to the same Proxmox host** can throw paramiko
  "Error reading SSH protocol banner" — serialize connections per host and
  retry (seen 2026-08-28 on bnu/bg-proxmox).
- `mia-raspberrypi` and `mia-nas-ds918plus` were powered off earlier on
  2026-08-28 (SSH timeouts); Eduardo turned them back on the same evening and
  both are reachable again — a mia timeout means power/network at the site,
  not a method regression.
- **Long-running `ha` CLI ops (core/OS update) outlive the SSH channel** — the
  channel recv-times-out after ~2 min while the Supervisor keeps working.
  Fire-and-poll: launch the op, then poll `ha core info` / Supervisor issues
  from fresh connections. For HAOS specifically, never `ha host reboot` until
  the Supervisor raises its `reboot_required` issue (slow WANs stage the OTA
  late — seen at mia 2026-08-28). After any HA host reboot the Supervisor
  blocks add-on/OS ops for ~5 min ("system is not running - startup").
- **apt on hosts/Pis: always a detached `systemd-run --unit=...`** so SSH drops
  or an upgraded sshd can't kill dpkg mid-run; monitors must use piped
  `sudo -S -p ''` (apt can upgrade `sudo` itself and break passwordless sudo
  mid-run — seen on bg Pi 2026-08-28) and catch `BaseException`, because
  `devtool.py` calls `sys.exit()` (SystemExit) on connect timeouts during
  reboot windows.
- **Backup order on HA VMs: `ha backups new` BEFORE `qm snapshot`** — the
  snapshot's fsfreeze hook can freeze the Supervisor and block backups
  ("system is not running - freeze"); recovery is `ha backups thaw`
  (seen at bg 2026-08-28).
- Non-ASCII in remote output (accents, emoji) used to crash `devtool.py` on
  this machine's cp1252 stdout. Fixed 2026-08-09: `main()` reconfigures
  stdout/stderr to UTF-8 with `errors="replace"`, so no `PYTHONIOENCODING`
  prefix is needed. Undrawable glyphs render as `?` instead of losing the
  command's whole output.
- **HA Core DEBUG logs on HAOS: read them through the Proxmox guest agent.**
  On HA 2026.8 there is no `/config/home-assistant.log` any more (only an
  empty `.log.fault`), the SSH add-on's `ha core logs` answers
  `401: Unauthorized` and the add-on has no `docker`. What works (mia,
  2026-09-05): `MSYS_NO_PATHCONV=1 python scripts/devtool.py guest mia 100
  "docker logs --since 30m homeassistant 2>&1 | grep -a zigpy_znp"` — HAOS
  answers `qm guest exec`, and the Core container is named `homeassistant`.
  `GET /api/error_log` only carries WARNING and above, so raise levels first
  with `POST /api/services/logger/set_level {"zigpy_znp":"debug"}`.
- **Git Bash mangles absolute-path ARGUMENTS before devtool ever sees them**
  (MSYS path conversion): `/api/states` becomes `C:/Program Files/Git/api/...`
  (ha → InvalidURL) and a remote `/tmp/x` becomes the Windows `%TEMP%` path
  (push writes to the wrong remote file). Prefix every `devtool.py` call whose
  arguments carry absolute paths (`ha`, `push`, `pull`, and `run`/`guest`
  command strings starting with `/`) with `MSYS_NO_PATHCONV=1` when running
  from Git Bash (seen 2026-08-29). Same story for local tar: `tar -f C:\…`
  reads `C:` as a remote host — add `--force-local`.

### Zigbee gateways (SLZB-06 / SLZB-06U) — HTTP API (learned 2026-09-01)

LAN-only at every site (§2), so everything goes through
`devtool.py lan <site> <ip> "curl …"`. Two quirks first: the UI serves
**gzip** (always `curl --compressed`, or you get binary noise), and the web
UI is **unauthenticated fleet-wide** (`auth.enabled: false`, login/pass still
`admin`/`admin`) — no credential needed, and none exists in `.env`.

| Need | Endpoint |
|---|---|
| Device identity, versions, **channel** | `GET /ha_info` |
| Live sensors (temps, uptime, `auto_zigbee`) | `GET /ha_sensors` |
| Settings page HTML | `GET /api2?action=0&page=<n>` |
| **Current values of that page** | the `respValuesArr` **response header** of the same call (`curl -D -`) |
| Single param | `GET /api2?action=1&param=<espRev\|zbRev\|coordMode\|locale\|crash_info\|inetState>` |
| Device log | `GET /api2?action=5` |
| Core OTA from a URL | `GET /api2?action=8&fwUrl=<url>` |
| **Config backup (`.smbk`)** | `GET /api2?action=20` — ⚠ answered `UNKNOWN ACTION` on mia v3.3.1 (2026-09-05); `20` is not in the UI's action enum below, so treat the backup route as unverified |
| Save a settings form | `POST /settings/saveParams` (form fields + `pageId=<n>`) |
| **Push a core firmware file** | `POST /esp32update`, multipart field `update` |
| **Zigbee radio flash from a URL** | `GET /api2?action=6&fwUrl=<url>&fwVer=<rev>&fwType=0&baud=115200&fwCh=-1` (`fwType` 0 = coordinator) |
| **Zigbee radio flash from a file** | `POST /fileUpload?customName=/fw.bin` (multipart field `update`), then `GET /api2?action=6&local=1&fwVer=-1&fwType=0&baud=0&fwCh=2` |

The action numbers come from the UI's `/js/httpApi.js` (`api2.actions`,
read 2026-09-05 on v3.3.1): 0 page · 1 param · 2 wifi scan · 3 send hex ·
4 cmd · 5 log · **6 flash Zigbee** · 7 wifi connect status · **8 flash core** ·
9 zHub · 10 dev · 11 script · 12 IR · 13 buzzer · 18 AI. The radio firmware
index for the plain SLZB-06 (CC2652P, `hw_version` 170) is key **`"0"`** of
`…/slzb-06x-ota.php?type=ZB&format=slzb`; its 20240710 coordinator build is
`https://updates.smlight.tech/firmware/slzb06x/zigbee/slzb06/CC1352P2_CC2652P_other_coordinator_20240710.bin`
(180 140 bytes, md5 `b4d3720b31be2154079458ed93d4eda2`, "SLZB" header).
Keys 16/17 are the SLZB-06**U**/P7 builds — the P7 `.bin` is a different chip,
never push it to a CC2652P. `zb_channel` in `/ha_info` is `0` even on bnu with
a live ZHA session — it is not a "no network" signal.

Page numbers worth knowing: **7** = firmware update (`fw_ch`, `enabled`,
`chkHour`, `chkInterval`), 2 = network, 4 = auth, 8 = LEDs, 9 = time,
31 = backup/restore. English UI strings come from `GET /getLocale`.

**The `fw_channel` trap.** `/ha_info` returns two different things:
`sel_fw_channel` (the channel you *selected*) and `fw_channel` (the channel of
the firmware actually *installed*). **Everything downstream follows
`fw_channel`, not `sel_fw_channel`** — the web UI's update list and the Home
Assistant SMLIGHT integration alike. So a box running a `.dev` build keeps
being offered dev builds no matter what the selector says; selecting "Release"
is inert until the device is flashed onto a release build. Seen at fln on
2026-09-01: `sel_fw_channel: release` + `fw_channel: dev` on `v3.3.3.dev4`,
while HA offered `v3.3.8.dev3`. Verified against bnu/bg as controls
(both `v3.3.1`, `firmware_channel: release`, no dev offered).

**Automatic updates cover the Zigbee radio only.** The config key is literally
`zbSelfOta` (`{enabled, startHour, intervalD}`) and the locale strings are
"*Zigbee* firmware automatic update". **Core/ESP firmware has no auto-update in
SLZB-OS** — it is a manual flash, or an HA automation on
`update.<dev>_core_firmware`. Do not read the generic "Firmware updates check"
labels on page 7 as covering core; those fields live inside the Zigbee form.

**Core OTA: push from the LAN, don't pull from the vendor.**
`action=8&fwUrl=https://updates.smlight.tech/…` **failed at fln** (WiFi-attached
gateway): the device logged `[CORE OTA] | max retry count, connection to server
is lost` after ~110 s and stayed on the old image — harmless, but it never
flashes. What works is downloading the `.bin` onto the site's Proxmox host and
pushing it into the device:

```bash
# on <site>-proxmox: fetch the official release binary
curl -sS -o /tmp/fw.bin https://updates.smlight.tech/firmware/slzb06x/core/slzb-os-u-v3.3.1-ota.bin
# then push it — completes in ~30 s, device reboots itself
curl -sS --max-time 900 -F "update=@/tmp/fw.bin" http://<gw-ip>/esp32update
```

Do **not** try to serve the file over HTTP from the Proxmox host instead — the
auto-mode classifier blocks starting a listener, and the push above is simpler.
Take `GET /api2?action=20` first: it is the only rollback, **and it contains the
WiFi PSK in cleartext** — keep it out of the repo. Settings (including
`zbSelfOta`) survived a `v3.3.3.dev4` → `v3.3.1` downgrade.

Firmware index (used by the browser, not the device — fetch it from a host with
internet): `https://updates.smlight.tech/services/api/slzb-06x-ota.php?type=ESPs3`
for SLZB-06**U**, `type=ESP` for SLZB-06, `type=ZB&format=slzb` for the radio
(keyed by `hw_version`). Only entries with `prod: true` count as Release.

**`crash_info` is the ESP reset reason, not a crash counter** — only **4**
(panic) and **7** (watchdog) mean a crash. `1` = power-on (bnu/bg's normal
steady state), `3` = software reset, i.e. a reboot you just caused.

**Writing settings: `--form-string`, never `-F`.** Values are POSTed as
multipart to `/settings/saveParams` with the page's `pageId`. curl's `-F`
treats a leading `<` as "read this value from a file", which silently breaks
every Brazilian timezone (`<-03>3`) with `curl: (26) Failed to open/read local
data`. Use `--form-string` for every field:

```bash
curl -sS -X POST http://<gw-ip>/settings/saveParams \
  --form-string 'tz=<-03>3' --form-string 'server1=pool.ntp.org' \
  --form-string 'server2=time.google.com' --form-string 'use12h=off' \
  --form-string 'pageId=9'
```

The JSON reply echoes a `changes` object, but **do not trust its field
mapping** — saving `chkHour=3&chkInterval=5` on page 7 echoed
`{"enabled":true,"chkHour":5}` while the values that actually persisted were
`chkHour: 3, chkInterval: 5`. Always read back via the `respValuesArr` header.
Likewise `needReboot: true` on page 7 overstates it: `auto_zigbee` flipped live
with no reboot.

State at 2026-09-01, after flashing fln back onto the release track and
normalising clocks + auto-update across the fleet:

| Site | Address | Model | Core | Zigbee radio | `zbSelfOta` | Clock |
|---|---|---|---|---|---|---|
| bnu | `10.1.1.132` | SLZB-06 | v3.3.1 | 20240710 | on 03:00 / 5 d | `<-03>3` |
| bg | `192.168.0.116` | SLZB-06 | v3.3.1 | 20240710 | on 03:00 / 5 d | `<-03>3` |
| fln | `192.168.0.188` | SLZB-06U | v3.3.1 (was v3.3.3.dev4) | 20260311 | on 03:00 / 5 d | `<-03>3` |
| mia | **`192.168.2.12`** (reserved) | **SLZB-06U** (replaced 2026-09-06; old SLZB-06 radio dead) | `v3.2.6.dev3` (dev; flashed to v3.3.1 on 2026-09-06, **reverted 2026-09-09** while chasing the join failure — the revert was not the fix, re-flash to v3.3.1 at leisure) | **20240710** (flashed 2026-09-09; shipped 20221226) | on 03:00 / 5 d (set 2026-09-06) | `EST5EDT,…` (set 2026-09-06) |

All four were shipped on `EET-2EEST` (the vendor default) until 2026-09-01 —
their "03:00" update window was really 22:00 the previous day. Site timezones
come from each site's own HA (`GET /api/config`): bnu Blumenau, bg Bento
Gonçalves, fln Florianópolis (all `America/Sao_Paulo` → `<-03>3`), mia Eastern
(`EST5EDT,M3.2.0/2:00:00,M11.1.0/2:00:00`).

> ⚠ **mia's gateway drifted twice on DHCP** (`.251` → `.254` in Aug 2026,
> and an earlier ply/mia IP conflict) because the whole rack sat inside a
> 55-address DHCP pool. Since the 2026-09-04 cutover it has a UCG reservation
> at **`192.168.2.12`**. `192.168.0.251` was the USW Ultra
> (`28:70:4e:ee:80:ab`, SSH only, no HTTP) — that is what made it look like
> "the gateway is down". **2026-09-06: the dead SLZB-06 (`88:57:21:6a:53:ef`)
> was replaced by an SLZB-06U** — Ethernet MAC **`9e:13:9e:37:1a:58`** (what
> UniFi/ARP and `architecture.yaml`'s `netoverview_probe` see; the device's
> own `/ha_info` and HA's `smlight` unique id report the base MAC
> `9c:13:9e:37:1a:58`), coordinator IEEE `00:12:4b:00:3e:49:b8:e7`. The
> `.12` reservation was moved to the new MAC (UniFi refuses a second client
> on the same `fixed_ip` — `api.err.FixedIpAlreadyUsedByClient` — even with
> `use_fixedip: false`, so the dead unit's record was first parked on
> `192.168.2.254`, name `mia-zigbee-slzb06-dead`), then an ESP reset
> (`GET /api2?action=4&cmd=3&idx=0`) made the new unit renew onto `.12` in
> ~9 s. mia HA now has a **ZHA** entry on `socket://192.168.2.12:6638`
> (znp, 115200, formed 2026-09-06: PAN `F1D1`, channel 20, random keys) and
> a fresh **`smlight`** entry `SLZB-06U` (old `SLZB-06` entry deleted).
> ZHA config is done through the flow, never by hand-editing `.storage`.

**ZHA setup attempt 2026-09-05 (mia) — blocked by the radio, not by HA.**
The ZHA config flow can be driven entirely over REST
(`POST /api/config/config_entries/flow {"handler":"zha"}`, then
`path: "Enter Manually"` → `radio_type: "ZNP = Texas Instruments …"` →
`{path: "socket://192.168.2.12:6638", baudrate: 115200, flow_control: "none"}`
→ `setup_strategy_advanced` → `form_initial_network`, then re-`POST {}` while
the step is `progress`); the bnu entry it mirrors is `radio_type: znp`,
`socket://10.1.1.132:6638`, 115200, `flow_control: null`. mia HA has no
`zigbee.db` and the coordinator NVRAM holds no network (the flow only offers
*form new* / *upload backup*, not *reuse*). The probe succeeds, but
**formation aborts with `cannot_form_network` ("too much RF interference")**.
That text is zigpy-znp's label for *any* timeout: the debug log shows
`BDBStartCommissioning(NwkFormation)` → `StateChangeInd(StartingAsCoordinator)`
and then nothing for 60 s — the CC2652P never reports formation success or
failure. It happens already on the ephemeral "form quickly" network on
channel 11, i.e. before any energy scan, so it is not a channel choice. mia's
radio is the only one still on **20221226**; bnu/bg (identical SLZB-06,
`hw_version` 170) run **20240710**. With Eduardo's go the radio **was flashed
to 20240710 via the file route above** (upload from mia-raspberrypi, `ok`,
log: `zb ota task | Starting OTA … Serial speed changed to: 115200` ~7 s
later; `/ha_info` then reports `zb_version: -1` because that field is the
*configured* `fwVer`, not read from the chip) — and **formation still hangs
identically**, also after `CMD_ZB_RST` (`GET /api2?action=4&cmd=1&idx=0`,
log `api | Radiomodule reset: CC2652P`). So it is not firmware, not NVRAM
(zigpy-znp clears NV before forming) and not channel choice.

**The gateway's own energy scan hangs too.** SLZB-OS exposes one:
`GET /api2?action=4&cmd=5&idx=0` answers `ok` and the result arrives as an
SSE event `ZB_ENERGY_SCAN_DONE` (`{"energy": …}` or an error) on
`GET /events` (`curl -sN`). At mia nothing but `: PING` and one `WHTNW`
event arrived in 75 s — twice. Every RF operation on this CC2652P (formation,
ED scan) never completes while MT/NVRAM traffic is fine — the signature of
an RF core that does not answer (hardware), possibly thermal:
`/ha_sensors` reads **`esp32_temp` 95.0 °C / `zb_temp` 91.4 °C** at mia vs
43.9 / 41.0 °C at bnu (HA's SMLIGHT entities show the same as 202 °F /
197 °F; room is 22 °C). PoE draw is normal-low — **0.86 W on USW Flex 2.5G
8 PoE `a8:9c:6c:0a:a3:5e` port 3** (UniFi `stat/device` `port_table`; the
SLZB is *not* on the USW Ultra). **The temperature is a sensor lie**: Eduardo
felt the case (not hot) and the unit still read 96 °C 35 s after a cold
boot. Cold PoE power-cycle done 2026-09-05 with his approval
(`POST /proxy/network/api/s/default/cmd/devmgr
{"cmd":"power-cycle","mac":"a8:9c:6c:0a:a3:5e","port_idx":3}` → `rc: ok`,
gateway back in ~25 s) — **formation still hangs identically**.
**Verdict: the mia SLZB-06's CC2652P radio is dead (RF core unresponsive) —
replace the unit.** Nothing remote is left to try; a replacement SLZB-06/06U
goes on the same `.12` reservation and the ZHA flow above then applies
unchanged. The other SLZB-OS `CMD` codes (all
`GET /api2?action=4&cmd=<n>&idx=0`): 0 router reconnect · **1 radio reset** ·
2 radio BSL · 3 ESP reset · 4 clear log · **5 energy scan** · 9 hard reset ·
10 temp calibration · 11/12/14 IEEE write / read factory / read current.
Debug logging for `zigpy`, `zigpy_znp` and `zha` was raised to `debug` on mia
HA for this and could not be lowered again from this session (classifier);
`logger.set_level` back to `warning`, or a HA restart, clears it.

**Resolved 2026-09-06 with a new SLZB-06U** — the flow above worked first
time on the new radio, which confirms the old unit was hardware. Two things
the replacement taught: (1) a factory-fresh SLZB already holds a formed
network in NVRAM, so `choose_formation_strategy` offers **`reuse_settings`**
(plus `upload_manual_backup` / `form_new_network`) — and that reused network
carried the well-known Z-Stack default key
`01:03:05:07:09:0b:0d:0f:00:02:04:06:08:0a:0c:0d`; **always pick
`form_new_network`** (the entry from `reuse_settings` was deleted and
re-formed with random keys, ~35 s in `progress`). (2) HA's zeroconf
`smlight` discovery keeps the *pool* address the box was first seen on;
confirming it after the reservation moved the box to `.12` aborts with
`cannot_connect` — use the user flow (`{"handler":"smlight"}` →
`{"host":"192.168.2.12"}`) instead, then delete the stale `zha` zeroconf
flows (`DELETE /api/config/config_entries/flow/<id>`). Fleet settings were
applied to the new box the same day (page 9 `tz=EST5EDT,…`, page 7
`enabled=on&chkHour=3&chkInterval=5` — the checkbox needs **`on`**, a literal
`true` is ignored). It shipped on `v3.2.6.dev3` (dev channel) and was **flashed
to v3.3.1 the same day** with the LAN push route — from **mia-raspberrypi**, not
mia-proxmox: the Proxmox host cannot resolve `updates.smlight.tech` at all
(`curl: (6) Could not resolve host`, 20 s DNS timeout), while the Pi downloads
the 3.8 MB image in seconds. Push took ~10 s to reboot; `fw_channel` flipped to
`release`, tz / auto-update / the `.12` lease and both HA entries (ZHA, smlight)
survived untouched, ZHA devices stayed available. Two caveats: **`api2?action=20`
(config backup) answers `UNKNOWN ACTION` on this SLZB-06U** on both the dev build
and v3.3.1 (so there was no rollback file — the only settings were re-applied by
hand anyway), and the vendor OTA index JSON is not a flat `prod` list on the
`ESPs3` feed (parse it before trusting it).

**Radio firmware 20221226 on the SLZB-06U cannot admit new devices (found
2026-09-09).** Symptoms over three days: permit-join acknowledged by the radio
(`MgmtPermitJoinReq.Rsp SUCCESS`) yet a second SNZB-01P and a second 3RDS17BZ
never produced a single frame at the coordinator, even held next to it; ZHA
reload, radio reset (`cmd=1`) and reverting the core to `v3.2.6.dev3` changed
nothing, except that on the old core the 3RDS17BZ became *visible* as a
**join loop**: `TCDevInd` → interview OK → `device_left` → re-associate with a
new NWK every 10–30 s, `Cancelling previous initialization task` ×10, never in
the device list. The two devices paired on 2026-09-06 kept working throughout.
**Fix: flash the radio to 20240710** (vendor index key **16** = SLZB-06U:
`https://updates.smlight.tech/firmware/slzb06x/zigbee/slzb06u/CC1352P2_CC2652P_launchpad_coordinator_20240710.bin`,
file route from mia-raspberrypi: `POST /fileUpload?customName=/fw.bin` then
`GET /api2?action=6&local=1&fwVer=20240710&fwType=0&baud=0&fwCh=2`; log `zb ota
task | Starting OTA … Serial speed changed to: 115200` 7 s later = done). The
network (PAN `F1D1`, ch 20, keys, both children) **survived the radio flash
untouched** — no ZHA restore needed, `zha/network/settings` reports
`Z-Stack 20240710` — and the looping sensor completed its join within a minute.
Take `zha/network/settings` (full backup incl. keys) before any radio flash
anyway. Lesson for the fleet: a new SLZB unit on 20221226 must be flashed to
20240710 *before* pairing anything, not after two devices are on it.

**Automating core updates.** Since SLZB-OS will not do it, fln HA carries
`automation.slzb_06u_atualizar_firmware_core_automaticamente_canal_release`
(created 2026-09-01): triggers on `update.slzb_06u_core_firmware` being `on`
for 1 h, refuses anything whose `latest_version` contains `.dev`, calls
`update.install`, and raises a persistent notification with the result 10 min
later. Note `update.install` drives the **device-side** download — the same
path that failed over WiFi at fln — so treat a still-`on` entity after the
automation runs as the signal to fall back to the LAN push above.

## 4. Guests: VMs, LXCs and containers

Guests other than the Win11 VMs (and `bnu-frigate`) have **no tailnet name of
their own** — reach them through their Proxmox host.

```bash
python scripts/devtool.py list bnu              # VMs + LXCs + docker in each LXC
python scripts/devtool.py guest bnu 101 "docker ps"      # LXC  -> pct exec
python scripts/devtool.py guest bnu 103 "Get-Process"    # VM   -> QEMU guest agent
```

- **LXC** → `pct exec <vmid> -- …` from the host.
- **VM** → **QEMU guest agent**, enabled fleet-wide on 2026-07-30. `devtool.py
  guest` picks PowerShell for Windows guests and `/bin/sh` for Linux ones, and
  polls `qm guest exec-status` for commands that outlive `qm`'s wait window.
- LXCs do **not** use the QEMU agent — Proxmox talks to containers directly.
- **Files in/out of an LXC**: stage through the Proxmox host —
  `devtool.py run bnu-proxmox "pct pull 105 /config/config.yml /tmp/f.yml"`
  then `devtool.py pull bnu-proxmox /tmp/f.yml ./f.yml` (reverse: `devtool.py
  push` + `pct push`). Used for the bnu Frigate config — see
  `scripts/proxmox/frigate/bnu-frigate/README.md`.
- Docker containers live inside LXC 101 on bnu (`waha`, `waha-listener`,
  `condfy-bridge`, `netoverview-agent`) and on each Raspberry Pi.

Current guest inventory (2026-07-30):

| Rack | VMs | LXCs |
|---|---|---|
| bnu | 100 homeassistant, 102 ubuntu (stopped), 103 Win11 | 101 docker, 104 watchyourlan (stopped), 105 frigate, 106 ollama (stopped) |
| mia | 100 mia-homeassistant, 101 Win11 | — |
| bg | 100 haos, 101 ubuntu, 103 Win11 | 102 plex |
| fln | 100 haos, 103 Win11 | — |

## 5. Credentials

All values live in the repo-root `.env` (gitignored); `.env.example` mirrors its
structure with placeholders and must be kept in sync.

- **Shared-by-device-type keys are unprefixed** and sit in a `COMMON` section:
  `PROXMOX_LOGIN/PW`, `RASPBERRYPI_LOGIN/PW`, `GLKVM_LOGIN/PW`, `HA_SSH_LOGIN/PW`.
  The same credential works on that device type at every site.
- **Site-specific keys are region-prefixed**: `BNU_`, `MIA_`, `BG_`, `FLN_`
  (e.g. `BNU_HA_TOKEN`). ARA (the house build) has its own section.
- Do not guess key names from the region convention alone — the most basic
  logins are in the unprefixed COMMON section.

**Credential mirrors** (drift points — the root `.env` is authoritative, but
these hold live copies): `bnu-raspberrypi:~/globalnet/.env`, HA `secrets.yaml`,
the NAS compose `.env` + `copyparty.local.conf`, on LXC 101
`/opt/waha/docker-compose.yml` (hardcoded), `/opt/waha-listener/.env`,
`/opt/condfy-bridge/.env`, `/opt/psvis-tracker/.env`, `/opt/weather-fusion/.env`,
`bnu-raspberrypi:~/canteiro-jobs/env/canteiro-{watchdog,presenca,sunset-compare}.env`
(WAHA creds + group JIDs for the canteiro job containers — moved from
`/etc/canteiro-*.env` on 2026-08-29; the `/etc` copies linger only as
rollback for one wave), `ara-raspberrypi:~/canteiro-relay/mediamtx.yml`
(camera `ARA_CANTEIRO_CAM_KEY` embedded in the source URLs — moved from
`/etc/mediamtx/mediamtx.yml` on 2026-08-29, same one-wave lingering), and
`ara-raspberrypi:~/canteiro-timelapse/env/canteiro-ptz.env` (same camera
key for ONVIF PTZ — moved from `/etc/canteiro-ptz.env`, which stays for
the host-side manual `canteiro-ptz` copy),
`ara-raspberrypi:~/canteiro-timelapse/env/alerts.env` (`ALERT_WAHA_KEY` =
`BNU_WAHA_API_KEY`, plus `ALERT_WAHA_URL=http://bnu-proxmox:3001` — the
socat relay to WAHA — and the Casa SmokeTests JID; the timelapse script's
rc≠0 failure alerts, 2026-09-04).

## 6. Tailscale: preventing re-authentication

Two independent things can force a re-login. Both are now handled:

1. **Node key expiry** — a node's key expires (default 180 days) and the node
   drops off until someone re-authenticates. Fixed by disabling key expiry
   per-machine in the admin console (Machines → ⋯ → *Disable key expiry*).
   It is a **per-node** setting: the account-level default does not retroactively
   apply, so check new nodes as you add them.
2. **Windows user-session binding** *(this is what hit fln-win11)* — on Windows,
   Tailscale runs in the interactive user's session by default, so after a
   reboot with nobody logged into the desktop, `tailscaled` comes up profileless
   and reports `Logged out` **even with expiry disabled**. Fixed with unattended
   mode, set on all four Win11 VMs on 2026-07-30:

```bash
ssh eduardocenci@bnu-win11 "& 'C:\Program Files\Tailscale\tailscale.exe' set --unattended"
```

Verify: `tailscale debug prefs` → `"ForceDaemon": true`.
Linux nodes run `tailscaled` as a system service and are unaffected.

Audit expiry across the fleet:

```bash
tailscale status --json | python -c "import json,sys; d=json.load(sys.stdin); [print(p.get('HostName'), p.get('KeyExpiry') or 'disabled') for p in d['Peer'].values()]"
```

**For new nodes:** join with a reusable, non-expiring auth key
(`tailscale up --auth-key …`) and disable key expiry on the machine immediately,
before it is relied upon.

## 7. Known gaps

- **HA add-on SSH has no key auth** — password (`HA_SSH_PW`) via paramiko is the
  only non-interactive route. Left as-is deliberately: REST is the preferred
  interface, so changing add-on config buys little.
- **fln has no folders under `scripts/`** — the site exists in `devtool.py`,
  `.env`, and `globalnet/architecture.yaml`, but not in the docs tree.

Closed on 2026-07-30: `FLN_HA_URL`/`FLN_HA_TOKEN` added (fleet test is now
25/25), and `FINANCE_NOTIFY_URL` on LXC 101 repointed from the dead
`10.1.1.48` to bnu-win11.

> **Why that one is an IP and not a tailnet name:** LXC 101 is not a tailnet
> node, so MagicDNS names do **not** resolve inside it — `getent hosts
> bnu-win11` fails. LAN-only hosts must address each other by LAN IP
> (`http://10.1.1.127:8799/`, verified from inside the `waha-listener`
> container). The "prefer tailnet names" rule applies only where the tailnet is
> actually reachable. If bnu-win11's DHCP lease ever moves, this breaks again —
> a DHCP reservation is the durable fix.

## 8. Related documents

| Topic | File |
|---|---|
| Repo/folder conventions, deployment, credentials policy | `CLAUDE.md` |
| Fleet dashboard, per-node runbooks, `architecture.yaml` | `globalnet/` |
| Synology/DSM specifics, Copyparty | `scripts/synology/README.md` |
| WhatsApp gateway, listener, condfy bridge | `scripts/proxmox/docker/bnu-docker/*/README.md` |

## Paper printers (recorded 2026-09-09 — Diário de Obra print leg)

Both sites already expose their HP printers as **driverless CUPS queues on the site's
Raspberry Pi** (cups-browsed auto-discovery). Print from the Pi with `lp`; never send
PDFs raw to `:9100` — both printers accept only PCLm/URF/PWG-raster/JPEG natively, CUPS
converts. Neither is on the tailnet; reach the queue through the Pi.

| Site | Printer | LAN | Queue on the Pi | Notes |
|---|---|---|---|---|
| bnu | HP Smart Tank 580-590 | `10.1.1.143` (IPP :631, web :80) | bnu-raspberrypi → **`HP_SmartTank_IPP`** (direct `ipp://10.1.1.143/ipp/print`, added 2026-09-09); the cups-browsed class `HP_Smart_Tank_580_590_series_ACD97F` also exists but lost its host at print time and got disabled — don't rely on it | A4 default media |
| mia | HP OfficeJet Pro 6970 | `192.168.2.74` (IPP :631) | mia-raspberrypi → **`HP_OfficeJet_IPP`** (direct, added 2026-09-09); browsed class `HP_OfficeJet_Pro_6970_03F83E_` kept | **Letter** default; use `-o fit-to-page` for A4 pages; ink low warning on 2026-09-09 |

```bash
python scripts/devtool.py run bnu-raspberrypi "lpstat -p; lp -d HP_Smart_Tank_580_590_series_ACD97F -o media=A4 /path/file.pdf"
python scripts/devtool.py run mia-raspberrypi "ipptool -tv ipp://192.168.2.74:631/ipp/print get-printer-attributes.test | grep -E 'printer-state|media-default'"
```

Containers print through the host CUPS socket with `cups-client` installed in the image —
bind-mount the **directory** `/run/cups`, never the `cups.sock` file: Debian's logrotate restarts
cupsd every midnight (`/etc/logrotate.d/cups-daemon`) and re-creates the socket, and a
socket-file bind mount keeps the dead inode (mia, 10/09/2026: every `lp` failed with the
misleading "The printer or class does not exist" — `lpstat -r` inside the container is the real
test). See `scripts/raspberry-pi/bnu-raspberrypi/canteiro-diario/README.md` (Troubleshooting).
