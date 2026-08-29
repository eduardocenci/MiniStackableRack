# canteiro-jobs — the canteiro WhatsApp jobs as Docker containers

One shared image (`canteiro-jobs:local`), three containers — migrated from
systemd timers on 2026-08-29 (decisão Eduardo):

| Container | Schedule | What it does |
|---|---|---|
| `canteiro-watchdog` | 60 s loop (shell loop, script stays a oneshot) | WhatsApp alert with the last cached frame when the ARA canteiro relay drops; recovery message when it returns |
| `canteiro-presenca` | supercronic, daily 20:00 America/Sao_Paulo | "who was at the obra today" report from ara netoverview `/api/presence` |
| `canteiro-sunset-compare` | supercronic, Mon–Fri 20:10 America/Sao_Paulo | yesterday-vs-today sunset montage (rclone ⇄ Drive, ffmpeg vstack), archived to Drive + sent to WhatsApp |

The scripts themselves stay authoritative in their sibling folders
([`../../canteiro-watchdog/`](../../canteiro-watchdog/),
[`../../canteiro-presenca/`](../../canteiro-presenca/),
[`../../canteiro-sunset-compare/`](../../canteiro-sunset-compare/)) — this
folder is only the container packaging.

## Live layout on the Pi (`~/canteiro-jobs/`)

```
~/canteiro-jobs/
├── Dockerfile, compose.yml, crontab-presenca, crontab-sunset-compare  (from here)
├── canteiro-watchdog.py, canteiro-presenca.py, canteiro-sunset-compare.py  (from the script folders)
└── env/canteiro-{watchdog,presenca,sunset-compare}.env   (live env — NOT in git;
    moved from /etc/canteiro-*.env at migration, chmod 600)
```

State: the watchdog bind-mounts the same `/var/lib/canteiro-watchdog` the
systemd unit used (state.json + lastframe.jpg) — no re-seed on migration or
rollback. sunset-compare mounts `~/.config/rclone` rw (token refresh writes
back; container runs as uid 1000 so ownership never flips).

## Deploy (from this repo)

```bash
# copy packaging + scripts into the build context on the Pi
for f in docker/canteiro-jobs/{Dockerfile,compose.yml,crontab-presenca,crontab-sunset-compare} \
         canteiro-watchdog/canteiro-watchdog.py canteiro-presenca/canteiro-presenca.py \
         canteiro-sunset-compare/canteiro-sunset-compare.py; do
  MSYS_NO_PATHCONV=1 python scripts/devtool.py push bnu-raspberrypi \
    "scripts/raspberry-pi/bnu-raspberrypi/$f" "canteiro-jobs/$(basename $f)"
done
python scripts/devtool.py run bnu-raspberrypi \
  "cd ~/canteiro-jobs && docker compose build && docker compose up -d"
```

Not yet on the 5-min DockerHub pull cron — the image is built locally on the
Pi. TODO: GitHub Actions in this repo → `cenci/canteiro-jobs:latest` →
switch `image:` and add the cron line (then deploy = git push, like
netoverview/globalnet).

## Test (SmokeTests group ONLY — never the family/timelapse groups)

```bash
python scripts/devtool.py run bnu-raspberrypi "docker exec canteiro-watchdog python3 /app/canteiro-watchdog.py --test-alert 120363410899542847@g.us"
python scripts/devtool.py run bnu-raspberrypi "docker exec canteiro-presenca python3 /app/canteiro-presenca.py --test"
python scripts/devtool.py run bnu-raspberrypi "docker exec canteiro-sunset-compare python3 /app/canteiro-sunset-compare.py --test"
```

(`--test`/`--test-alert` default to the SmokeTests `TEST_JID` for presenca and
sunset-compare, but the watchdog's flag defaults to the REAL group — always
pass the SmokeTests JID explicitly there.)

## Rollback

The old unit files stay on the Pi, disabled, for one wave:
`docker compose stop <name>` + `sudo systemctl enable --now <name>.timer`
(watchdog/presenca/sunset-compare). The watchdog state dir is shared, so no
state surgery is needed in either direction.
