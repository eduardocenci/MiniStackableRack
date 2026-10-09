# canteiro-alertas — truck alerts from the canteiro camera to WhatsApp

Phase 2 of home-ara **decision 0010** (`homes/ara/decisions/0010-decisions-api-canteiro.md`):
the canteiro camera asks the OpenAI **Decisions API** about every vehicle that
Frigate sees, and posts one alert per truck — snapshot + caption — to the
WhatsApp group **"Casa Céu Azul"** (decisão Eduardo 09/10/2026, after the
backtest: 0 false alerts and 92% correct truck type in 11 camera days; ~1 in 4
trucks does not alert, mostly because Frigate never produced a usable event).

```
Frigate (bnu LXC 105) ──/api/events──▶ canteiro-alertas (bnu-raspberrypi, container)
   camera "canteiro"      snapshot.jpg     │  ≥ 10 s vehicle event
                                            ├─▶ OpenAI /v1/decisions (V2 questions, gpt-6-luna)
                                            ├─▶ canteiro.alertas.Motor (episodes, quiet hours, caps)
                                            └─▶ WAHA sendImage (LXC 101) ──▶ "Casa Céu Azul" / SmokeTests
```

| | |
|---|---|
| container | `canteiro-alertas` (image `canteiro-jobs:local`, see [`../docker/canteiro-jobs/`](../docker/canteiro-jobs/)) |
| logic | `homes/ara/canteiro/` (alertas, decisions, questions, metrics, datasets) — copied into the image; CI-tested in home-ara |
| state | `/var/lib/canteiro-alertas/` — `state.json` (seen event ids, open episode, counters; a restart never re-alerts), `alertas.jsonl` (one line per decision), `heartbeat.json`, `cache/` (every Decisions answer — audit trail) |
| env | `~/canteiro-jobs/env/canteiro-alertas.env` (600) — template [`canteiro-alertas.env.example`](canteiro-alertas.env.example) |
| cost | ~US$ 0,0001 per vehicle event (~1.300 input tokens); `BUDGET_USD_DAY` caps it (default US$ 0,30) |

## Rules (from the backtest — details in decision 0010)

- Trigger: vehicle event (`car`/`bus`/`motorcycle`) ≥ 10 s; alert when P(truck) ≥ 0,85;
  the type is the one of the first event (munck · caçamba · carroceria · prancha).
- One episode per truck of ANY type (IoU ≥ 0,3 or ≤ 10 min since last sighting; closes
  after 20 min) — the same truck flipping munck/carroceria between events is one alert.
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
off during the two-week learning window (decisão Eduardo 09/10/2026).

## Modes — each step only with Eduardo's OK

| `MODE` | sends to |
|---|---|
| `log` | nothing — decisions in `alertas.jsonl` (first deploy) |
| `shadow` | Casa SmokeTests, caption prefixed "👁️ SOMBRA (iria ao Casa Céu Azul)" |
| `live` | "Casa Céu Azul" (`GROUP_JID` = `ARA_ALERTAS_GROUP_JID` = `ARA_WHATSAPP_GROUP_JID`) |

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
