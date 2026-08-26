# raspberry-pi — rack Pi fleet

One Pi per rack site (`<site>-raspberrypi`), each a 29 GB-SD Raspberry Pi 4
running the netoverview container (bnu additionally runs globalnet). The ara
Pi (`ara-raspberrypi/`) is a home-build device — systemd services plus the
netoverview container since 2026-08-26 (58 GB SD); it stays out of
`globalnet/architecture.yaml` because ara is not a rack site.

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
| `ply-raspberrypi` | **unknown — offline since ~2026-08-04**; audit + add prune when it comes back | pending |
| `ara-raspberrypi` | one cron line (`~/netoverview`), exactly as above | 2026-08-26 (deployed with prune from day one) |

`docker system df` shows the image bloat; the bytes live under
`/var/lib/containerd` (containerd image store), not `/var/lib/docker`.
`sudo apt-get clean` is the other recurring win (1–3 GB of package cache
per Pi). `sudo` needs a password on fln — see `REMOTE_ACCESS.md` §3.
