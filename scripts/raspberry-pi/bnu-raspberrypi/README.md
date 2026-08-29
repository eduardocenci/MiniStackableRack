# bnu-raspberrypi — rack Pi (Blumenau SC)

Network monitoring node of the bnu rack; also the bnu-side hub of the ARA
build-site camera (restream, live pages and WhatsApp reports). It no longer
drives its attached screen — the mpv wall view (`canteiro-screen`) was
removed on 2026-08-29 (decisão Eduardo: camera on the Pi screen not needed).

| Fact | Value |
|---|---|
| Hardware | Raspberry Pi 4 Model B Rev 1.5, 4 GB RAM, 29 GB SD (`/dev/mmcblk0p2`) |
| OS | Raspberry Pi OS (Debian 13 trixie), aarch64, desktop image (labwc/LightDM) |
| Tailnet | `bnu-raspberrypi` (100.91.64.62) |
| SSH | `ssh eduardocenci@bnu-raspberrypi` — key auth (see `REMOTE_ACCESS.md`) |

## Services

| Service | What it does |
|---|---|
| [`docker/globalnet/`](docker/globalnet/) | Multi-site dashboard container (`cenci/globalnet:latest`, port 5001→5050) |
| netoverview | LAN discovery container (`cenci/netoverview:latest`, host networking; compose lives on the Pi at `~/netoverview/`, sourced from the netoverview repo) |
| [`docker/go2rtc/`](docker/go2rtc/) | Restream hub of the ARA canteiro camera — single Starlink pull fanned out to the TV cast, browser live view and the bnu Frigate NVR (LXC 105) |
| [`docker/canteiro-hls/`](docker/canteiro-hls/) | mediamtx container packaging the local go2rtc producer into proper HLS (4 s segments, ~35 s window) for the browser `/live` page — go2rtc's own HLS window (~1 s) kept collapsing (2026-08-29) |
| [`canteiro-watchdog/`](canteiro-watchdog/) | container, 60 s loop ([`docker/canteiro-jobs/`](docker/canteiro-jobs/)): WhatsApp alert with the last frame when the ARA canteiro relay drops, recovery message when it returns |
| [`canteiro-presenca/`](canteiro-presenca/) | container, daily 20:00 America/Sao_Paulo ([`docker/canteiro-jobs/`](docker/canteiro-jobs/)): WhatsApp report of how many people were at the ARA obra today, from the ara netoverview `/api/presence` |
| [`canteiro-sunset-compare/`](canteiro-sunset-compare/) | container, Mon–Fri 20:10 America/Sao_Paulo ([`docker/canteiro-jobs/`](docker/canteiro-jobs/)): fetches yesterday's + today's `posicao1/por-do-sol` frames from Drive `CeuAzul/Timelapse/` (rclone), stacks them vertically (ffmpeg) and sends the "Dia de Trabalho" comparison to WhatsApp via WAHA `sendImage` |
| wayvnc | VNC access to the desktop session |

## Docker auto-update cron (and disk-space guard)

Both containers self-update via the user crontab (`crontab -l` as
`eduardocenci`) — the standard 5-min pull described in the root `CLAUDE.md`,
**plus `docker image prune -f`**:

```
*/5 * * * * cd ~/globalnet && docker compose pull -q && docker compose up -d && docker image prune -f >/dev/null
*/5 * * * * cd ~/netoverview && docker compose pull -q && docker compose up -d
```

The prune is not optional on this Pi. Every pull of a new `:latest` untags
the previous image, and with a 29 GB SD card the dangling layers eventually
fill the root filesystem: on 2026-08-24 the disk hit 91 % with **65 dangling
images (12.15 GB reclaimable)** accumulated under `/var/lib/containerd`
(Docker uses the containerd image store here, so `du` shows the space there,
not in `/var/lib/docker`). `docker image prune -f` removes dangling images
only — never the tagged `:latest` images the running containers use — so it
is safe to run right after each pull. `sudo apt-get clean` freed another
2.8 GB of package cache in the same incident (91 % → 40 %).

If the crontab is ever rewritten, keep **both** lines: the netoverview pull
line was found missing on 2026-08-24 (presumably clobbered when the
globalnet line was installed), which had left the netoverview container
7 weeks stale.
