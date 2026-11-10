# go2rtc — canteiro restream for LAN devices (bnu-raspberrypi)

Bridges the tailnet-only ARA camera relay onto the bnu house LAN: everything
at bnu (the TV cast, phone/PC viewers AND the bnu Frigate NVR) reads from
this go2rtc, and ara's Starlink carries each canteiro stream exactly once.
Since 2026-10-29 the **main stream** reaches go2rtc through canteiro-hls
(mediamtx, the one that pulls it from ara, with `alwaysAvailable`); the
substream is still pulled here directly:

```
rtsp://ara-raspberrypi2:8554/canteiro ──tailnet (1 copy)──▶ canteiro-hls (mediamtx, alwaysAvailable)
                                                            ├─ :8888 HLS → browser /live page
                                                            └─ rtsp://127.0.0.1:8555/canteiro ──▶ go2rtc `canteiro`
rtsp://ara-raspberrypi2:8554/canteiro-sub ──tailnet (1 copy)──────────────────▶ go2rtc `canteiro_sub`
rtsp://ara-raspberrypi2:8554/canteiro-alt-sub ──tailnet (1 copy, 2026-11-04)──▶ go2rtc `canteiro_fixa_sub` (fixed lens, 640×480)
rtsp://ara-raspberrypi2:8554/canteiro-alt ──tailnet (1 copy, 2026-11-05)──────▶ go2rtc `canteiro_fixa` (fixed lens, HEVC main)
go2rtc on bnu-raspberrypi
  ├─ rtsp://10.1.1.123:8554/canteiro + canteiro_sub → bnu Frigate LXC 105 (record + person/vehicle detect, 2026-08-26)
  ├─ rtsp://10.1.1.123:8554/canteiro_fixa_sub + canteiro_fixa → bnu Frigate camera `canteiro_fixa` (detect 5 fps on the sub since 2026-11-04; record on the HEVC main since 2026-11-05)
  ├─ /api/stream.mp4?src=canteiro → 55" TV (Cast)
  └─ same URL ─tailnet─▶ bg-proxmox :1984 forward → bg 50" TV (Cast, on demand, 2026-10-29)
```

**Why the main stream goes through mediamtx (2026-10-29):** the camera
closes its RTSP session at the start of most PTZ excursions of the
timelapse; the relay reopens it 5 s later with a new RTP timestamp base.
go2rtc reconnects its producer fine (~6 s), but it swaps it under the
consumers' live sessions, so they get a timestamp jump — the HLS muxer
crash-looped until the watchdog's auto-heal (~100 s, 1–3× per solar
sequence). No go2rtc option changes that. mediamtx with `alwaysAvailable`
keeps its readers connected with a "sem sinal" card and one continuous
timeline instead — so go2rtc's `canteiro` producer no longer sees EOFs at
all, and during a camera or Starlink outage its consumers (TV, MSE, Frigate
record, watchdog frame) get the card. Full story:
[`../canteiro-hls/README.md`](../canteiro-hls/README.md).

(The mpv wall view of this stream on the Pi's own screen —
`canteiro-screen.service` — was removed on 2026-08-29, decisão Eduardo.)

The RTSP listener is LAN-exposed (`:8554`, was localhost-only until
2026-08-26) so the Frigate LXC can attach as another reader of the shared
producer — no reader auth, same LAN+tailnet trust as the `:1984` API.

**Why single-pull (2026-08-26 incident):** with the then-existing wall
screen reading ara directly *and* go2rtc pulling for the TV, daytime
bitrate × 2 streams saturated the canteiro's Starlink upload — the relay
logged "reader is too slow, discarding frames" and the TV froze. One
upstream copy fixed it; the rule still applies to today's consumers.

**Do not cast the `canteiro_h264` transcode:** ffmpeg software-decoding the
3MP HEVC pushes the Pi 4 to load ~6 and starves everything on it (seen
2026-08-26). The TV plays the HEVC passthrough natively; the transcode path
exists only as a compatibility fallback for other devices, used sparingly.
If the TV ever needs H.264 permanently, restream the camera's native
substream (channel=1&subtype=1, H.264 640×480 — zero transcode) instead.

