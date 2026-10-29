# canteiro-hls — mediamtx: HLS for the /live page + bnu end of the main stream (bnu-raspberrypi)

Packages the canteiro stream into **proper HLS** for browsers, because
go2rtc's built-in HLS is unusable over a jittery path. Since 2026-10-29 it
is also **the single puller of the main stream** from the ara relay, with
`alwaysAvailable`, and go2rtc reads it on localhost — ara's Starlink still
carries exactly one copy (single-pull rule, [`../go2rtc/`](../go2rtc/)).
Docker container since 2026-08-29 (was a systemd unit + hand-downloaded
binary), image pinned to the same mediamtx version.

```
rtsp://ara-raspberrypi2:8554/canteiro ──tailnet (1 copy)──▶ mediamtx (canteiro-hls), path canteiro
                                                             │  alwaysAvailable: offline.mp4 fills any gap
                                                             ├─ :8888/canteiro/index.m3u8 (fMP4/HEVC HLS,
                                                             │  4 s segments, 7-segment ≈ 35 s window)
                                                             │  └─ tailscale serve /hls → the /live page
                                                             └─ rtsp://127.0.0.1:8555/canteiro (TCP, local only)
                                                                └─ go2rtc `canteiro` → Frigate record, TV, MSE, watchdog frame
(canteiro_sub still goes relay → go2rtc directly: Frigate detect only)
```

## PTZ-excursion EOFs and `alwaysAvailable` (2026-10-29)

**Symptom:** 1–3 `🔧 Auto-heal do /live` + `✅ recuperado` pairs in Casa
SmokeTests per solar sequence, almost every day (20–29/10), `/live` dead
~100 s each time. **Cause, from the logs of 29/10 05:27/05:37/05:47 BRT:**

1. the `timelapse-capture` on ara moves the PT lens; at the start of most
   excursions the camera closes its RTSP session — the relay logs
   `[path canteiro] [RTSP source] EOF`, drops its readers and reopens the
   camera 5 s later, with a **new random RTP timestamp base**;
2. go2rtc (then the WAN puller) logged `error=EOF` and **did** reconnect
   within ~6 s — but by swapping the producer under the *same* RTSP
   session it served to this container;
3. this muxer got `114616 RTP packets lost` (sequence jump) and a DTS that
   went from +485 s to −108 s → `DTS is not monotonically increasing`, then
   `sample timestamp is impossible to handle` every 10 s: a recreated muxer
   inherits the source session's timeline, which is now negative, so it
   never recovers while that session lives;
4. the watchdog saw ≥3 crashes in 90 s and restarted go2rtc + canteiro-hls.

**Fix:** pull the relay here and set `alwaysAvailable`. mediamtx sees the
EOF itself, keeps every reader connected (the HLS muxer, go2rtc and,
through it, Frigate/TV), plays [`offline.mp4`](offline.mp4) ("Sem sinal da
câmera do canteiro") on repeat, reconnects after 5 s and splices the camera
back on one continuous timeline. Tested on bnu with a twin chain on spare
ports (source killed 5×): the HLS media sequence never broke, no muxer
error, 0 decode errors across card ↔ camera segments, an RTSP reader kept
the same session (bytes still flowing), camera back 5–10 s after the EOF
(+5 s when the retry lands before the relay's own camera reconnect → 404).

**Rejected:** (a) this container pulling the relay *in addition to*
go2rtc — +1.4 Mbps on the Starlink upload (measured 170 KB/s), the
2026-08-26 stall; (b) a go2rtc setting — none breaks consumer sessions or
re-bases timestamps on a producer swap, and an `ffmpeg:` source sees the
same jump through the loop; (c) `readTimeout`/`sourceOnDemand` here —
packets keep flowing after the swap, so nothing times out, and a short
timeout would also fire on ordinary Starlink stalls.

**Card constraints:** mediamtx refuses a source whose tracks differ from
the offline stream's (`wants to publish [H265 MPEG-4 Audio], but stream
expects [H265]`), and the built-in card (`alwaysAvailableTracks`) rejects
AAC below 22050 Hz — the iM9 sends 16 kHz mono. Hence a file with the
camera's exact tracks: H265 Main 2304×1296 `hvc1` + silent AAC-LC 16 kHz
mono, 1 s, one keyframe per loop; same resolution so readers never see a
size change. [`make-offline-card.sh`](make-offline-card.sh) regenerates it
on the Pi (ffmpeg from the pinned go2rtc image, DejaVu fonts from the host).

**Side effects (accepted):** during a camera or Starlink outage every
go2rtc consumer of `canteiro` gets the card instead of an EOF — the TV and
MSE viewers show it, Frigate records it (discarded: retention keeps only
alert/detection windows, and detection runs on `canteiro_sub`), and the
watchdog's cached "last frame" can be the card. go2rtc's `canteiro` now
depends on this container; restart order does not matter (go2rtc retries
every second).

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
  instead of waiting for a keyframe.
- **`docker logs canteiro-hls` can go blind**: from 2026-09-03 to
  2026-10-29 it stopped at 09-03 while the json log file kept growing
  (86 MB). The cause, confirmed on the ara relay the same day: a NUL-filled
  line from a power cut or hard reset stops every forward read (`docker
  logs`, `--since`, the API with `since=`), while `--tail N` still works.
  The watchdog's crash counter reads with `since=`, so it was blind too
  (heals came from the playlist test). The recreate on 2026-10-29 started a
  clean file. Test: `sudo grep -a -c -P '\x00' /var/lib/docker/containers/<id>/<id>-json.log`
  (`<id>` from `docker inspect -f '{{.Id}}' canteiro-hls`); if it is > 0, read the file
  with `sudo grep -a`, or `docker compose up -d --force-recreate`.
- `ERR [path canteiro] [RTSP source] EOF` followed 5–10 s later by
  `[path canteiro] stream is online` (sometimes with a `bad status code:
  404` in between) is the fix working at each excursion, not an incident;
  `ERR [HLS] [muxer canteiro] muxer instance crashed` is the old failure and
  should no longer appear.

## Install layout (live)

| File | Purpose |
|---|---|
| `~/canteiro-hls/compose.yml` | copy of [`compose.yml`](compose.yml) — image `bluenviron/mediamtx:1.20.1` (pinned; same version as the ara relay's binary) |
| `~/canteiro-hls/mediamtx.yml` | copy of [`mediamtx.yml`](mediamtx.yml), ro-mounted into the container |
| `~/canteiro-hls/offline.mp4` | copy of [`offline.mp4`](offline.mp4) — the `alwaysAvailable` card, ro-mounted at `/offline.mp4` |
| `~/canteiro-hls/make-offline-card.sh` | copy of [`make-offline-card.sh`](make-offline-card.sh) — regenerates `offline.mp4` in place |
| `~/canteiro-hls/*.bak-20261029` | pre-`alwaysAvailable` `compose.yml`/`mediamtx.yml` (rollback: restore them **and** `~/go2rtc/go2rtc.yaml.bak-20261029`, then `docker compose up -d` here and `docker restart go2rtc`) |

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
# offline/online pairs since a given UTC time (the docker logs view is stale, see Gotchas)
python scripts/devtool.py run bnu-raspberrypi "id=\$(docker inspect -f '{{.Id}}' canteiro-hls); sudo grep -a -E 'is online|RTSP source|crashed' /var/lib/docker/containers/\$id/\$id-json.log | grep -a '2026-10-30T08' | cut -c9-110"
```
