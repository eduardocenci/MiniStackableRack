# canteiro-painel — live data band under the canteiro mosaic

The canteiro mosaic `[fixed lens | PT lens]` is 32:9; a TV is 16:9. This service draws the
other half — a **1920×540 PNG with five blocks, redrawn every 60 s** — shown under the
mosaic **on the office TV and on the `/live` page** (pedido Eduardo 15/11/2026).

| Block | Source | Refresh |
|---|---|---|
| NA REDE AGORA — in large type, how many distinct people's devices were seen on the site Wi-Fi in the last hour (online now or gone < 1 h ago); in small type under it, how many are on the network right now and how many distinct devices were seen over the whole day ("3 agora, 7 únicos hoje" — pedido Eduardo 18/11/2026, wording and count-only of 20/11/2026: the devices themselves are not listed on the card, only in `dados.rede.unicos` of `/painel.json` — nome, tag, ts, on); behind the text, across the whole card, the day's curve of that last-hour count over the whole day (X axis 00:00–24:00, a label every 3 h — pedido Eduardo 23/11/2026; until then only the working hours, 07:00–18:00; Y axis with a single tick — the day's peak, a dashed line labelled "máx. N" at the right; one point per 5 min kept in `STATE_DIR/rede-serie.json`; it starts when the container first reads the network that day — no backfill) (fixed gear is named only when one is down), and "Hoje:" — one pill per tag seen today, the tag being what the device nickname in netoverview carries in its last parentheses (`moto-g14 (Ariane)` → Ariane): lit (green) while one of its devices was on the network in the last hour, greyed out when it was there earlier today; devices without a tag are counted as "+ N sem tag". To tag a device, rename it in the ara netoverview. netoverview drops an offline device older than 30 days from `/api/devices`, so the panel keeps the day's sightings itself in `STATE_DIR/rede-hoje.json` (it sees every device while online, one read a minute) (top of column 1) | ara netoverview `GET /api/devices` | 60 s |
| PORTÃO — TAGS — last gate passes (bottom of column 1) | `condfy.db` (bnu docker LXC 101) through `bnu-proxmox`, paramiko — the path `canteiro-diario` uses | 1 min (the bridge itself polls Condfy every 60 s; was 3 min until 2026-11-15) |
| VENCIMENTOS — titles falling due in the next 7 days ("A VENCER") and the ones overdue up to 7 days still open ("VENCIDOS SEM BAIXA"), each `agendado DD/MM` or `sem agendamento`; older open ones as one total line. **Who gets the visible rows** (pedido Eduardo 18/11/2026): first the titles FALLING DUE with no payment scheduled — item by item (vendor, due date, value, amber `sem agendamento`), soonest first, with the count on the section line ("A VENCER · 3 SEM AGENDAMENTO"); then the overdue ones with none (red), most recent first; a scheduled title (green) only in the room that is left. What does not fit is one "+ N · R$" line per section — amber/red when some of the hidden ones have no payment scheduled ("+ 7 (6 sem agend.)"), grey when all of them are scheduled. Until 18/11/2026 the overdue ones came first and took every row, leaving "A VENCER" as a single summary line. **Pay or not** (pedido Eduardo 24/11/2026): under each title FALLING DUE with no payment scheduled, a guidance line — green `PAGAR`, red `NÃO PAGAR` or grey-outlined `A CONFERIR` (nobody checked it yet) — followed by what the title is about, in up to 3 lines (~105 characters); when the card is full the guidance gives up lines, one at a time, before an open title is left out. The text is the mark `[pagar: sim — …]` / `[pagar: não — …]` that the /atualiza payment scrutiny writes at the start of Observações (home-ara `finance.sheets_client.set_pagar`); the panel only reads it. A title due on a Saturday or Sunday stays under "A VENCER" until the next working day (regra Eduardo 24/11/2026: paying on the next working day is fine; holidays are not known to the panel) | sheet tab `Financeiro` (same spreadsheet as the plan): open = Pago (U) not starting with "Sim"; scheduled = `Agend. (IA), DD/MM/YYYY` there; due date = column Data (K) on retention guias and parcels without NF, `venc. DD/MM/AAAA` in Observações on invoiced rows (none → skipped, not guessed) | 15 min |
| FORNECEDORES NA OBRA (was ENTREGAS until 2026-11-23) · PREVISTO + CONCLUÍDO RECENTEMENTE — what the last Diário de Obra expected (hollow dot; `part` amber, `no` red; a "não esperada" label becomes the when - "semana 21" / "sem data") and what the last `DIARIO_N` (3) diaries recorded as concluded (green dot, each with its day). "Concluded" = status `ok` or a label like "entregue…", "executada…", "no canteiro" (`RECEBIDO_RE`) | Drive `Diario/<date>/diario.json` (rclone; public copy, no `equipe`) | 30 min |
| FORNECEDORES NA OBRA · PRESENTE HOJE — who came today, before tonight's Diário, **one row per happening** (pedido Eduardo 23/11/2026: a truck and the item it fulfilled used to show twice). A truck Claudinho identified with confidence ≥ 60 % (`HOJE_CONF`) gets a **checked blue box**; when its contract CON-NNN has ONE compatible expected item (`_compat`: a "retirada" truck never closes an "Entrega de…" item) that item leaves PREVISTO and the row takes its words ("… · no previsto"), otherwise "… · fora do previsto". Right side = the camera's times ("10:19 → 10:24", "no canteiro desde 08:10", "09:40 · saída não vista"). Below 60 %: empty amber box, "provável NN %", nothing moves; no answer: empty grey box, "não identificado". A tag on the site Wi-Fi with no truck of that company is a row too ("N aparelhos no Wi-Fi · sem caminhão", `REDE_AVISO_FORA` never shows). **Checked = present, not done** — evidence, never state; green comes only from a Diário, which replaces the section at night | obra group through `waha-listener` `GET /messages` (`LISTENER_URL`/`LISTENER_TOKEN`/`OBRA_CHAT_JID`, already in the container's env): the 🚚/🚧 alerts of `canteiro-alertas` and the 🫡 answer of Claudinho that follows each (≤ 15 min) — `ALERTA_RE`, `CLAUD_RE`, `SAIU_RE`. Test a past day: `PAINEL_HOJE=2026-11-09` | 60 s |
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

### Últimas detecções — the thumbnails over the `/live` video (pedido Eduardo 23/11/2026)

Same port, same `PAINEL_ALLOW`. A thread of its own asks the bnu Frigate (`FRIGATE_URL`, already
in the env) every 15 s (`DET_S`) for the review items of each lens (`DET_CAMERAS`, default
`canteiro_fixa,canteiro` = left | right of the mosaic) and keeps the last 3 (`DET_N`) ENDED ones
with Frigate's own preview GIF (`/api/review/<id>/preview`, 240×180, 8 fps) in memory. Eduardo's
rules of 23/11/2026: no filter by duration, no expiry — always the last three, however old.

| Route | What |
|---|---|
| `/det.json` | `{ts, erro, cams: {<camera>: [{id, start, end, sev, objetos}]}}`, newest first; `erro` = the last Frigate failure (the last good list stays) |
| `/det/<id>.gif` | the preview GIF of a LISTED item (anything else = 404) |
| `/det/<id>.mp4` | the recording of that stretch (`/api/<camera>/start/…/end/…/clip.mp4`, ± 2 s), fetched on the first click and kept in `STATE_DIR/det-clips/` (last 6, ≤ 120 MB each) — Frigate streams it chunked with no `Range`, so it is served from the file, with `Range` |

Check: `curl -s 127.0.0.1:8557/det.json` on the Pi.

**For the TV** (same day, phase 2): `mosaico-tv` is a video, so the thumbnails are burned in by
`canteiro-mosaico` (bnu Frigate LXC) — and drawn here, where PIL is, so the composer only copies
bytes:

| Route | What |
|---|---|
| `/det/tv.json` | `{w: 172, h: 96, fps: 8, slots: [key \| null × 6]}` — slots 0–2 = left lens (`canteiro_fixa`), 3–5 = right lens; `key` = `<id>.<crc of the age text>` |
| `/det/tv/<key>.yuv` | that item as a loop of raw I420 frames 172×96 (≤ 120, `DET_TV_MAX_FRAMES`) with everything on it: border by severity, "3m atrás", type icon (person, car, bus, bicycle, motorcycle). Fetched again only when the age text changes |

The composer polls `/det/tv.json` every 15 s. Look at one loop:
`ffmpeg -f rawvideo -pix_fmt yuv420p -video_size 172x96 -i loop.yuv -frames:v 1 x.png`.

## Consumers

- **`canteiro-mosaico`** (bnu Frigate LXC 105,
  [`../../../proxmox/frigate/bnu-frigate/canteiro-mosaico.sh`](../../../proxmox/frigate/bnu-frigate/canteiro-mosaico.sh)):
  fetches the PNG every 30 s and stacks it under the TV output → `mosaico-tv` 1920×1080.
- **`/live`** ([`../docker/go2rtc/canteiro-live.html`](../docker/go2rtc/canteiro-live.html)):
  `tailscale serve --bg --set-path=/painel http://127.0.0.1:8557` (tailnet only); the page
  shows `/painel/painel.png` under the 2560×720 video, reloaded every 30 s.
  `…/live?painel=0` = video only. Since 2026-11-23 it also lays the last three detections of
  each lens over the video, at the bottom right of that lens (`/painel/det.json` every 15 s,
  `/painel/det/<id>.gif`; red border = alert, amber = detection; a click plays
  `/painel/det/<id>.mp4`, with a "Baixar clip" button at the top right that saves that file as
  `canteiro-<fixa|pt>-AAAAMMDD-HHMMSS.mp4`). `…/live?det=0` = without them. Since 2026-11-24 the mouse over a
  title of the VENCIMENTOS card (a tap on a phone) shows the whole note of the sheet —
  `/painel/zonas.json` (fetched with each PNG) lists the rectangles of the current picture, in
  the PNG's own pixels, with the title, the full guidance and the Observações text. The TV has
  no hover: there the guidance line is all there is.

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

