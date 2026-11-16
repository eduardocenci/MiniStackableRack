# canteiro-painel — live data band under the canteiro mosaic

The canteiro mosaic `[fixed lens | PT lens]` is 32:9; a TV is 16:9. This service draws the
other half — a **1920×540 PNG with five blocks, redrawn every 60 s** — shown under the
mosaic **on the office TV and on the `/live` page** (pedido Eduardo 15/11/2026).

| Block | Source | Refresh |
|---|---|---|
| NA REDE AGORA — in large type, how many distinct people's devices were seen on the site Wi-Fi in the last hour (online now or gone < 1 h ago); in small type under it, how many are on the network right now; behind the text, across the whole card, the day's curve of that last-hour count over the working hours (X axis 07:00–18:00, one point per 5 min kept in `STATE_DIR/rede-serie.json`; it starts when the container first reads the network that day — no backfill) (fixed gear is named only when one is down), and "Hoje:" — one pill per tag seen today, the tag being what the device nickname in netoverview carries in its last parentheses (`moto-g14 (Ariane)` → Ariane): lit (green) while one of its devices was on the network in the last hour, greyed out when it was there earlier today; devices without a tag are counted as "+ N sem tag". To tag a device, rename it in the ara netoverview. netoverview drops an offline device older than 30 days from `/api/devices`, so the panel keeps the day's sightings itself in `STATE_DIR/rede-hoje.json` (it sees every device while online, one read a minute) (top of column 1) | ara netoverview `GET /api/devices` | 60 s |
| PORTÃO — TAGS — last gate passes (bottom of column 1) | `condfy.db` (bnu docker LXC 101) through `bnu-proxmox`, paramiko — the path `canteiro-diario` uses | 1 min (the bridge itself polls Condfy every 60 s; was 3 min until 2026-11-15) |
| VENCIMENTOS — titles falling due in the next 7 days ("A VENCER") and the ones overdue up to 7 days still open ("VENCIDOS SEM BAIXA"), each `agendado DD/MM` or `sem agendamento`; older open ones as one total line | sheet tab `Financeiro` (same spreadsheet as the plan): open = Pago (U) not starting with "Sim"; scheduled = `Agend. (IA), DD/MM/YYYY` there; due date = column Data (K) on retention guias and parcels without NF, `venc. DD/MM/AAAA` in Observações on invoiced rows (none → skipped, not guessed) | 15 min |
| ENTREGAS — deliveries of the last Diário de Obra, split into A RECEBER (top) and JÁ RECEBIDOS (bottom, green dot); the text of each item wraps to as many lines as the card has room for (up to 4). The Diário marks most deliveries `na` and says it in words, so "received" = status `ok` or a label like "entregue…", "executada…", "no canteiro" (`RECEBIDO_RE`) | Drive `Diario/<date>/diario.json` (rclone; public copy, no `equipe`) | 30 min |
| ENTREGAS · HOJE — the trucks of the current day, before tonight's Diário: one row per contract/company (arrival time, company, what, `saiu HH:MM`), blue = seen by the camera. Shown only while the Diário on screen is older than that day. **Automatic baixa**: a truck Claudinho identified as an `entrega` of contract CON-NNN with confidence ≥ 60 % (`HOJE_CONF`) moves the A RECEBER item of that contract to JÁ RECEBIDOS with a blue "chegou hoje HH:MM"; below 60 % the row says "provável NN %" and nothing moves; a truck with no answer is "não identificado". Evidence, never state — the Diário replaces it at night | obra group through `waha-listener` `GET /messages` (`LISTENER_URL`/`LISTENER_TOKEN`/`OBRA_CHAT_JID`, already in the container's env): the 🚚/🚧 alerts of `canteiro-alertas` and the 🫡 answer of Claudinho that follows each (≤ 15 min) — `ALERTA_RE`, `CLAUD_RE`, `SAIU_RE`. Test a past day: `PAINEL_HOJE=2026-11-09` | 60 s |
| PLANO DA SEMANA — planned activities, next week, status dots. When Ênio has filled the "Realizado" column it decides: a line he STRUCK THROUGH there was not executed (red ring, text struck), an unstruck one was (green), and what he added beyond the plan is listed in full under "ALÉM DO PLANO — N FEITOS" (green +), which has priority over next week's items — those only get the room that is left; the Diário's marks only fill what he left out. The strikethrough is formatting — read with `fetch_sheet_metadata(includeGridData, textFormatRuns)`, never `get_all_values()` (until 2026-11-15 every struck line read as done) | sheet `PlanejadoRealizado` (gspread) + the Diário's `plano` when it is of the same week (fuzzy match: the Diário rewrites the sheet's wording) | 30 min |

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

## Supplier-on-the-network notice (added 2026-11-15)

Besides drawing, the panel sends ONE WhatsApp message the first time a tag shows up on the site
Wi-Fi in the day (a device of that tag online now, tag not announced today):

    📶 *IBEV no canteiro* — 10:05
    _Aviso automático · 2 aparelhos da IBEV na rede Wi-Fi da obra_

- `REDE_AVISO` (compose): `off` · `teste` (to `TEST_JID`, Casa SmokeTests) · `live` (to
  `OBRA_CHAT_JID`, the obra group "Cenci Céu Azul Casa-Hangar"). `live` since 2026-11-15
  (Eduardo: "ligar sem a Ariane").
- `REDE_AVISO_FORA` (default `Eduardo,Jorge,Ênio,Ariane`): tags that never announce — owners, manager,
  and the daily crew (Ariane is on the Wi-Fi every morning at ~06:40; a notice a day is noise).
- Once per tag per day: the announced tags live in `STATE_DIR/rede-hoje.json` (`avisados`), so a
  restart does not repeat; a send that fails is retried on the next read (60 s).
- A tag is the text in the last parentheses of the device nickname in the ara netoverview —
  renaming a device there is all it takes to make it announce.
- Sent through WAHA `sendText` (`WAHA_URL`/`WAHA_KEY`/`WAHA_SESSION` from the env file).

