# canteiro-hls — mediamtx HLS packager for the /live page (bnu-raspberrypi)

Packages the canteiro stream into **proper HLS** for browsers, because
go2rtc's built-in HLS is unusable over a jittery path. It reads the shared
go2rtc producer on localhost — ara's Starlink still carries exactly one
copy of the stream (single-pull rule, [`../go2rtc/`](../go2rtc/)).
Docker container since 2026-08-29 (was a systemd unit + hand-downloaded
binary), image pinned to the same mediamtx version.

```
go2rtc rtsp://127.0.0.1:8554/canteiro ──▶ mediamtx (canteiro-hls container)
                                           └─ :8888/canteiro/index.m3u8 (fMP4/HEVC HLS,
                                              4 s segments, 7-segment ≈ 35 s window)
                                              └─ tailscale serve /hls → the /live page
```

## Why this exists (diagnosed 2026-08-29)

The /live page ("stream keeps interrupting" complaint) originally used
go2rtc's own `stream.m3u8`. Measured behavior of that endpoint:

- live window of just **2 segments advertised as 0.5 s each** (~1 s total),
  while the real segment cadence was ~2 s — hls.js's latency math breaks on
  the lie, and any hiccup > 1 s rotates the segment out from under the
  player → fatal error → restart;
- the camera keyframe interval is **4 s** (632 KiB I-frame burst, then
  8–16 KiB/s of P-frames), so every restart also waited up to 4 s for a
  picture;
- remote viewers often ride a **DERP relay** (tailscale direct connection
  unavailable; ~200 ms RTT via `sao`), where segment fetches take 2–7 s —
  fatal with a 1 s window, trivial with a 35 s one.

A 60 s soak through the tailnet after the switch: 16 consecutive segments,
0 errors, sustained 1.4–4.1 Mbps vs the ~1.3 Mbps stream. go2rtc's HLS rows
stay in the [`../go2rtc/README.md`](../go2rtc/README.md) table only as
diagnostics.

## Gotchas

- **`?cookieCheck=1` must be in the entry URL.** mediamtx (≥ v1.20) answers
  a bare `index.m3u8` request with a 302 to `/canteiro/index.m3u8?cookieCheck=1`
  — an absolute path that escapes the `/hls` mount, because `tailscale
  serve` strips the mount prefix before proxying. Passing the param up
  front skips the redirect entirely (200 straight away); variant playlists
  and segments are never redirected. The param trips no mainstream
  adblock list (EasyPrivacy/uBlock checked 2026-08-29).
- **`moq: no` stays**: mediamtx v1.20 enables a MoQ server by default and
  tries to write a self-signed cert to the CWD (denied under the old
  hardened systemd unit, pointless noise in the container — the ports are
  useless here either way).
- Segments land at 4 s (= camera GOP), occasionally 8 s when a keyframe
  misses the boundary — `#EXT-X-TARGETDURATION:8` is normal.
- The muxer stays hot (`hlsAlwaysRemux`), so page loads start immediately
  instead of waiting for a keyframe; the localhost re-read from go2rtc is
  free.

## Install layout (live)

| File | Purpose |
|---|---|
| `~/canteiro-hls/compose.yml` | copy of [`compose.yml`](compose.yml) — image `bluenviron/mediamtx:1.20.1` (pinned; same version as the ara relay's binary) |
| `~/canteiro-hls/mediamtx.yml` | copy of [`mediamtx.yml`](mediamtx.yml), ro-mounted into the container |

(Until 2026-08-29 this ran as a systemd unit with a hand-downloaded binary
at `/usr/local/bin/mediamtx`; the disabled unit stays on the Pi for one
wave as rollback.)

The `tailscale serve` route (persists across reboots):

```bash
sudo tailscale serve --bg --set-path=/hls http://127.0.0.1:8888
```

## Operate

```bash
python scripts/devtool.py run bnu-raspberrypi "docker ps --filter name=canteiro-hls"
python scripts/devtool.py run bnu-raspberrypi "curl -s 'http://127.0.0.1:8888/canteiro/index.m3u8?cookieCheck=1'"
python scripts/devtool.py run bnu-raspberrypi "docker logs canteiro-hls --tail 20"
```
