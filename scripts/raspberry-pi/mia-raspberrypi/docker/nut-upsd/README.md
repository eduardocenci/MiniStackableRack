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
| `mia-nas-ds918plus` | DSM UPS client since 26/11/2026: type "Synology UPS server", server `192.168.2.10`, safe mode "until low battery", UPS not shut down. DSM shows it online (CP1000PFCLCD, charge and runtime) |

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
battery 100 %, runtime 1560 s, transfer 88–139 V.

## Low battery = 10 minutes of runtime left (regra Eduardo 26/11/2026)

`ups.conf` sets `ignorelb` + `override.battery.runtime.low = 600` (+ `override.battery.charge.low = 10`):
the driver ignores the UPS's own low-battery flag (raised at 300 s / 10 %) and sets `LB`
itself when the estimated runtime drops below 600 s or the charge below 10 %. Nothing is
written to the UPS. `LB` is what the clients act on: on `OB LB` the NAS enters safe mode
(services stopped, volumes unmounted) and restarts by itself when the grid is back.
Check: `upsc ups@127.0.0.1 battery.runtime.low` → `600`, `driver.flag.ignorelb` → `enabled`.

The nobreak feeds a Tapo P316M strip — outlets: 1 Verizon 5G Home · 2 Milwaukee battery charger · 3 rack strip (MiniPC, network devices, KVM) · 4 NAS · 5 Anker USB power supply (Pi, monitor) · 6 empty. A rule to power off the
other devices (MiniPC, Pi) is still being designed.

Not verified yet: a real discharge down to the threshold.

**NAS side.** Set and read with the DSM web API as root (no DSM login needed):

```bash
synowebapi --exec api=SYNO.Core.ExternalDevice.UPS version=1 method=get
synowebapi --exec api=SYNO.Core.ExternalDevice.UPS version=1 method=set enable=true \
  mode='"SLAVE"' net_server_ip='"192.168.2.10"' delay_time=-1 shutdown_device=false
```

`delay_time=-1` = "until low battery"; `enable=false` turns the client off.

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
