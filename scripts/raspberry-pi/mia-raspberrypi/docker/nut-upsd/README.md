# nut-upsd (mia) — NUT server for the rack nobreak

The mia rack nobreak — a **CyberPower CP1000PFCLCD** (1000 VA / 600 W) — is plugged
into the USB of `mia-raspberrypi` since 26/11/2026 (`0764:0501`, standard HID Power
Device). It feeds a TP-Link Tapo P316M power strip, and through it essentially every
device of the site (Eduardo 26/11/2026). This container owns that USB interface (NUT 2.8.0, driver `usbhid-ups`) and
serves the readings on **`:3493`** (NUT protocol), on the LAN and on the tailnet.

**Consumers**

| Who | How |
|---|---|
| `ups-monitor` (same Pi) | `LIST VAR ups` on `127.0.0.1:3493` every 1 s → WhatsApp alerts + globalnet card |
| `mia-nas-ds918plus` | DSM UPS client, type "Synology UPS server", server `192.168.2.10` — **not enabled yet** (the shutdown rules are still to be defined); the path is proven: `upsc ups@192.168.2.10` on the NAS answers |

**Read-only.** `upsd.users` has one account (`monuser` / `secret`, the fixed pair a
Synology uses — not a secret) with no `actions` and no `instcmds`: nobody can change a
setting, silence the beeper or command a shutdown of the UPS through this server.

The UPS must stay named `ups` in `ups.conf`: a Synology asks for exactly `ups@<server>`.

## Operate

```bash
# every variable the UPS reports
python scripts/devtool.py run mia-raspberrypi "docker exec nut-upsd upsc ups@127.0.0.1"
# driver / server log
python scripts/devtool.py run mia-raspberrypi "docker logs --tail 30 nut-upsd"
```

Readings on 26/11/2026: `ups.status OL`, input/output 118 V, load 20 % of 600 W,
battery 100 %, runtime 1560 s, `battery.runtime.low 300`, `battery.charge.low 10`,
transfer 88–139 V.

## Install / update

```bash
# from the repo root, Git Bash: MSYS_NO_PATHCONV=1 so /home/... is not rewritten
for f in Dockerfile compose.yml entrypoint.sh nut.conf ups.conf upsd.conf upsd.users; do
  python scripts/devtool.py push mia-raspberrypi scripts/raspberry-pi/mia-raspberrypi/docker/nut-upsd/$f /home/eduardocenci/nut-upsd/$f
done
python scripts/devtool.py run mia-raspberrypi "cd ~/nut-upsd && docker compose up -d --build"
```

The build installs `nut-server` from Debian (needs network). The entrypoint starts the
driver, restarts it every 10 s while it is not running (UPS unplugged, USB reset) and
runs `upsd` in the foreground.

Runbook: globalnet `docs/runbooks/ups-monitor.md` (section *mia*).
