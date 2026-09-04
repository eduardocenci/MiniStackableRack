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
| `bnu_frigate` | `10.1.1.160` | Frigate LXC 105 — *also* a tailnet node (`bnu-frigate`), so either route works |
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
on bnu-raspberrypi):

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
- **mia HA cannot originate connections to the tailnet** (Tailscale add-on is
  inbound-only): `curl http://100.x…` from inside HA times out. When mia HA
  must consume a tailnet service, forward it onto the rack LAN from
  mia-proxmox — pattern: socket-activated `systemd-socket-proxyd` units, see
  `scripts/proxmox/mia-proxmox/` (`192.168.2.20:8554/:1984` → `bnu-frigate`,
  feeds `camera.frigate_birdseye`).
- **mia Kasa HS300 power strips** — TWO, both on SSID `Cenci-IoT` (= the
  rack `192.168.2.0/24`), both **cloud-free since 2026-09-01** (factory reset
  + local provisioning, no Kasa account; discovery `owner` empty, default
  creds `kasa@tp-link.net`/`kasaSetup`): rack strip `192.168.2.50` (reserved; MAC
  `E0-D3-62-D0-F8-45`, fw 1.1.2 → **KLAP v2** on :80, port 9999 closed;
  outlets MiniRack_Main / MiniRack_UPS_NAS / MiniRack_AUX_Rasp / Desktop /
  Anker_USBcHub / Plug1, HA entry `01KMXYA5Z7D3JHRXFN52RQ177P`) and
  `192.168.2.51` (reserved; MAC `…FD-B8`, fw 1.0.11 → legacy :9999, HA entry
  `01M1FS8A0MBCHZPN7M8MPES6FN`). **python-kasa 0.10.2 (= what HA 2026.8 pins)
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
  `enp6s18`), which Matter needs. First Matter device: **Tapo P316M**
  6-outlet strip (Matter over Wi-Fi, per-outlet energy). Tapo Matter
  devices pair over **Bluetooth** (BLE name like `P316M_xxxxxxxx`, seen from
  mia-raspberrypi's `bluetoothctl scan on`), they do NOT open a Wi-Fi setup
  AP, so the python-kasa/`pi_ap.sh` route does not apply — commission from
  the HA Companion app (phone BLE) or, phone-less, chip-tool on the Pi +
  `commission over IP`. The HA VM has no Bluetooth adapter.
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
| `.40–.49` | Cellphones | phones, watches, e-readers — only with MAC randomization off for the SSID |
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
link. Wi-Fi phones with private (randomized) MACs cannot be reserved.

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
`{"framerate":2,"verify_ssl":true,"rtsp_transport":"tcp"}`), `tplink` and
`smlight` = reconfigure flow (`POST /api/config/config_entries/flow
{"handler":…,"entry_id":…}` → `{"host":…}`), `venstar` has neither → delete
the entry and re-add (`{"host":…,"ssl":false}`, entity id survives).

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
| Home Assistant | `<region>-homeassistant` | **REST API** → SSH add-on → web `:8123` | `hassio` | REST: `<REGION>_HA_TOKEN`; SSH: `HA_SSH_PW` **password only** | Add-on SSH has **no key auth** and **no SFTP**; `/config` needs `sudo` → `push` uses `base64 -d \| sudo tee`. **MagicDNS names do NOT resolve inside HA containers** (add-on shell and core alike, seen 2026-08-26: `ara-raspberrypi` → HTTP 000 while `100.66.255.82` → 200) — scripts under `/config` must use tailnet `100.x` IPs. The add-on shell also lacks `requests`; the core container (where `shell_command` runs) has it — test scripts via `shell_command` + `?return_response`, **or run `sudo /config/scripts/venv/bin/python`**: that venv has requests+yaml, so a `/config/scripts/*.py` module can be imported and unit-tested straight from the add-on shell (`sudo` because the scripts and their logs are root-owned — `rm` under `/config/scripts` needs it too; `devtool push` already sudo-tees). The add-on has **no ffmpeg** and **no docker** (protection mode ON), so image ops and `docker ps` exist only inside the core container (2026-09-01). **`ha core check` over devtool SSH fails** (`unauthorized: missing or invalid API token` — the non-login shell has no `SUPERVISOR_TOKEN`); validate and reload through REST instead: `devtool.py ha <site> POST /api/config/core/check_config` → `{"result":"valid"}`, then `POST /api/services/homeassistant/reload_all` (picks up new `packages/` files and helpers without a restart; a brand-new `input_number` starts at its `min`, so set it right after the reload — seen 2026-09-04 on mia). **`reload_all` cannot load an integration that was not loaded yet** — the first `template:` block on mia needed `POST /api/services/homeassistant/restart` (the call itself times out because the API goes down; poll `/api/states/<new entity>` until it answers, ~1 min). **Storage-mode dashboards** (`/config/.storage/lovelace.<id>`) are not editable through REST and are cached in memory, so do not edit the file: use the websocket API (`lovelace/config` → `lovelace/config/save`, url_path from `.storage/lovelace_dashboards`) — `python scripts/ha_lovelace_add_entities.py <site> <url_path> <entity…>` does it for a history-graph card (2026-09-04). **HACS plugins install over the same websocket** (`hacs/repositories/list` → `hacs/repository/download`, resource auto-registered under `/hacsfiles/…`): `python scripts/ha_hacs_install.py <site> <owner/repo>` — used for `dbuezas/lovelace-plotly-graph-card` on mia (2026-09-04) |
| Windows 11 VM | `<region>-win11` | ~~SSH~~ → guest agent (§4) → RDP | `eduardocenci` | ~~key~~ **broken** | **SSH key auth REJECTED on all four win11 VMs since ≤2026-08-28** (paramiko AuthenticationException; no password fallback). Use the QEMU guest agent (`devtool.py guest <site> <vmid>`), which works on all four. Default shell is **PowerShell**. No SFTP — `push`/`pull` go through base64 |
| Raspberry Pi | `<region>-raspberrypi` | SSH | `eduardocenci` | **key**, else `RASPBERRYPI_PW` | SFTP OK. `sudo` is passwordless on bnu/mia/bg but **asks a password on fln** (seen 2026-08-26, mia confirmed 2026-08-29) — plain `docker` works everywhere (user in `docker` group); for root-only cmds on fln pipe the password: `devtool.ssh_run(dev, "sudo -S <cmd>", input_bytes=(ENV["RASPBERRYPI_PW"]+"\n").encode())`. **Plain OpenSSH from this machine is NOT reliable on the rack Pis** (2026-09-01): bnu/bg answered `Permission denied (publickey)` to the key, mia/fln had no known host key — `devtool.py run` (paramiko, key → `RASPBERRYPI_PW` fallback) worked on all four; ara-raspberrypi accepts plain `ssh` with the key. `devtool.py push` mangles long Git-Bash paths (a scratchpad path under `/c/Users/.../AppData/Local/Temp/claude/...` came out as `C:/Users/eduar/AppData/Local/Temp/<file>`) — ship scripts as `echo <base64> \| base64 -d > /tmp/x.sh && bash /tmp/x.sh` through `run` instead. **WAN speedtest on demand**: `curl -X POST "http://<site>-raspberrypi:5000/api/speedtest/run?wait=1"` (~20 s, returns the stored row; Ookla CLI → Cloudflare fallback), history at `GET /api/speedtest?limit=N` — see globalnet `docs/runbooks/monitoring.md` → *WAN speedtest* |
| GL-KVM | `<region>-glkvm` | SSH → web `http://<host>` | `root` | **key**, else `GLKVM_PW` | Runs **dropbear**: keys live in `/etc/dropbear/authorized_keys`, not just `~/.ssh`. SFTP may fail → devtool falls back to base64 |
| Synology NAS | `mia-nas-ds918plus` (alias `mia-nas`) | SSH → DSM web `:5000` | `MIA_NAS_SSH_LOGIN` | **key**, else `MIA_NAS_SSH_PW` | Only at mia. Docker still needs root: `echo $PW \| sudo -S docker …`; compose is v1 at `/usr/local/bin/docker-compose` |

