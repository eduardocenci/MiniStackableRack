# go2rtc — canteiro restream for LAN devices (bnu-raspberrypi)

Bridges the tailnet-only ARA camera relay onto the bnu house LAN, and is the
**single upstream consumer**: everything at bnu (the wall screen AND the TV
cast) reads from this go2rtc, which pulls the canteiro from ara exactly once:

```
rtsp://ara-raspberrypi:8554/canteiro ──tailnet (1 copy)──▶ go2rtc on bnu-raspberrypi
                                                             ├─ rtsp://127.0.0.1:8554/canteiro → canteiro-screen (mpv)
                                                             └─ /api/stream.mp4?src=canteiro → 55" TV (Cast)
```

**Why single-pull (2026-08-26 incident):** with the wall screen reading ara
directly *and* go2rtc pulling for the TV, daytime bitrate × 2 streams
saturated the canteiro's Starlink upload — the relay logged "reader is too
slow, discarding frames" and the TV froze. One upstream copy fixed it.

**Do not cast the `canteiro_h264` transcode:** ffmpeg software-decoding the
3MP HEVC pushes the Pi 4 to load ~6 and starves everything on it (seen
2026-08-26). The TV plays the HEVC passthrough natively; the transcode path
exists only as a compatibility fallback for other devices, used sparingly.
If the TV ever needs H.264 permanently, restream the camera's native
substream (channel=1&subtype=1, H.264 640×480 — zero transcode) instead.

| URL (LAN `10.1.1.123` / tailnet `bnu-raspberrypi` = `100.91.64.62`) | Content |
|---|---|
| `…:1984/api/stream.mp4?src=canteiro` | HEVC passthrough (2304×1296) — the TV cast |
| `…:1984/stream.html?src=canteiro&mode=mse` | **browser live view** (phone/PC bookmark; HEVC via MSE — verified in-browser 2026-08-26) |
| `…:1984/stream.html?src=canteiro_h264` | browser fallback if a device can't decode HEVC (starts the on-demand transcode — occasional use only) |
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
