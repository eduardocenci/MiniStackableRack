# go2rtc — canteiro restream for LAN devices (bnu-raspberrypi)

Bridges the tailnet-only ARA camera relay onto the bnu house LAN: everything
at bnu (the TV cast, phone/PC viewers AND the bnu Frigate NVR) reads from
this go2rtc, and ara's Starlink carries each canteiro stream exactly once.
Since 2026-10-29 the PT lens' **main stream** reaches go2rtc through
canteiro-hls (mediamtx, the one that pulls it from ara, with
`alwaysAvailable`); since 2026-11-14 so does the fixed lens' main, and
go2rtc also serves the **mosaic** `[fixed lens | PT lens]` — THE view for
people (TV, `/live`, watchdog frame). The substreams are still pulled here
directly:

```
rtsp://ara-raspberrypi2:8554/canteiro ─────tailnet (1 copy)──▶ canteiro-hls path canteiro      ──▶ go2rtc `canteiro`      (127.0.0.1:8555/canteiro)
rtsp://ara-raspberrypi2:8554/canteiro-alt ─tailnet (1 copy)──▶ canteiro-hls path canteiro-fixa ──▶ go2rtc `canteiro_fixa` (127.0.0.1:8555/canteiro-fixa)
rtsp://ara-raspberrypi2:8554/canteiro-sub ──tailnet (1 copy)──────────────────▶ go2rtc `canteiro_sub`
rtsp://ara-raspberrypi2:8554/canteiro-alt-sub ──tailnet (1 copy, 2026-11-04)──▶ go2rtc `canteiro_fixa_sub` (fixed lens, 640×480)
go2rtc on bnu-raspberrypi
  ├─ rtsp://10.1.1.123:8554/canteiro + canteiro_sub → bnu Frigate LXC 105 (record + person/vehicle detect, 2026-08-26)
  ├─ rtsp://10.1.1.123:8554/canteiro_fixa_sub + canteiro_fixa → bnu Frigate camera `canteiro_fixa` (detect 5 fps on the sub since 2026-11-04; record on the HEVC main since 2026-11-05)
  │     └─ canteiro-mosaico.service in that LXC (VAAPI): both mains from Frigate's own go2rtc → H.264 2560×720 15 fps
  │           └─ publishes rtsp://10.1.1.123:8555/mosaico (canteiro-hls path `mosaico`) ─▶ /hls/mosaico → the /live page
  ├─ `canteiro_mosaico` ◀─ rtsp://127.0.0.1:8555/mosaico
  ├─ /api/stream.mp4?src=canteiro_mosaico → 55" TV (Cast)
  └─ /api/stream.mp4?src=canteiro_mosaico_tv (1920×540, ◀─ :8555/mosaico-tv) ─tailnet─▶ bg-proxmox :1984 forward → bg 50" TV (Cast, on demand, 2026-10-29)
```

**The mosaic (2026-11-14, decisão Eduardo).** The two lenses were re-aimed
so that side by side they give an "almost 180°" view: fixed lens looking
left to the street, PT lens parked at its rest position looking right
(nothing automated moves it any more). The composition runs in the bnu
Frigate LXC, where the GPU is and where both mains already arrive — no
extra copy over the Starlink; what, why and how to operate it:
[`../../../../proxmox/frigate/bnu-frigate/README.md`](../../../../proxmox/frigate/bnu-frigate/README.md).
Each lens alone is still here as `canteiro` / `canteiro_fixa` (HEVC).

**Why the main streams go through mediamtx (2026-10-29):** go2rtc reconnects
a dropped producer fine (~6 s), but it swaps it under the consumers' live
sessions, so they get a timestamp jump — the HLS muxer crash-looped until
the watchdog's auto-heal. No go2rtc option changes that. mediamtx with
`alwaysAvailable` keeps its readers connected with a "sem sinal" card and
one continuous timeline instead — so go2rtc's `canteiro`/`canteiro_fixa`
producers no longer see EOFs at all, and during a camera or Starlink outage
their consumers (Frigate record, the mosaic composer, MSE) get the card.
History: the daily trigger was the camera closing its RTSP session at the
start of most PTZ excursions of the timelapse (1–3× per solar sequence);
those excursions ended on 2026-11-14, the design stays for camera/Starlink
dropouts — and because the mosaic's `hstack` stalls when one input stops.
Full story: [`../canteiro-hls/README.md`](../canteiro-hls/README.md).

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
2026-08-26). It exists only as a compatibility fallback for a single lens,
used sparingly. A device that cannot decode HEVC should get
`canteiro_mosaico` — already H.264, encoded on the Frigate LXC's GPU, zero
transcode on this Pi.