**Rule:** use the highest-priority interface that works, and fall back down the
list. Never open a browser unless every CLI/API option is exhausted.

### Tooling constraints on this machine
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
| **Config backup (`.smbk`)** | `GET /api2?action=20` |
| Save a settings form | `POST /settings/saveParams` (form fields + `pageId=<n>`) |
| **Push a core firmware file** | `POST /esp32update`, multipart field `update` |

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
| mia | **`192.168.2.12`** (reserved) | SLZB-06 | v3.3.1 | 20221226 | on 03:00 / 5 d | `EST5EDT,…` |

All four were shipped on `EET-2EEST` (the vendor default) until 2026-09-01 —
their "03:00" update window was really 22:00 the previous day. Site timezones
come from each site's own HA (`GET /api/config`): bnu Blumenau, bg Bento
Gonçalves, fln Florianópolis (all `America/Sao_Paulo` → `<-03>3`), mia Eastern
(`EST5EDT,M3.2.0/2:00:00,M11.1.0/2:00:00`).

> ⚠ **mia's gateway drifted twice on DHCP** (`.251` → `.254` in Aug 2026,
> and an earlier ply/mia IP conflict) because the whole rack sat inside a
> 55-address DHCP pool. Since the 2026-09-04 cutover it has a UCG reservation
> at **`192.168.2.12`** (MAC `88:57:21:6a:53:ef`, which is also what
> `architecture.yaml`'s `netoverview_probe` keys on). `192.168.0.251` was the
> USW Ultra (`28:70:4e:ee:80:ab`, SSH only, no HTTP) — that is what made it
> look like "the gateway is down". The `smlight` entry in mia HA is still
> pinned to `.251` (`setup_retry`) → *Reconfigure* it to `192.168.2.12`.
> **There is no ZHA entry in mia HA at the moment** (2026-09-04, checked via
> `/api/config/config_entries/entry`); when Zigbee is set up again, point ZHA
> at `socket://192.168.2.12:6638` from the UI (Settings → Devices & services →
> ZHA → ⋮ → *Reconfigure*), never by hand-editing `.storage`. (Decision
> 2026-09-01: Eduardo does the ZHA step by hand.)

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
the host-side manual `canteiro-ptz` copy).

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
