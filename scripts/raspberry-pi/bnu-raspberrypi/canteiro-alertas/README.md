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
   cameras "canteiro" (PT)  recording      │  vehicle event ≥ 10 s (PT) / ≥ 1 s (fixed lens)
   + "canteiro_fixa" (fixed) frames        │  ONE episode per truck across both lenses
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
| consumers | **Claudinho 🫡** (home-ara decision 0016, since 30/10/2026): the waha-listener rule `caminhao` queues every truck alert this container posts in the obra group; mia-desktop (`python -m canteiro.claudinho loop`) answers it with the delivery/pickup it was, quoting the alert. It parses the caption's first line (`<emoji> *<label> no canteiro* — HH:MM (Nº do dia)`) — change that line or the labels (`canteiro.alertas.NOMES`) and `canteiro.claudinho.CLASSES` + the listener rule must follow (test `test_rotulos_batem_com_o_motor`) |

## Rules (from the backtest — details in decision 0010)

- Trigger: vehicle event (`car`/`bus`/`motorcycle`) ≥ 10 s; alert when P(truck) ≥ 0,85;
  the type is the one of the first event: caçamba · munck · poliguindaste com caçamba (para
  retirar entulho) · carroceria · prancha · **betoneira · bomba** (V3, 29/10/2026 — concrete
  trucks alert like any other truck; the pour start/end machine was retired).
- **What the API sees** (adendo 0010, 29/10/2026): up to `QUADROS` = 3 frames of the
  RECORDING (2304×1296) — the track point nearest the camera (`data.path_data`), +3 s and
  the middle of the event — best = highest P(truck); it stops early on a confident frame
  (`P_CONFIANTE`) or when the first frame is clearly a car (`P_CARRO`). The 640×480
  snapshot is used only when no recording frame exists. On the 27/10 pour day this took
  betoneira 08:33 from 0,26 to 0,99 and the bomba-lança leaving from 0,30 to 1,00; the
  poliguindaste class only works on these frames (0,71 vs ≤ 0,14 on the snapshot). The
  alert image is the classified frame, resized to `ENVIO_MAX_PX`. `alertas.jsonl` keeps
  `fonte` and `quadros` (what each frame answered).
- One episode per truck of ANY type (IoU ≥ 0,3 or ≤ 10 min since last sighting; closes
  after 20 min) — the same truck flipping munck/carroceria between events is one alert.
- **Two lenses, one alert (adendo 0010, 04/11/2026).** The iM9's FIXED lens is a second
  Frigate camera, `canteiro_fixa` (640×480 substream, 5 fps, `FRIGATE_CAMERAS=canteiro=10,
  canteiro_fixa=1` — 1 s since 08/11/2026, 3 s until then): Eduardo aimed it at the path vehicles take in, where many only pass the
  PT lens and park out of its angle (30/10/2026: a Versátil carroceria seen only by the
  timelapse, no alert). The two streams overlap a lot, so the engine (`Motor`, rule 2c) joins
  by time across lenses (≤ 10 min since the episode's last sighting) but compares IoU only
  within the SAME lens (one box per lens in `state.json` `ep.boxes`); 10–20 min apart on the
  other lens → the day's gallery decides (its references come from both lenses and its
  context says so). Recording frames of the fixed lens are 2304×1296 since 2026-11-05 (its
  HEVC main is Frigate's record feed; on 2026-11-04 they were 640×480 — see
  `../docker/go2rtc/README.md`, "Two main streams"); short events classify at the nearest
  point, +1 s and the middle. The vehicle registry (0011) stays on the PT lens only.
  `alertas.jsonl` lines carry `camera`.
- **Gallery of the day's trucks** (decisão 0010 adendo 29/10/2026 — the same carroceria was
  alerted at 08:43 and 09:29 after the PT lens moved the box): before opening a new episode
  on a day that already had an alert, ONE Decisions call compares the new snapshot with the
  snapshots of the trucks alerted today (`STATE_DIR/caminhoes/<event>.jpg`, kept `CAM_DIAS`
  = 7 days). P(id) ≥ `P_MESMO_CAMINHAO` (0,8) → same truck, joins its episode silently
  (`alertas.jsonl` line: `galeria` + log "pela galeria"); anything else → normal alert.
  Since 09/11/2026 the MASS counts too: P(nenhum) ≤ 1 − `P_MESMO_CAMINHAO` = one of today's
  trucks (the most probable reference) — at 13:05 on 09/11 the answer was 0,37 + 0,37 + 0,22
  across three references of the SAME truck and "nenhum" ≈ 0,04, and the rule still said "new".
- **Arrival and departure through the ENTRANCE lens — "first and last"** (adendo 0010,
  09/11/2026: the Marcondes carroceria stayed 1h45 and went out as 4 alerts; Eduardo deleted
  them and wants the first and the last at most). For events of `ENTRADA_LENTE` (the fixed
  lens) the service reads the direction in the event's track (`data.path_data`,
  `canteiro.alertas.direcao_trilha`, `ENTRADA_SENTIDO` −1 = x decreasing is coming in) and
  passes it to the engine (rule 2d): **chegando** → the alert says "· chegando"; **saindo**
  → the episode's second and last message, "· saindo (chegou às HH:MM)" (`Mensagem.tipo`
  `saida`, with the exit frame); **parado** on the entrance lens, or any PT-lens sighting,
  while the last truck of the day came in through the entrance and has not left (≤ 60 min
  since its last sighting) = that truck, joined silently BEFORE the gallery ("sem chegada
  nova pela canteiro_fixa" in the log). A truck that left drops out of the gallery (back
  again = new visit). A departure can only be decided once the event has ENDED (a parking
  manoeuvre also drives "out": 11:20:46 on 09/11 moved +0,27 at 30 s and +0,07 at the end), so
  an open entrance-lens event without a clear direction waits up to `DIRECAO_ESPERA_S`
  (30 s) before it is evaluated. `alertas.jsonl` lines carry `direcao`.
- **Weak sightings keep the episode alive** (rule 2e, 09/11/2026): P(truck) between 0,5 and
  the threshold never alerts, but extends the open episode — on 09/11 the truck was seen 9
  times at 0,46–0,82 between the alerts and the engine "lost" it three times.
- **Honest type** (rule 2f, 09/11/2026 — Poter 14:31: "betoneira" with 26 % of the truck
  mass, the caption said "confiança 94 %", which is P(truck)): the type is named only when
  the winning class holds ≥ 0,4 of the truck mass (`Config.tipo_min`); below that the alert
  is "🚚 *Caminhão no canteiro*" + "Tipo incerto: betoneira 28 % ou caçamba 24 %." with no
  unloading line, and the footer shows both numbers when a type is named ("confiança 95% ·
  tipo 75%"). Every frame's full class distribution is logged (`quadros[].probs`,
  `agregado.probs`/`tipo`) so V3 can be re-tuned offline.
- **Latest frame of an event still running**: an open event ≥ 20 s also classifies the frame
  at now − 10 s ("agora"), even when the first frame said "car" — on 09/11 the PT lens had the
  Poter poliguindaste for 54 s and judged it on the +3 s frame alone (P 0,04).
- Never: team van, cars, machines; below threshold; 19:00–06:00 (log only).
- Caps: 6/hour, 15/day — one "alertas pausados" note to SmokeTests.
- OpenAI failure (key, IP, credits, network) = no alert, only a log line.
- Concrete pours (start + end summary): **retired 29/10/2026** — the pour happens out of
  frame; betoneira and bomba are per-vehicle alerts (daily cap raised to `CAP_DAY` = 25).

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