| URL (LAN `10.1.1.123` / tailnet `bnu-raspberrypi` = `100.91.64.62`) | Content |
|---|---|
| `…:1984/api/stream.mp4?src=canteiro` | HEVC passthrough (2304×1296) — the TV cast |
| `https://bnu-raspberrypi.woodpecker-shark.ts.net/live` | **browser live view — THE bookmark** ([`canteiro-live.html`](canteiro-live.html), hls.js over the **mediamtx `/hls` endpoint** ([`../docker/canteiro-hls/`](../docker/canteiro-hls/)) — 4 s segments, ~35 s window riding out Starlink/DERP jitter; native HLS on iOS; served by `tailscale serve --set-path=/live`). Until 2026-08-29 it used go2rtc's own HLS, whose ~1 s window caused constant interruptions. |
| `https://…ts.net/hls/canteiro/index.m3u8?cookieCheck=1` | the HLS endpoint itself (mediamtx via `tailscale serve --set-path=/hls`; the query param dodges a redirect that escapes the mount — see [`../docker/canteiro-hls/README.md`](../docker/canteiro-hls/README.md)) |
| `…:1984/stream.html?src=canteiro&mode=hls` | go2rtc's own HLS page — diagnostics only: ~1 s live window (2 fake-0.5 s segments), collapses on any hiccup; also **Safari/iOS only** (desktop Chrome has no native HLS) |
| `…:1984/stream.html?src=canteiro&mode=mse` | low-latency view (~1 s behind live, but stutters on every network hiccup — HEVC via MSE) |
| `…:1984/stream.html?src=canteiro_h264` | browser fallback if a device can't decode HEVC (starts the on-demand transcode — occasional use only) |
| `…:8554/canteiro` · `…:8554/canteiro_sub` | RTSP (TCP) — Frigate's record feed (HEVC main) and detect feed (H.264 640×480 substream) |
| `…:1984/` | go2rtc web UI (diagnostics) |

Every viewer above consumes the **shared local producer** — N phones/PCs
load bnu's network only; ara's Starlink always carries exactly one copy.
Away from home the links work over the tailnet (Tailscale app on the
device). **Bookmark the full MagicDNS FQDN**
(`bnu-raspberrypi.woodpecker-shark.ts.net`) — it also resolves for users
who got the node as a *shared* device from another tailnet, where the short
name does not. Live-view bookmarks are recorded in the repo-root `.env`
(`ARA_LIVE_VIEW_URL*`).

