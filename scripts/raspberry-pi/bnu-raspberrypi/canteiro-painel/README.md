# canteiro-painel — live data band under the canteiro mosaic

The canteiro mosaic `[fixed lens | PT lens]` is 32:9; a TV is 16:9. This service draws the
other half — a **1920×540 PNG with five blocks, redrawn every 60 s** — shown under the
mosaic **on the office TV and on the `/live` page** (pedido Eduardo 15/11/2026).

| Block | Source | Refresh |
|---|---|---|
| NA REDE AGORA — people's devices on the site Wi-Fi, fixed gear, last to leave (top of column 1) | ara netoverview `GET /api/devices` | 60 s |
| PORTÃO — TAGS — last gate passes (bottom of column 1) | `condfy.db` (bnu docker LXC 101) through `bnu-proxmox`, paramiko — the path `canteiro-diario` uses | 3 min |
| VENCIMENTOS — titles falling due in the next 7 days ("A VENCER") and the ones overdue up to 7 days still open ("VENCIDOS SEM BAIXA"), each `agendado DD/MM` or `sem agendamento`; older open ones as one total line | sheet tab `Financeiro` (same spreadsheet as the plan): open = Pago (U) not starting with "Sim"; scheduled = `Agend. (IA), DD/MM/YYYY` there; due date = column Data (K) on retention guias and parcels without NF, `venc. DD/MM/AAAA` in Observações on invoiced rows (none → skipped, not guessed) | 15 min |
| ENTREGAS — deliveries of the last Diário de Obra | Drive `Diario/<date>/diario.json` (rclone; public copy, no `equipe`) | 30 min |
| PLANO DA SEMANA — planned activities, next week, status dots | sheet `PlanejadoRealizado` (gspread) + the Diário's `plano` when it is of the same week (fuzzy match: the Diário rewrites the sheet's wording) | 30 min |

"Expected teams" has no structured source yet (the Diário only carries them as prose), so
the panel shows who IS on site (Wi-Fi + gate) rather than who is expected.

Evidence, never state: read-only. A failing source keeps its last good data; the header
turns amber ("dado de HH:MM") when it is stale, red ("sem dados") if it never loaded.

## Where it runs

Container `canteiro-painel` on bnu-raspberrypi — image `canteiro-jobs:local`, service in
[`../docker/canteiro-jobs/compose.yml`](../docker/canteiro-jobs/compose.yml), env
`env/canteiro-diario.env` (same sources as the Diário), state `/var/lib/canteiro-painel`
(last PNG).

HTTP `:8557`: `/painel.png` and `/painel.json` only for `PAINEL_ALLOW`
(`127.0.0.1,10.1.1.160`); `/health` for anyone (no names — it feeds the globalnet LED).

## Consumers

- **`canteiro-mosaico`** (bnu Frigate LXC 105,
  [`../../../proxmox/frigate/bnu-frigate/canteiro-mosaico.sh`](../../../proxmox/frigate/bnu-frigate/canteiro-mosaico.sh)):
  fetches the PNG every 30 s and stacks it under the TV output → `mosaico-tv` 1920×1080.
- **`/live`** ([`../docker/go2rtc/canteiro-live.html`](../docker/go2rtc/canteiro-live.html)):
  `tailscale serve --bg --set-path=/painel http://127.0.0.1:8557` (tailnet only); the page
  shows `/painel/painel.png` under the 2560×720 video, reloaded every 30 s.
  `…/live?painel=0` = video only.

## Deploy / test

```bash
MSYS_NO_PATHCONV=1 python scripts/devtool.py push bnu-raspberrypi   scripts/raspberry-pi/bnu-raspberrypi/canteiro-painel/canteiro-painel.py canteiro-jobs/canteiro-painel.py
python scripts/devtool.py run bnu-raspberrypi   "cd ~/canteiro-jobs && docker compose build canteiro-watchdog && docker compose up -d canteiro-painel"
# one picture without touching the service (prints which sources answered):
python scripts/devtool.py run bnu-raspberrypi   "docker exec -e STATE_DIR=/tmp/p canteiro-painel python3 /app/canteiro-painel.py --once /tmp/painel.png"
python scripts/devtool.py run bnu-raspberrypi "curl -s 127.0.0.1:8557/health"
```

First install only: `sudo install -d -o 1000 -g 1000 /var/lib/canteiro-painel`.

## Privacy

Device names, who passed the gate and what is owed to whom: home LAN (two clients) and tailnet only — never send
this picture to a WhatsApp group.

Runbook: globalnet `docs/runbooks/canteiro-painel.md`.
