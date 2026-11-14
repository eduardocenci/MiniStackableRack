# ups-monitor — nobreak (UPS) do rack bnu → alertas de falta de energia no WhatsApp

The bnu rack **nobreak** (UPS) is plugged into **bnu-raspberrypi** through a
USB-serial adapter. The `ups-monitor` container reads it every 2 s and sends
**one message to "Casa Blumenau"** (the bnu group) in each case:

- 🔴 **falta de energia**: the UPS has been on battery for 10 s.
- 🟠 **bateria abaixo de 20 %** during the outage.
- ✅ **energia voltou**: the grid has been back for 10 s.

Every transfer to battery (*ativação*) is counted. The **Nobreak (UPS)** card on
globalnet shows the counters for 24 h / 7 d / 30 d, the live state, and the
recent episodes and messages in its tooltip (`GET /api/ups`). Decisão Eduardo
11/11/2026. Live since **2026-11-11 13:27 BRT** in `MODE=live`.

Runbook (rule, sample messages, operate, failure modes): globalnet
`docs/runbooks/ups-monitor.md`.

## The UPS

| | |
|---|---|
| UPS | **SMS Premium 1500 VA, bivolt** (SMS / Legrand). It reports model `PRM1500BiBiW`, firmware `1.3` |
| Reading the model string | `PRM` = Premium line, `1500` = 1500 VA, `BiBi` = bivolt in / out. Matches retail *Nobreak SMS Premium 1500VA, 8 tomadas, WiFi, Bivolt — cód. 0029501* (≈1050 W at PF 0.7, output selectable 115/220 V). The trailing `W` is most likely the Wi-Fi variant. This is inferred from the string, not read off the label |
| Rating (`F` reply) | `EBiS115` = *entrada* bivolt, *saída* 115 V · battery **24 V** (2 × 12 V) · **60 Hz** |
| Link to the Pi | Prolific **PL2303** USB-serial adapter (`067b:23a3`, serial `BHAFb149907`), plugged in on 2026-10-28. `/dev/ttyUSB0` = `/dev/serial/by-id/usb-Prolific_Technology_Inc._USB-Serial_Controller_BHAFb149907-if00-port0` (`root:dialout`). ModemManager 1.24 runs on this Pi but does not probe the adapter (strict filter policy), so no udev rule is needed |
| Protocol | SMS Brazil "mono" protocol, 2400 8N1, the one NUT's **`sms_ser`** driver implements. Megatec/Q1 queries get no answer |
| Second interface | SMS **"UPS Wi-Fi"** module at `10.1.1.159` (MAC `14:33:5c:1e:df:04`, web UI only). Not used: its credentials are unknown (`admin`/`admin` is refused) |

Snapshot on 2026-11-11 (on grid): input 220–223 V · output 115–116 V / 60 Hz ·
load 15 % · battery 100 % · internal 48 °C · status byte `0x39`.

## Files

| File | What |
|---|---|
| [`sms_serial.py`](sms_serial.py) | Read-only protocol: sends **only** `Q` (status), `I` (identification) and `F` (rating). Also a one-shot CLI |
| [`ups_monitor.py`](ups_monitor.py) | The rule (written out in its header), SQLite store, WhatsApp outbox and HTTP API |
| [`test_ups_monitor.py`](test_ups_monitor.py) | Offline tests: real captured frames plus simulated outages (blip, long outage, flapping grid, low battery, self-test, brownout, comm loss, restart mid-outage, outbox retry/order). Run with `python -m pytest -q test_ups_monitor.py` |
| [`Dockerfile`](Dockerfile), [`compose.yml`](compose.yml) | `python:3.11-slim`, stdlib only. Host network, user 1000 + group `dialout`, `/dev` bind-mounted with cgroup rule `c 188:*` (survives a re-plug), host zoneinfo |

Live copy: `bnu-raspberrypi:~/ups-monitor/`. State: `/var/lib/ups-monitor/ups.db`
(owned by 1000), with tables `episodes`, `outbox`, and `samples` (kept 90 days).

## Configuration

| Env (compose.yml) | Value |
|---|---|
| `MODE` | `live`. `shadow` = Casa SmokeTests with a `[sombra]` prefix; `log` = send nothing |
| `ALERT_JID` | Casa Blumenau `120363428209351902@g.us` (= root `.env` `BNU_WHATSAPP_GROUP_JID`) |
| `TEST_JID` | Casa SmokeTests `120363410899542847@g.us` (= `SMOKETESTS_WHATSAPP_GROUP_JID`), only for `POST /api/test` |
| `WAHA_URL/KEY/SESSION` | from `env_file` = `~/canteiro-jobs/env/canteiro-watchdog.env`, the bnu WAHA credentials already on this Pi (mirror of `.env` `BNU_WAHA_*`). That file's other keys are ignored. To decouple, copy the three keys to `~/ups-monitor/ups-monitor.env` and repoint `env_file` |
| `POLL_S` · `ALERT_AFTER_S` · `RESTORE_CONFIRM_S` · `LOW_BATTERY_PCT` | 2 · 10 · 10 · 20. `INPUT_MIN_V` defaults to 80 |
| `TZ` | `America/Sao_Paulo` (the Pi itself runs Europe/London) |

## Deploy / update

```bash
# from the repo root (Git Bash: MSYS_NO_PATHCONV=1, or /home/... gets rewritten)
for f in sms_serial.py ups_monitor.py Dockerfile compose.yml; do
  MSYS_NO_PATHCONV=1 python scripts/devtool.py push bnu-raspberrypi \
    scripts/raspberry-pi/bnu-raspberrypi/docker/ups-monitor/$f /home/eduardocenci/ups-monitor/$f
done
python scripts/devtool.py run bnu-raspberrypi "cd ~/ups-monitor && docker compose up -d --build"
```

First install only: `sudo mkdir -p /var/lib/ups-monitor && sudo chown 1000:1000 /var/lib/ups-monitor`.

## Check

```bash
curl -s http://bnu-raspberrypi:8795/api/status     # state grid|battery|returning|no_comm + reading
curl -s http://bnu-raspberrypi:8795/api/stats      # ativações 24h/7d/30d/all, recent episodes, messages
curl -s -X POST http://bnu-raspberrypi:8795/api/test   # one "[teste]" message to Casa SmokeTests
python scripts/devtool.py run bnu-raspberrypi "docker logs --tail 50 ups-monitor"
```

The port takes one reader at a time. To use the CLI (`python3 sms_serial.py
--raw`), stop the container first, or just read `/api/status`. The protocol also
has `T`/`L` (battery test), `M` (beeper), `S`/`R` (shutdown / restore) and
`C`/`D` (cancel). They change the UPS state and are deliberately not
implemented.

## Not verified yet

Not tested with a real outage. On grid the status byte is `0x39`
(*ups_ok · boost · bypass · beep*), and the *on battery* bit (`0x80`) has not
been seen set. The rule therefore also triggers on input < 80 V. After the first
real *queda de energia*, check `/api/events` and `/api/history?hours=2` and
record what the UPS reported here.