**Browser links MUST be HTTPS via `tailscale serve`** (configured on the Pi
2026-08-27, persists across reboots: `tailscale serve --bg
http://127.0.0.1:1984` → `https://bnu-raspberrypi.woodpecker-shark.ts.net/`
with an auto-renewed Let's Encrypt cert; `sudo tailscale serve status` to
inspect, `sudo tailscale serve --https=443 off` to remove). Reason: `ts.net`
is on the browsers' **HSTS preload list**, so phones/PCs force `https://` on
that domain and plain `http://…:1984` dies with ERR_SSL_PROTOCOL_ERROR.
WebSocket (the player's transport) proxies through serve fine (verified 101
upgrade). The `:1984` HTTP endpoints remain for LAN/IP consumers (the TV
cast uses `http://10.1.1.123:1984/...` — IPs are not HSTS'd).

Consumed by the **bnu HA script `script.canteiro_na_tv`** ("Canteiro na
TV"), which casts the **HEVC passthrough** URL to the 55" Neo QLED
(`media_player.qn85f1443`, Samsung's 2026 native Google Cast receiver).

The **bg twin** (2026-10-29) is bg HA `script.canteiro_na_tv` → the
living-room 50" QLED (`media_player.50_qled`), fetching this same
`stream.mp4` through the `canteiro-tv-proxy` forward on bg-proxmox
(`http://192.168.0.120:1984/…`, see `scripts/proxmox/bg-proxmox/`) — one
more reader of the shared producer, so ara's Starlink still carries one
copy; bnu's WAN upload carries ~2 Mbps extra while it plays. Its own
start/stop links are `BG_TV_WEBHOOK_ON` / `BG_TV_WEBHOOK_OFF`.

**One-click control (browser bookmarks).** Two HA webhook automations on
bnu-homeassistant expose start/stop as plain-GET URLs (no login — the long
random id is the credential; tailnet/LAN reachable only):
`automation.webhook_canteiro_na_tv_ligar` → `script.canteiro_na_tv`, and
`automation.webhook_canteiro_na_tv_parar` → `media_player.media_stop` on the
TV. The live bookmark URLs (with the secret ids) are in the repo-root `.env`
as `ARA_TV_WEBHOOK_ON` / `ARA_TV_WEBHOOK_OFF`. Clicking either returns a
blank HTTP 200 — that is normal for an HA webhook.

Findings from the 2026-08-24 test session (what works and what doesn't):
- **55" Neo QLED via Google Cast + progressive MP4: WORKS** — including
  native HEVC decode (2304×1296 passthrough, no transcode). This is the
  production path.
- 55" via Cast + go2rtc **HLS**: the Default Media Receiver launches but
  never fetches segments (CORS headers were present; the receiver just
  rejects this HLS flavor). Use `stream.mp4`, not `stream.m3u8`.
- 60" AU8000 via **DLNA** (`media_player.samsung_au8000_60_tv`): accepts
  the push, flashes "playing", then drops to idle without fetching video —
  for both MP4 and HLS. Samsung's DMR does not play endless live streams;
  the 60" is a dead end for this feed (its script was removed).
- Cold power-on of the 55" over the network requires the TV setting
  **"Power On with Mobile"** — in deep standby the Cast entity is
  `unavailable` and `media_player.turn_on` has nothing to talk to.

The API is unauthenticated by design — LAN + tailnet exposure only, same
trust level as the relay. `10.1.1.123` is a DHCP lease; pin a reservation on
the EdgeRouter if it drifts (the HA script hardcodes the IP because DLNA/Cast
devices cannot resolve local names).

## Install layout (live)

Docker container since 2026-08-29 (was a systemd unit + hand-downloaded
binary; the disabled unit, `/usr/local/bin/go2rtc` and `/etc/go2rtc/`
stay on the Pi for one wave as rollback).

| File | Purpose |
|---|---|
| `~/go2rtc/compose.yml` | copy of [`compose.yml`](compose.yml) — image `alexxit/go2rtc:1.9.14` (pinned), host network, `/dev/video10-12` + `video` group for v4l2m2m encode |
| `~/go2rtc/go2rtc.yaml` | copy of [`go2rtc.yaml`](go2rtc.yaml), ro-mounted at `/config/go2rtc.yaml` |
| `/usr/local/share/canteiro-live/index.html` | copy of [`canteiro-live.html`](canteiro-live.html) — the `/live` page, served as a static file by `tailscale serve` (independent of the container) |

## Operate

```bash
python scripts/devtool.py run bnu-raspberrypi "docker ps --filter name=go2rtc"
python scripts/devtool.py run bnu-raspberrypi "curl -s http://127.0.0.1:1984/api/streams | head -5"
python scripts/devtool.py run bnu-raspberrypi "docker logs go2rtc --tail 20"
```

Streams are pulled from the relay **on demand** (go2rtc connects upstream
only while a client is reading). Since 2026-08-26 the bnu Frigate holds
`canteiro` + `canteiro_sub` open 24/7 (record + detect), since 2026-11-04
also `canteiro_fixa_sub` (the fixed lens' 640×480 substream, its detect
feed) and since 2026-11-05 `canteiro_fixa` (the fixed lens' HEVC main, its
record feed), so the Starlink upload carries **two main streams + two
substreams** continuously — still one copy of each; TV/browser viewers
keep adding zero upstream load.

**Two main streams (2026-11-05, decisão Eduardo — "tentar de novo").** The
2026-08-26 stall was two copies of the PT main at daytime bitrate while the
tailnet path was unmeasured. Measured on 2026-11-05 at night before
switching: each main ≈ 1.3–1.4 Mbps (ONVIF `BitrateLimit` 1280 kbps on both
channels), each sub ≈ 0.1–0.2 Mbps; the dish's raw upload ≈ 8 Mbps (12 s
to speed.cloudflare.com with the streams running); the path the streams
actually take — **Tailscale via DERP São Paulo, never a direct UDP
connection** (`tailscale ping` from either side: "direct connection not
established", although both `netcheck`s report UDP ok and
`MappingVariesByDestIP: false`) — carried only **3.4–3.7 Mbps more** on
top of the 1.9 Mbps the streams already used (10 s TCP sink test to
bnu:5299). In the first hour after the switch (03:03–04:03 UTC) the tunnel
(`tailscale0` tx on ara) averaged 3.2 Mbps (p95 3.8, peak 6.2) — inside the
measured DERP capacity with ~1.5–2 Mbps of headroom at night — with **zero
relay discards in 60 min**; the first 13 min were unstable (4 stalls ≥ 5 s of
the PT main, 1 drop of the fixed main, i.e. fresh TCP sessions after the
restarts), the next 47 min clean, at the baseline stall rate (~1.4/h over
the previous 27 h). The workday is the real test (daytime congestion, the
crew's phones, timelapse uploads). The real limiter is the DERP TCP path over a lossy
link (0.8–2.2 % TCP retransmits on the relay's reader sockets, ~2 stalls of
the PT main per hour already before the change), not the dish — so the
relay now buffers 2048 packets per reader (`writeQueueSize`, ~13 s of a
main) instead of discarding after ~3 s, and the first lever if two mains
prove too much is a **direct tailnet path** (fixed UDP port for tailscaled
on bnu-raspberrypi + port-forward on the bnu router), then lowering the
fixed lens' bitrate via ONVIF, then a Starlink plan with more upload (the
100 Mbps figure of the current plan is download — irrelevant here, we use
0.3 Mbps down). **Rollback** = point the `record` role of Frigate's
`canteiro_fixa` back at `canteiro_fixa_sub` (go2rtc then drops the pull on
its own). Watch: `ssh ara-raspberrypi2 'docker logs canteiro-relay --tail
300 | grep -c "too slow"'`, Frigate `/api/stats` `camera_fps` of both
cameras, go2rtc `i/o timeout` lines, and the ad-hoc sampler
`~/uplink-monitor/uplink.csv` on ara (10 s rows: dish up/down bps, ping,
drop, tailscale0/wlan0 Mbps, relay discards, per-reader TCP retransmits;
`python3 ~/uplink-monitor/uplink_report.py <csv> <split ISO UTC>` prints
before/after statistics).
