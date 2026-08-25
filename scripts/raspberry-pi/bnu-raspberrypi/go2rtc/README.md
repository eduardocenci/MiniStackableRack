# go2rtc — canteiro restream for LAN devices (bnu-raspberrypi)

Bridges the tailnet-only ARA camera relay onto the bnu house LAN so dumb
renderers (Samsung DLNA TVs, anything that can fetch an HTTP video URL) can
play the obra live:

```
rtsp://ara-raspberrypi:8554/canteiro ──tailnet──▶ go2rtc on bnu-raspberrypi ──HTTP fMP4 on LAN──▶ TV
```

| URL (LAN) | Content |
|---|---|
| `http://10.1.1.123:1984/api/stream.mp4?src=canteiro` | HEVC passthrough (2304×1296) |
| `http://10.1.1.123:1984/api/stream.mp4?src=canteiro_h264` | H.264 hardware transcode (fallback for HEVC-rejecting renderers) |
| `http://10.1.1.123:1984/` | go2rtc web UI (diagnostics) |

Consumed by the **bnu HA script `script.canteiro_na_tv`** ("Canteiro na
TV"), which casts the **HEVC passthrough** URL to the 55" Neo QLED
(`media_player.qn85f1443`, Samsung's 2026 native Google Cast receiver).

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

| File | Purpose |
|---|---|
| `/usr/local/bin/go2rtc` | static binary (GitHub release, linux_arm64) |
| `/etc/go2rtc/go2rtc.yaml` | copy of [`go2rtc.yaml`](go2rtc.yaml) |
| `/etc/systemd/system/go2rtc.service` | copy of [`go2rtc.service`](go2rtc.service) (system user `go2rtc`, `video` group for v4l2m2m encode) |

## Operate

```bash
python scripts/devtool.py run bnu-raspberrypi "systemctl status go2rtc --no-pager"
python scripts/devtool.py run bnu-raspberrypi "curl -s http://127.0.0.1:1984/api/streams | head -5"
```

Streams are pulled from the relay **on demand** (go2rtc connects upstream
only while a client is reading), so this adds Starlink upload at the
canteiro only while a TV is actually playing.