| URL (LAN `10.1.1.123` / tailnet `bnu-raspberrypi` = `100.91.64.62`) | Content |
|---|---|
| `…:1984/api/stream.mp4?src=canteiro_mosaico` | **the TV cast** since 2026-11-14 — the mosaic `[fixed lens \| PT lens]`, H.264 2560×720 15 fps (any device decodes it). Still frame: `…:1984/api/frame.jpeg?src=canteiro_mosaico` (the watchdog's "last frame"; the HEVC `frame.jpeg?src=canteiro` was answering HTTP 500) |
| `…:1984/api/stream.mp4?src=canteiro` · `…?src=canteiro_fixa` | one lens alone, HEVC passthrough (2304×1296) — `canteiro` was the TV cast until 2026-11-13 |
| `https://bnu-raspberrypi.woodpecker-shark.ts.net/live` | **browser live view — THE bookmark**; plays the mosaic (`/hls/mosaico`) since 2026-11-14, `…/live?src=canteiro` or `…/live?src=canteiro-fixa` plays one lens alone; verified in Chrome 2026-11-14 (2560×720 playing) ([`canteiro-live.html`](canteiro-live.html), hls.js over the **mediamtx `/hls` endpoint** ([`../docker/canteiro-hls/`](../docker/canteiro-hls/)) — 4 s segments, ~35 s window riding out Starlink/DERP jitter; native HLS on iOS; served by `tailscale serve --set-path=/live`). Until 2026-08-29 it used go2rtc's own HLS, whose ~1 s window caused constant interruptions. |
| `https://…ts.net/painel/painel.png` | the live data band (`canteiro-painel` `:8557` via `tailscale serve --set-path=/painel`) — since 2026-11-15 `/live` shows it under the mosaic video, together 16:9 (`…/live?painel=0` = video only); the TV gets the same band burned into `mosaico-tv` (1920×1080) by `canteiro-mosaico`. See [`../../canteiro-painel/`](../../canteiro-painel/) |
| `https://…ts.net/hls/mosaico/index.m3u8?cookieCheck=1` | the HLS endpoint itself — also `/hls/canteiro/…` (PT lens) and `/hls/canteiro-fixa/…` (fixed lens), HEVC (mediamtx via `tailscale serve --set-path=/hls`; the query param dodges a redirect that escapes the mount — see [`../docker/canteiro-hls/README.md`](../docker/canteiro-hls/README.md)) |
| `…:1984/stream.html?src=canteiro&mode=hls` | go2rtc's own HLS page — diagnostics only: ~1 s live window (2 fake-0.5 s segments), collapses on any hiccup; also **Safari/iOS only** (desktop Chrome has no native HLS) |
| `…:1984/stream.html?src=canteiro&mode=mse` | low-latency view (~1 s behind live, but stutters on every network hiccup — HEVC via MSE) |
| `…:1984/stream.html?src=canteiro_h264` | browser fallback if a device can't decode HEVC (starts the on-demand transcode — occasional use only) |
| `…:8554/canteiro` · `…:8554/canteiro_sub` · `…:8554/canteiro_fixa` · `…:8554/canteiro_fixa_sub` | RTSP (TCP) — Frigate's record feeds (HEVC mains) and detect feeds (H.264 640×480 substreams) of the two lenses |
| `…:1984/` | go2rtc web UI (diagnostics) |

Every viewer above consumes the **shared local producer** — N phones/PCs
load bnu's network only; ara's Starlink always carries exactly one copy of
each stream (the mosaic is composed at bnu from the copies already here).
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
TV"), which casts the mosaic at 1920x540 as **HLS from canteiro-hls** —
`http://10.1.1.123:8888/mosaico-tv/index.m3u8?cookieCheck=1`, content type
`application/x-mpegURL`, `extra: {stream_type: LIVE, media_info: {hlsVideoSegmentFormat: fmp4}}`
— since 2026-11-14 (the HEVC passthrough `stream.mp4?src=canteiro` until then) — to the 55" Neo QLED (`media_player.qn85f1443`, Samsung's 2026
native Google Cast receiver). The script lives only in the live HA config
(`/config/scripts.yaml`, backup `.bak-20261114`); bg has the same cast — see
the **bg twin** below. **Seen on the physical bnu TV on 2026-11-14 evening:** the 2560x720
`src=canteiro_mosaico` leaves the Cast receiver `idle` (it never requests the
URL) while the HEVC single lens and a 1920x540 H.264 mosaic play — hence
`canteiro_mosaico_tv`, the second output of the composer in the Frigate LXC
(`mosaico-tv` on canteiro-hls). Do NOT build it with a go2rtc `ffmpeg:` source
on the Pi: the software decode took the load to 9 and stalled both camera
pulls. mediamtx does not pick up a new path by itself — `docker restart
canteiro-hls` after editing `mediamtx.yml`. Backups of this change:
`scripts.yaml.bak-20261114b` (bnu), `/config/canteiro_na_tv.yaml.bak-20261114b` (bg).
**Progressive `stream.mp4?src=canteiro_mosaico_tv` plays but rebuffers every
5-10 s** (the player sits on the live edge with no cushion; Eduardo, on the
TV); mediamtx's HLS gives it ~3 segments of reserve and played with no
`buffering` state in 70 s. The URL needs `?cookieCheck=1` (without it mediamtx
answers 302 + a `Secure` cookie the receiver cannot keep) and has NO `/hls`
prefix on `:8888` (that prefix exists only behind `tailscale serve`). When the composer restarts, the HLS muxer is recreated and the TV keeps
flipping `buffering`/`playing` every ~11 s until the cast is started again —
`automation.canteiro_na_tv_retomar_apos_travar` (bnu HA, id `canteiro_na_tv_retomar`,
created through the config API 2026-11-14) puts the cast back. Rewritten three
times in the night test of 2026-11-17 (the TV was watched for 12 h, bytes pulled
from `:8888` included, because HA's `media_position` does not move while playing):
triggers = the TV enters `buffering`, or sits 10 s in `idle` — both only while the
receiver still has `mosaico-tv` loaded (stopping the cast closes the app and clears
the content id; a TV switched off is `off` and never triggers). Action = wait up to
30 s for 20 s of steady `playing` (a healthy rebuffer passes here and nothing
happens); otherwise rerun `script.canteiro_na_tv` and wait again (40 s), up to 40
times, giving up when the TV goes `off` or shows something else. Why each piece:
03:17 the single retry fired 7 s before the mosaic was back and the TV ended
`idle`; 06:19 the TV went `playing` → `idle` with no `buffering` at all; 06:47,
07:28 and 07:59 the player flipped `buffering`/`playing` for 5 min pulling only
the playlist (18 kB/10 s) and then switched itself `off`, never adding up 8 s of
`buffering` — that mode follows a canteiro-hls restart (the watchdog heal, which
is what brings the mosaic back: see its README). The final version recovered the
09:34 drop by itself (8 recasts, playing 26 s after the mosaic returned). Not covered: the TV going `off` on its own
(01:36, stream healthy, cause unknown — Samsung auto power-off or a person).
Previous versions: `C:/Users/eduar/wt/tv-noite/retomar-antes-1117*.json` (mia-desktop).
Do NOT restart `canteiro-mosaico` repeatedly to test: two restarts within
minutes made the canteiro-watchdog auto-heal restart go2rtc + canteiro-hls twice
(🔧 🔧 🚨 on WhatsApp), after which the composer sat alive without publishing
until a manual `systemctl restart canteiro-mosaico`. bg still casts
`stream.mp4?src=canteiro_mosaico_tv` through its go2rtc proxy (no HLS proxy
there). Backup: `scripts.yaml.bak-20261114c`.
The other findings below are from the HEVC stream.

The **bg twin** (2026-10-29) is bg HA `script.canteiro_na_tv` → the
living-room 50" QLED (`media_player.50_qled`), fetching
`stream.mp4?src=canteiro_mosaico_tv` (since the evening of 2026-11-14;
`src=canteiro_mosaico` earlier that day, `src=canteiro` before; live file
`/config/packages/canteiro_na_tv.yaml`, backups
`/config/canteiro_na_tv.yaml.bak-20261114` and `…bak-20261114b`, repo copy
`scripts/proxmox/homeassistant/bg-homeassistant/packages/`) through the
`canteiro-tv-proxy` forward on bg-proxmox (`http://192.168.0.120:1984/…`,
see `scripts/proxmox/bg-proxmox/`) — one more reader of the shared
producer, so ara's Starlink still carries one copy; bnu's WAN upload
carries ~1.2 Mbps extra while it plays (through the bg forward: 3.0 MB in
20 s, 2026-11-15). That is the progressive URL that rebuffers on the bnu
TV: bg-proxmox forwards only go2rtc's `:1984`, not canteiro-hls `:8888`, so
the HLS cast is not reachable from the bg LAN yet. Not yet seen on the bg
TV with any mosaic URL. Its own start/stop links are `BG_TV_WEBHOOK_ON` /
`BG_TV_WEBHOOK_OFF`.

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
  native HEVC decode (2304×1296 passthrough, no transcode). This was the
  production path until 2026-11-13; the mosaic uses the same
  `stream.mp4` mechanism with H.264.
- 55" via Cast + go2rtc **HLS**: the Default Media Receiver launches but
  never fetches segments (CORS headers were present; the receiver just
  rejects this HLS flavor). mediamtx's HLS (fMP4, H.264) DOES play — see above.
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
| `~/go2rtc/go2rtc.yaml.bak-20261114` · `/usr/local/share/canteiro-live/index.html.bak-20261114` | pre-mosaic copies (rollback together with `~/canteiro-hls/mediamtx.yml.bak-20261114`) |

## Operate

```bash
python scripts/devtool.py run bnu-raspberrypi "docker ps --filter name=go2rtc"
python scripts/devtool.py run bnu-raspberrypi "curl -s http://127.0.0.1:1984/api/streams | head -5"
python scripts/devtool.py run bnu-raspberrypi "docker logs go2rtc --tail 20"
python scripts/devtool.py run bnu-raspberrypi "curl -s -o /dev/null -w '%{http_code} %{size_download}\n' 'http://127.0.0.1:1984/api/frame.jpeg?src=canteiro_mosaico'"
```

Streams are pulled from the relay **on demand** (go2rtc connects upstream
only while a client is reading). Since 2026-08-26 the bnu Frigate holds
`canteiro` + `canteiro_sub` open 24/7 (record + detect), since 2026-11-04
also `canteiro_fixa_sub` (the fixed lens' 640×480 substream, its detect
feed) and since 2026-11-05 `canteiro_fixa` (the fixed lens' HEVC main, its
record feed — since 2026-11-14 through canteiro-hls, which holds both mains
open 24/7 on its own), so the Starlink upload carries **two main streams + two
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
`canteiro_fixa` back at `canteiro_fixa_sub` (until 2026-11-14 go2rtc then
dropped the pull on its own; since the mosaic, canteiro-hls keeps pulling
`canteiro-alt` for it — freeing the uplink now also means stopping
`canteiro-mosaico` and removing the `canteiro-fixa` path). Watch: `ssh ara-raspberrypi2 'docker logs canteiro-relay --tail
300 | grep -c "too slow"'`, Frigate `/api/stats` `camera_fps` of both
cameras, go2rtc `i/o timeout` lines, and the ad-hoc sampler
`~/uplink-monitor/uplink.csv` on ara (10 s rows: dish up/down bps, ping,
drop, tailscale0/wlan0 Mbps, relay discards, per-reader TCP retransmits;
`python3 ~/uplink-monitor/uplink_report.py <csv> <split ISO UTC>` prints
before/after statistics).
