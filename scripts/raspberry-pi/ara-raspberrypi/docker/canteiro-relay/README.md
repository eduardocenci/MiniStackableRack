# canteiro-relay — canteiro camera relay (mediamtx, ara-raspberrypi)

Relays the Intelbras iM9+ Full Color site camera (`192.168.1.56`, Wi-Fi,
LAN-only) onto the tailnet so any fleet node can watch the obra without
touching the camera or the house LAN. Docker container since 2026-08-29
(`canteiro-relay`, image `bluenviron/mediamtx:1.20.1` pinned; was a systemd
unit + hand-downloaded binary, left disabled on the Pi one wave as rollback):

```
iM9 camera ──RTSP 554 (LAN)──▶ canteiro-relay on ara-raspberrypi ──RTSP 8554 (tailnet)──▶ go2rtc on bnu-raspberrypi
                                                                      (single consumer)     ├─ TV cast + /live HLS page
                                                                                            └─ bnu Frigate (record + detect)
```

**Cutover/restart guard:** the bnu `canteiro-watchdog` container pages the
family ~3 min after `:8554` goes dark — `docker stop canteiro-watchdog` on
bnu first for anything longer than a blip. After a relay restart the bnu
`canteiro-hls` muxer can crash-loop on the RTSP timestamp jump ("sample
timestamp is impossible to handle", /live answers 500) —
`docker restart canteiro-hls` on bnu fixes it (seen 2026-08-29).

| Path | What it is |
|---|---|
| `rtsp://ara-raspberrypi:8554/canteiro` | lens on `channel=1`, main stream (HEVC 2304×1296) — pulled 24/7 (`sourceOnDemand: no`) |
| `rtsp://ara-raspberrypi:8554/canteiro-alt` | lens on `channel=2`, main stream — pulled on demand |
| `rtsp://ara-raspberrypi:8554/canteiro-sub` | lens on `channel=1`, substream (H264 640×480) — on demand; bnu Frigate's detect feed (held open 24/7 while Frigate is up, added 2026-08-26) |

The relay has no reader auth: it is reachable only from the tailnet and the
house LAN, and the camera credential stays on this Pi.

## Install layout (live)

| File | Purpose |
|---|---|
| `~/canteiro-relay/compose.yml` | copy of [`compose.yml`](compose.yml) — pinned image, host network |
| `~/canteiro-relay/mediamtx.yml` | config — copy of [`mediamtx.yml`](mediamtx.yml) with the real `chave de acesso` (600 `eduardocenci`, ro-mounted at `/mediamtx.yml`) |

## Camera key — install / update

While `ARA_CANTEIRO_CAM_KEY` is unknown, the **live config keeps the two
paths without a `source:`** (publisher mode) instead of the repo config's
camera URLs — a wrong key retried every 5 s keeps the camera's Dahua
anti-bruteforce lockout warm (401 → 403), so the relay must not pull until
the key is real. To install the key (from repo root, key in `.env`):

```bash
sed "s/__ARA_CANTEIRO_CAM_KEY__/<CHAVE>/g" scripts/raspberry-pi/ara-raspberrypi/docker/canteiro-relay/mediamtx.yml \
  | ssh eduardocenci@ara-raspberrypi "cat > ~/canteiro-relay/mediamtx.yml && chmod 600 ~/canteiro-relay/mediamtx.yml && docker restart canteiro-relay"
ssh eduardocenci@ara-raspberrypi "docker logs canteiro-relay --tail 20"   # expect "[path canteiro] source ready"
```

URL-encode the key first if it contains symbols. Keep the key in the
repo-root `.env` (`ARA_CANTEIRO_CAM_KEY`) — the live config here is a
credential mirror (listed in `REMOTE_ACCESS.md` §5).

## Troubleshooting

- `401` in the journal → wrong/missing chave de acesso (factory sticker key,
  not the app-changed one; reset the camera if the sticker key was rotated).
- `403 Forbidden` after 401s → the camera's anti-bruteforce lockout; stop
  retrying with bad credentials (revert the paths to publisher mode) and let
  it cool for some minutes before trying a corrected key.
- Source connects but no video → check the camera's "criptografia de
  imagem/vídeo" toggle in the Mibo Cam app (Imou twins block plain RTSP
  payload when image encryption is on).
- Camera IP drifted → it is a DHCP lease on the Starlink router; update
  `192.168.1.56` here or pin a reservation in the Starlink app.
- After replacing `~/canteiro-relay/mediamtx.yml`, always
  `docker restart canteiro-relay` — do NOT trust the hot-reload: it can
  catch the file mid-write, fall back to an all-defaults config (extra
  listeners on :1935/:8888/:8889/:8890/:8892, "path 'canteiro' is not
  configured") and never recover (seen 2026-08-24 on the systemd-era
  binary; same engine in the container).
