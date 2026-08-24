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

Consumed by the **bnu HA script `script.canteiro_na_tv_60`** ("Canteiro na
TV 60\""), which wakes the Samsung AU8000 60" (`samsungtv` entity, WoL) and
pushes the MP4 URL to its DLNA renderer (`media_player.samsung_au8000_60_tv`).
The 55" Neo QLED is a Google Cast target instead (`media_player.qn85f1443`)
— it can cast the same URL once "Power On with Mobile" is enabled on it.

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
