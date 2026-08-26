# ara-raspberrypi — canteiro Pi (House Hangar, Araquari SC)

The "computadorzinho" installed in the site shed (barracão) at the ARA build,
powered up together with the Starlink kit and the Intelbras camera on
2026-08-18 (home-ara DOC-2026-197/200). **ara is a home build, not a rack
site** — this Pi is not in `globalnet/architecture.yaml` and `make fleet`
does not audit it. Services here are plain systemd units, plus Docker
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
  cheap detect/preview feed; not in the mediamtx relay yet.
| Starlink router | `192.168.1.1` | House LAN gateway |

## Services

| Service | What it does |
|---|---|
| [`mediamtx/`](mediamtx/) | RTSP relay: pulls the iM9 camera streams and re-serves them on the tailnet at `rtsp://ara-raspberrypi:8554/canteiro` (consumed by the bnu-raspberrypi screen) |
| netoverview (Docker) | LAN discovery/ARP monitor of the house LAN `192.168.1.0/24` · web UI `http://ara-raspberrypi:5000` · standard fleet compose (`netoverview/netoverview_docker/docker-compose.yml` → `~/netoverview/`), self-updates via the 5-min pull cron |
