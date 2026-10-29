# canteiro-alertas — truck alerts from the canteiro camera to WhatsApp

Phase 2 of home-ara **decision 0010** (`homes/ara/decisions/0010-decisions-api-canteiro.md`):
the canteiro camera asks the OpenAI **Decisions API** about every vehicle that
Frigate sees, and posts one alert per truck — snapshot + caption — to the obra
WhatsApp group **"Cenci Céu Azul Casa-Hangar"** (decisão Eduardo 13/10/2026; from
09/10 to 13/10 they went to "Casa Céu Azul", after the backtest: 0 false alerts
and 92% correct truck type in 11 camera days; ~1 in 4 trucks does not alert,
mostly because Frigate never produced a usable event).

```
Frigate (bnu LXC 105) ──/api/events──▶ canteiro-alertas (bnu-raspberrypi, container)
   camera "canteiro"      snapshot.jpg     │  ≥ 10 s vehicle event
                                            ├─▶ OpenAI /v1/decisions (V2 questions, gpt-6-luna)
                                            ├─▶ canteiro.alertas.Motor (episodes, quiet hours, caps)
                                            └─▶ WAHA sendImage (LXC 101) ──▶ obra group / SmokeTests
```

| | |
|---|---|
| container | `canteiro-alertas` (image `canteiro-jobs:local`, see [`../docker/canteiro-jobs/`](../docker/canteiro-jobs/)) |
| logic | `homes/ara/canteiro/` (alertas, decisions, questions, metrics, datasets) — copied into the image; CI-tested in home-ara |
| state | `/var/lib/canteiro-alertas/` — `state.json` (seen event ids, open episode, counters, today's alerted trucks; a restart never re-alerts), `alertas.jsonl` (one line per decision), `heartbeat.json`, `cache/` (every Decisions answer — audit trail), `caminhoes/` (snapshot of each alerted truck, the day's gallery) |
| env | `~/canteiro-jobs/env/canteiro-alertas.env` (600) — template [`canteiro-alertas.env.example`](canteiro-alertas.env.example) |
| cost | ~US$ 0,0001 per vehicle event (~1.300 input tokens); `BUDGET_USD_DAY` caps it (default US$ 0,30) |

## Rules (from the backtest — details in decision 0010)

- Trigger: vehicle event (`car`/`bus`/`motorcycle`) ≥ 10 s; alert when P(truck) ≥ 0,85;
  the type is the one of the first event (caçamba · munck · carroceria · prancha; betoneira/bomba
  = concretagem, off). **V3** adds *poliguindaste com caçamba (para retirar entulho)* but is
  EXPERIMENTAL: on the six 29/10/2026 snapshots every V3 variant pulled munck towards carroceria
  and never scored the poliguindaste above 0,14, while the Decisions API itself varies a lot
  between identical calls — V2 stays live (`QUESTIONS=v2`) until a V3 passes the backtest gate
  (home-ara decision 0010, adendo 29/10/2026).
- One episode per truck of ANY type (IoU ≥ 0,3 or ≤ 10 min since last sighting; closes
  after 20 min) — the same truck flipping munck/carroceria between events is one alert.
- **Gallery of the day's trucks** (decisão 0010 adendo 29/10/2026 — the same carroceria was
  alerted at 08:43 and 09:29 after the PT lens moved the box): before opening a new episode
  on a day that already had an alert, ONE Decisions call compares the new snapshot with the
  snapshots of the trucks alerted today (`STATE_DIR/caminhoes/<event>.jpg`, kept `CAM_DIAS`
  = 7 days). P(id) ≥ `P_MESMO_CAMINHAO` (0,8) → same truck, joins its episode silently
  (`alertas.jsonl` line: `galeria` + log "pela galeria"); anything else → normal alert.
- Never: team van, cars, machines; below threshold; 19:00–06:00 (log only).
- Caps: 6/hour, 15/day — one "alertas pausados" note to SmokeTests.
- OpenAI failure (key, IP, credits, network) = no alert, only a log line.
- Concrete pours (start + end summary): **off** (`POUR_ALERTS=0`) until a pour validates them.

## Vehicle registry (home-ara decision 0011, since 09/10/2026)

Every car/van that stays ≥ 20 s (not a truck) is **learned**: the service crops it
from the Frigate **recording** frame (2304×1296, ~10× the snapshot), asks the
Decisions API for descriptors (type, colour, make, sharpness) and "is it the SAME
vehicle?" against the reference crops, and files it under a stable `V-NNN`:

| file (`/var/lib/canteiro-alertas/veiculos/`) | |
|---|---|
| `veiculos.json` | operational registry — descriptors + reference ids, **no identity** (seeded from the home-ara backfill, same `V-NNN` sequence) |
| `refs/<event>.jpg` | high-res reference crops |
| `veiculos.jsonl` | one line per new visit (`veiculo`, `novo`, descriptors) |
| `abertas.json` | visits in progress |

Who a vehicle belongs to lives only in home-ara `docs/diario/veiculos.yaml`
(internal; gate tags, Wi-Fi, WhatsApp and diário context; owner only with Eduardo's
confirmation). `DESCONHECIDOS=1` turns on the "veículo não visto antes" alert —
off during the two-week learning window (decisão Eduardo 09/10/2026). When on, it
goes to `INTERNO_JID` ("Casa Céu Azul"), **never** to the obra group: the registry
is internal (an empty `INTERNO_JID` fails that send, it does not fall back).

## Modes — each step only with Eduardo's OK

| `MODE` | sends to |
|---|---|
| `log` | nothing — decisions in `alertas.jsonl` (first deploy) |
| `shadow` | Casa SmokeTests, caption prefixed "👁️ SOMBRA (iria ao grupo da obra)" |
| `live` | trucks (and pours, when on) → obra group "Cenci Céu Azul Casa-Hangar" (`GROUP_JID` = `ARA_FIN_GROUP_JID`, since 13/10/2026); unknown vehicle → "Casa Céu Azul" (`INTERNO_JID` = `ARA_ALERTAS_GROUP_JID`) |

Live posts in the obra group are transcribed by waha-listener like any other
message; home-ara `scripts/whatsapp.py` tags the `🚚` captions as `rotina`
(topic `#pi-canteiro`).

Change the mode: edit `MODE=` in the env file, then
`python scripts/devtool.py run bnu-raspberrypi "cd ~/canteiro-jobs && docker compose up -d canteiro-alertas"`.

## Checks

```bash
python scripts/devtool.py run bnu-raspberrypi "docker exec canteiro-alertas python3 /app/canteiro-alertas.py --selftest"
python scripts/devtool.py run bnu-raspberrypi "docker logs --tail 50 canteiro-alertas"
python scripts/devtool.py run bnu-raspberrypi "tail -5 /var/lib/canteiro-alertas/alertas.jsonl"
# sample alert with the latest vehicle snapshot — ALWAYS pass the SmokeTests JID explicitly
python scripts/devtool.py run bnu-raspberrypi "docker exec canteiro-alertas python3 /app/canteiro-alertas.py --test-alert <SMOKETESTS_JID>"
```

`--selftest` failing with `401 ip_not_authorized` = the Pi left bnu's allowlisted IP;
`429 insufficient_quota` = the OpenAI account (shared with bnu HA) is out of credits.

Replay of a past day through the same engine (on the PC, from the diário packs):
`cd homes/ara && python -m canteiro.alertas --replay 2026-09-09 [--to smoketests]`.
