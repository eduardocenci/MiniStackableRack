# raspberry-pi — rack Pi fleet

One Pi per rack site (`<site>-raspberrypi`), each a 29 GB-SD Raspberry Pi 4
running the netoverview container (bnu additionally runs globalnet). The ara
Pi (`ara-raspberrypi/`) is a home-build device — all-Docker since 2026-08-29;
replacement unit since 2026-09-28 (Pi 4 4 GB, 32 GB SD, staged off-site
behind `ara-raspberrypi/site-gate/`); registered 2026-08-26
in `globalnet/architecture.yaml` as a `home: true` site (decisão Eduardo),
so `make fleet` audits it like the rack Pis.

## rpi-health — temperature, throttling and SD health (fleet-wide)

[`rpi-health/`](rpi-health/) is a host systemd service, identical on every
Pi, that exports the PMIC temperature and the `vcgencmd get_throttled`
history to the node-exporter every Pi already runs; globalnet turns that
into the health row on each Pi card (SoC/PMIC °C, throttle LED with a 24 h
memory, SD bar, uptime) and into a WhatsApp alert when a card's root
filesystem goes read-only. `python scripts/raspberry-pi/rpi-health/deploy.py
<site>…` installs or updates it; the folder README has the metric list and
the rollout log.

## Docker auto-update MUST include a prune

Every rack Pi self-updates its containers with the 5-min pull cron from the
root `CLAUDE.md`. **Each pull of a new `:latest` untags the previous image,
and on a 29 GB SD card the dangling layers eventually fill the disk** —
audited 2026-08-26 after bnu hit 91 %: bnu had 12.15 GB of danglings, bg
~9 GB (plus no cron at all — installed that day), fln 3.93 GB. The fix is a
`docker image prune -f` in the same cron line — it removes dangling images
only, never the tagged `:latest` the running container uses:

```
*/5 * * * * cd ~/netoverview && docker compose pull -q && docker compose up -d && docker image prune -f >/dev/null
```

Per-site state of that cron (user crontab of `eduardocenci`):

| Pi | Update mechanism | Prune since |
|---|---|---|
| `bnu-raspberrypi` | two cron lines (`~/globalnet` + `~/netoverview`) — see [`bnu-raspberrypi/README.md`](bnu-raspberrypi/README.md) | 2026-08-26 |
| `bg-raspberrypi` | one cron line (`~/netoverview`), exactly as above | 2026-08-26 (cron was missing entirely before) |
| `fln-raspberrypi` | cron runs `~/netoverview/update.sh` (logs to `update.log`); prune is the script's last line | 2026-08-26 |
| `mia-raspberrypi` | back online since 2026-08-28 (was off ~2026-08-04→28 during the Plymouth→Miami move); audit + add prune | pending |
| `ara-raspberrypi2` | one cron line (`~/netoverview`), exactly as above. On the replacement unit it is written by `site-gate` when the Pi arms on the ara LAN, never off-site | 2026-08-26 (deployed with prune from day one) |

`docker system df` shows the image bloat; the bytes live under
`/var/lib/containerd` (containerd image store), not `/var/lib/docker`.
`sudo apt-get clean` is the other recurring win (1–3 GB of package cache
per Pi). `sudo` needs a password on fln — see `REMOTE_ACCESS.md` §3.
