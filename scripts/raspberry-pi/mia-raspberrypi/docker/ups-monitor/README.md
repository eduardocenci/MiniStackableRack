# ups-monitor (mia) — rack nobreak → WhatsApp + globalnet card

Same service as the bnu and ara `ups-monitor` (same rule, outbox and HTTP API on
`:8795`), for the mia rack nobreak — a **CyberPower CP1000PFCLCD** connected to
`mia-raspberrypi` on 26/11/2026 (pedido Eduardo). The nobreak feeds a TP-Link Tapo
P316M power strip, and through it essentially every device of the site. What differs: it does not touch the
USB; it reads the UPS from the **`nut-upsd`** container of the same Pi
(`../nut-upsd/`, `127.0.0.1:3493`) with `nut_client.py`, read-only (`LIST VAR`).

| | |
|---|---|
| Mode | **`live`** since 26/11/2026 — messages go to the WhatsApp group **Casa Miami** (`MIA_WHATSAPP_GROUP_JID`, created by Eduardo that day). It ran in `shadow` (Casa SmokeTests, `[sombra]`) for its first hours |
| Poll | `POLL_S=1` (nut-upsd itself polls the UPS every 1 s) |
| Readings | input/output V, load % and estimated watts (`load_w` = % × 600 W), battery %, runtime (the UPS's estimate), battery V. No temperature |
| Outage | `ups.status` contains `OB` and no self-test is running, or input < 80 V |
| Messages | `MIA — Falta de energia` · `Bateria do nobreak abaixo de 20 %` · `Energia voltou`; clock America/New_York |
| State | `/var/lib/ups-monitor/ups.db` on the Pi |

Not verified yet: a real outage (the UPS has only been seen `OL`).

## Operate

```bash
curl -s http://mia-raspberrypi:8795/api/status     # live state
curl -s http://mia-raspberrypi:8795/api/events     # episodes and messages
curl -s -X POST http://mia-raspberrypi:8795/api/test   # one "[teste]" to Casa SmokeTests
python scripts/devtool.py run mia-raspberrypi "docker logs --tail 30 ups-monitor"
```

## First install

`ups-monitor.env` (chmod 600, not in git) on the Pi, next to `compose.yml`:

```
WAHA_URL=http://bnu-proxmox:3001
WAHA_KEY=<root .env BNU_WAHA_API_KEY>
WAHA_SESSION=<root .env BNU_WAHA_SESSION>
```

```bash
python scripts/devtool.py run mia-raspberrypi "sudo mkdir -p /var/lib/ups-monitor && sudo chown 1000:1000 /var/lib/ups-monitor"
# push Dockerfile compose.yml nut_client.py ups_monitor.py to ~/ups-monitor/ (MSYS_NO_PATHCONV=1), then
python scripts/devtool.py run mia-raspberrypi "cd ~/ups-monitor && docker compose up -d --build"
```

Tests (offline): `python -m pytest -q test_ups_monitor.py` in this folder.

Runbook: globalnet `docs/runbooks/ups-monitor.md` (section *mia*).
