# ups-monitor (ara) — nobreak do canteiro (EcoFlow RIVER 3 Plus) → alertas de falta de energia

The canteiro **nobreak** is an **EcoFlow RIVER 3 Plus** portable power station
(286 Wh, 600 W, 1200 W peak) that Eduardo installed in the shed on his visit of
**2026-11-24**, with its USB port plugged into **ara-raspberrypi2**. The
`ups-monitor` container reads it every 2 s and sends **one WhatsApp message** in
each case:

- 🔴 **falta de energia**: the station has been on battery for 10 s.
- 🟠 **bateria abaixo de 20 %** during the outage.
- ✅ **energia voltou**: the grid has been back for 10 s.

Every transfer to battery (*ativação*) is counted. The **Nobreak (UPS)** card of
the ARA site on globalnet shows the counters for 24 h / 7 d / 30 d, the live
state, and the recent episodes and messages in its tooltip (`GET /api/ups`).

It is the same rule, outbox and HTTP API as the bnu rack
[`ups-monitor`](../../../bnu-raspberrypi/docker/ups-monitor/) (pedido Eduardo
24/11/2026: "integrar de maneira similar ao UPS de Blumenau"); only the reader
differs. Live since **2026-11-24 14:31 BRT** in `MODE=shadow` (messages go to
Casa SmokeTests with a `[sombra]` prefix).

Runbook (rule, sample messages, operate, failure modes): globalnet
`docs/runbooks/ups-monitor.md`.

## The station

| | |
|---|---|
| Station | **EcoFlow RIVER 3 Plus**, 286 Wh, 600 W (1200 W peak). USB reports manufacturer `EcoFlow`, product `EF-UPS-RIVER 3 Plus`, serial `R631Z61AXJ5D0256`, device release `1.00` |
| Link to the Pi | The station's USB port → a Pi USB 2.0 port. Device `3746:ffff`, bus-powered, 3 interfaces: **0 = HID Power Device** (the standard UPS class, used here) · 1–2 = CDC-ACM serial `/dev/ttyACM0` (not used) |
| Why not hidraw / NUT | The kernel's HID parser rejects the report descriptor (`unbalanced collection at end of report description`, probe error −22), so there is no `/dev/hidraw*`. Debian 13's NUT is 2.8.1, which has no EcoFlow subdriver. `ecoflow_hid.py` reads the reports directly from usbfs (`/dev/bus/usb/BBB/DDD`) with HID `GET_REPORT` |
| What it reports | Battery % (report `0x0c`) · its own runtime estimate in s (`0x0d`) · pack voltage (`0x0b`, 0.01 V) · status bits (`0x07`: charging, discharging, **AC present**, battery present, below capacity limit, …) |
| What it does NOT report | Input voltage, output voltage/frequency, load and temperature. Those fields of the ups-monitor reading are `null` here, and the dashboard card shows the runtime instead |
| Permissions | [`99-ecoflow-ups.rules`](99-ecoflow-ups.rules) gives the usbfs node to group `dialout` (mode 660), so the container runs as uid 1000 |

Snapshot on 2026-11-24 (on grid): battery 100 % · runtime 41 min · pack 13.00 V
(configured 13.80 V) · status `0x000c` (*AC present · battery present*) ·
warning limit 10 %.

## Files

| File | What |
|---|---|
| [`ecoflow_hid.py`](ecoflow_hid.py) | Read-only reader: finds `3746:ffff` in sysfs, claims HID interface 0 and sends **only** `GET_REPORT`. Also a one-shot CLI |
| [`ups_monitor.py`](ups_monitor.py) | The rule (written out in its header), SQLite store, WhatsApp outbox and HTTP API. Derived from the bnu file; outage = *AC not present* |
| [`test_ups_monitor.py`](test_ups_monitor.py) | Offline tests: the real captured reports plus simulated outages (blip, long outage, flapping grid, low battery, comm loss, restart mid-outage, outbox retry/order). Run with `python -m pytest -q test_ups_monitor.py` |
| [`Dockerfile`](Dockerfile), [`compose.yml`](compose.yml) | `python:3.13-slim`, stdlib only. Host network, user 1000 + group `dialout`, `/dev/bus/usb` bind-mounted with cgroup rule `c 189:*` (survives a re-plug), host zoneinfo |
| [`99-ecoflow-ups.rules`](99-ecoflow-ups.rules) | udev rule, live at `/etc/udev/rules.d/` |

