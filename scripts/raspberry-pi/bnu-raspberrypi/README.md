# bnu-raspberrypi — rack Pi (Blumenau SC)

Network monitoring node of the bnu rack; also drives the canteiro screen
(live view of the ARA build site camera).

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
| [`go2rtc/`](go2rtc/) | Restream hub of the ARA canteiro camera — single Starlink pull fanned out to the wall screen, TV cast, browser live view and the bnu Frigate NVR (LXC 105) |
| [`canteiro-screen/`](canteiro-screen/) | systemd unit: mpv fullscreen of the ARA site camera (reads the local go2rtc) |
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
