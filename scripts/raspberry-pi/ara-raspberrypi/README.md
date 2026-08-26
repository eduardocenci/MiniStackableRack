# ara-raspberrypi — canteiro Pi (House Hangar, Araquari SC)

The "computadorzinho" installed in the site shed (barracão) at the ARA build,
powered up together with the Starlink kit and the Intelbras camera on
2026-08-18 (home-ara DOC-2026-197/200). **ara is a home build, not a rack
site**, but since 2026-08-26 it IS a dashboard site: registered in
`globalnet/architecture.yaml` as `home: true` (nodes `ara_rpi`/`ara_nto` +
camera/router probes; `make fleet` audits this Pi like the rack ones —
decisão Eduardo). Services here are plain systemd units, plus Docker
running the standard **netoverview** container (deployed 2026-08-26, same
shape as the rack Pis: compose at `~/netoverview` + 5-min pull cron with
prune — see [`../README.md`](../README.md)).

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

### iM9 ONVIF event/stream facts (probed live 2026-08-26, fw 2.800.00IB00N.0.R)

- **ONVIF events work**: `WSPullPointSupport=true`, `CreatePullPointSubscription`
  + `PullMessages` deliver notifications (WS-UsernameToken auth, same pattern
  as [`ptz/canteiro-ptz.py`](ptz/canteiro-ptz.py)).
- Topic set is **motion-only**: `VideoSource/MotionAlarm` (per-lens
  `Source=VideoSource000/001`, `State=true/false`), `RuleEngine/TamperDetector/
  Tamper`, `VideoSource/GlobalSceneChange`, `DigitalInput`, media-config
  changes. **No person/vehicle topics** — the human detection behind
  auto-tracking is not exposed locally (Imou cloud/Mibo app only, like the
  PTZ presets; CGI stays 401). Object classification therefore has to happen
  on our side (e.g. bnu Frigate) from the relayed stream.
- Every lens has a **640×480 H264 substream**: `subtype=1` in the RTSP path
  (`rtsp://192.168.1.56:554/cam/realmonitor?channel=<1|2>&subtype=1`) —
  cheap detect feed. Channel 1's is relayed as `canteiro-sub` and feeds the
  bnu Frigate object detection (person/vehicle) — see [`mediamtx/`](mediamtx/).
- **OSD/watermark** — the top-left **"intelbras" corner logo is NOT removable
  at the camera on any interface** (fully probed 2026-08-26):
  - ONVIF media service reports `OSD="true"` but exposes only ONE OSD object,
    `OSDTIME` (bottom-left date/time; movable/removable via `SetOSD`/`DeleteOSD`
    — **don't**, it is the ground-truth stamp the timelapse relies on).
    `GetOSDOptions` is `Image="0"`, text-only.
  - **RPC2 authenticates where the HTTP CGI is 401-locked** — the reusable win
    here. `configManager.cgi` returns 401, but the Dahua/Intelbras **RPC2** JSON
    API works with the device password (`admin` + `ARA_CANTEIRO_CAM_KEY`):
    `POST http://192.168.1.56/RPC2_Login` `global.login` → challenge/response
    (`h1=MD5(user:realm:pass)`, `h2=MD5(user:random:h1)`, both UPPER) → then
    `configManager.getConfig`/`setConfig` on `http://192.168.1.56/RPC2`. This is
    the S.I.M. Next / DMSS backend, scriptable — **use it for any future
    camera-config change** (auth pattern mirrors [`ptz/canteiro-ptz.py`](ptz/canteiro-ptz.py)).
  - Even over RPC2 the **logo is not a config object**: `VideoWidget` (both
    channels) carries only `TimeTitle` (on) + `ChannelTitle`/`CustomTitle`/
    `Covers` (all `EncodeBlend=false`/empty); every candidate name (`Logo`,
    `Watermark`, `VideoWatermark`, `ChannelLogo`, …) returns `result:false`.
    It is firmware-burned branding (`magicBox`: Vendor=IntelBras, type iM9-M,
    fw 2.800.00IB00N.0.R). The Mibo app has no watermark toggle either
    (confirmed by Eduardo 2026-08-26).
  - **Only real removal path = pixel-level on our own frames.** The timelapse
    capture already re-encodes to JPEG, so `-vf delogo=x=8:y=38:w=350:h=82`
    drops the logo (leaves a small blur smudge over the roof corner). The live
    relay is copy-only (no re-encode on this Pi) → live TV + bnu Frigate
    recordings keep the logo unless the whole stream is re-encoded. A privacy
    `Cover` written via RPC2 `setConfig` would blank the corner everywhere, but
    as a black box over real scene pixels.

## Services

| Service | What it does |
|---|---|
| [`mediamtx/`](mediamtx/) | RTSP relay: pulls the iM9 camera streams and re-serves them on the tailnet at `rtsp://ara-raspberrypi:8554/canteiro` (+ `canteiro-alt`, `canteiro-sub`). Sole tailnet consumer: go2rtc on bnu-raspberrypi, which fans out to the wall screen, the TV and the bnu Frigate NVR (recording + person/vehicle detection of the obra since 2026-08-26) |
| netoverview (Docker) | LAN discovery/ARP monitor of the house LAN `192.168.1.0/24` · web UI `http://ara-raspberrypi:5000` · standard fleet compose (`netoverview/netoverview_docker/docker-compose.yml` → `~/netoverview/`), self-updates via the 5-min pull cron · its `/api/presence` feeds the daily 20:00 obra-presence WhatsApp report ([`../bnu-raspberrypi/canteiro-presenca/`](../bnu-raspberrypi/canteiro-presenca/)) |
| [`timelapse/`](timelapse/) | Daily construction-timelapse frames off the local relay: 5 sunset windows (T−20…T+20, PT lens + fixed-lens twin, NOAA sunset per day) + worker-presence frames every 25 min 07:00–17:50 → nightly 20:00 `rclone move` to Google Drive `CeuAzul/Timelapse/` (AI ground-truth for build progress; see home-ara CLAUDE.md) |
| [`starlink-names/`](starlink-names/) | systemd timer (5 min): Starlink router gRPC `wifi_get_clients` → auto-nickname new devices in netoverview with the names the Starlink app shows; never overwrites manual renames. Display-only (presence report names) — the bnu HA Frigate gate suppresses on ANY non-fixed device online |