Live copy: `ara-raspberrypi2:~/ups-monitor/`. State: `/var/lib/ups-monitor/ups.db`
(owned by 1000), with tables `episodes`, `outbox`, and `samples` (kept 90 days).
The stack is in the `STACKS` list of [`../../site-gate/`](../../site-gate/), so a
re-staged Pi starts it only on the ara LAN.

## Configuration

| Env (compose.yml) | Value |
|---|---|
| `MODE` | `shadow` = Casa SmokeTests with a `[sombra]` prefix. `live` = `ALERT_JID`; `log` = send nothing |
| `ALERT_JID` | Casa Céu Azul `120363410965618335@g.us` (= root `.env` `ARA_WHATSAPP_GROUP_JID`). Used only in `MODE=live` |
| `TEST_JID` | Casa SmokeTests `120363410899542847@g.us` (= `SMOKETESTS_WHATSAPP_GROUP_JID`): shadow messages and `POST /api/test` |
| `WAHA_URL/KEY/SESSION` | `env_file` = `~/ups-monitor/ups-monitor.env` (chmod 600, not in git). The bnu WAHA through the tailnet relay `http://bnu-proxmox:3001`, the same values as `ALERT_WAHA_*` in `~/canteiro-timelapse/env/alerts.env` (live copy of root `.env` `BNU_WAHA_API_KEY`) |
| `POLL_S` · `ALERT_AFTER_S` · `RESTORE_CONFIRM_S` · `LOW_BATTERY_PCT` | 2 · 10 · 10 · 20 |
| `DASHBOARD_URL` | unset: the 🔴 message carries no link (a tailnet URL is useless to the group) |
| `TZ` | `America/Sao_Paulo` |

## Deploy / update

```bash
# from the repo root
D=scripts/raspberry-pi/ara-raspberrypi/docker/ups-monitor
scp $D/ecoflow_hid.py $D/ups_monitor.py $D/Dockerfile $D/compose.yml eduardocenci@ara-raspberrypi2:ups-monitor/
ssh eduardocenci@ara-raspberrypi2 "cd ~/ups-monitor && docker compose up -d --build"
```

First install only (on the Pi):

```bash
sudo mkdir -p /var/lib/ups-monitor && sudo chown 1000:1000 /var/lib/ups-monitor
sudo install -m 644 99-ecoflow-ups.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=3746
( umask 077; sed -n -E 's/^ALERT_(WAHA_(URL|KEY|SESSION))=/\1=/p' ~/canteiro-timelapse/env/alerts.env > ~/ups-monitor/ups-monitor.env )
```

## Check

```bash
curl -s http://ara-raspberrypi2:8795/api/status     # state grid|battery|returning|no_comm + reading
curl -s http://ara-raspberrypi2:8795/api/stats      # ativações 24h/7d/30d/all, recent episodes, messages
curl -s -X POST http://ara-raspberrypi2:8795/api/test   # one "[teste]" message to Casa SmokeTests
ssh eduardocenci@ara-raspberrypi2 "docker logs --tail 50 ups-monitor"
```

The HID interface has one owner at a time. To use the CLI
(`python3 ecoflow_hid.py --raw`, as a user in group `dialout`), stop the
container first, or just read `/api/status`. The reader never sends
`SET_REPORT`, so it cannot change the beeper, the shutdown delays or any other
setting of the station.

## Not verified yet

Not tested with a real outage (or with the station's plug pulled). On grid the
status is `0x000c`; the *AC present* bit (`0x0004`) has not been seen cleared
and *discharging* (`0x0002`) has not been seen set. After the first real *queda
de energia* — or a deliberate 1-minute unplug of the station — check
`/api/events` and `/api/history?hours=2` and record here what the station
reported. Only then switch `MODE` to `live`.
