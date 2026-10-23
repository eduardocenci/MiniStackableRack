# canteiro-vigia — watch of the ARA canteiro camera while the obra is empty (WhatsApp)

While the canteiro Wi-Fi has **no person's device** on it (only the fixed gear:
router, camera, the Pi) the watch is **armed**: every **person or vehicle** the
bnu Frigate (LXC 105) tracks on the `canteiro` camera becomes **one short
message** in the WhatsApp group **"Casa Céu Azul"** — the snapshot of the moment
plus one line saying whether a known device joined the Wi-Fi in the minute after
the detection (a friendly sign: whoever has the Wi-Fi password may be there) or
whether nobody did, which makes it a stranger. The moment a person's device
shows up on the Wi-Fi the watch **disarms**; it re-arms `ARMAR_APOS_MIN` after
the Wi-Fi is empty again. In practice it arms on Friday when the crew leaves and
disarms on Monday when the crew arrives — nobody sleeps at the canteiro on
weekends — and it also covers holidays and the weeknights nobody slept there.
Decisão Eduardo 22/10/2026 (the first iteration, Saturday/Sunday only, lasted
a day).

```
🟢 *Canteiro · seg 26/10 06:28* — veículo (carro)
📱 Há dispositivos conhecidos no WiFi, então tudo deve estar OK: iPhone (Jorge) (entrou 06:28)
```
```
🔴 *Canteiro · dom 26/10 03:11* — pessoa
📵 Nenhum aparelho conhecido conectou no WiFi no minuto seguinte, logo deve ser alguém estranho !!!
```

"Known device" = any non-fixed device on the canteiro Wi-Fi: having the password
is the credential (the same doctrine as the weekday HA gate, decisão Eduardo
26/08/2026). Names come from netoverview nicknames when the device has one
(`iPhone (Jorge)`), else the router's DHCP name; two unnamed `iPhone` are told
apart by the IP's last octet.

## How it reads the Wi-Fi

The Starlink router answers `wifi_get_clients` over local gRPC with
**`associatedTimeS`** per client — seconds since that phone associated to the
Wi-Fi. `now − associatedTimeS` is the exact instant it joined, so neither the
armed state nor the verdict depends on netoverview's 5-min ARP scan (kept as a
±5 min fallback through `/api/devices`). The router is LAN-only at ara; the
Pi's [`router-proxy`](../../ara-raspberrypi/docker/router-proxy/) socat relay
makes it `ara-raspberrypi2:9000` on the tailnet (the dish already had the same
treatment, `starlink-proxy` on `:9200`). Fixed gear never counts (router
`74:24:9f`, iM9 camera `54:ba:d9`, the Pi `88:a2:9e` — matched by OUI and name
because the router masks client MACs to the OUI).

**Armed / disarmed** (read every `REDE_POLL_S` = 30 s):

| Wi-Fi | state |
|---|---|
| no person's device for `ARMAR_APOS_MIN` (5 min) | 🔒 **armed** — a phone that only blinked off does not arm it |
| any person's device | 🔓 **disarmed**, immediately |
| first read after a fresh start | whatever the Wi-Fi says, no note |
| router and netoverview both unreachable | state unchanged (last known) |

State changes post a one-line note "🔒 Vigia ligado · sex 24/10 17:07 — WiFi do
canteiro vazio há 5 min" / "🔓 Vigia desligado · seg 27/10 06:28 — iPhone
(Jorge) no WiFi" to `AVISAR_ESTADO`: `teste` (Casa SmokeTests, default — a
trail to validate the arming without touching the family group), `grupo`, or
`nao`.

**Verdict** for a detection at `t0`, read at `t0 + JANELA_S` (60 s), only while
armed:

| device associated at | line |
|---|---|
| `t0 − PRE_S` … `t0 + JANELA_S` | 🟢 `📱 Há dispositivos conhecidos no WiFi, então tudo deve estar OK: X (entrou HH:MM)` |
| any other time (already there / later) | 🟢 same line, name without the time |
| none | 🔴 `📵 Nenhum aparelho conhecido conectou no WiFi no minuto seguinte, logo deve ser alguém estranho !!!` |
| router and netoverview both down | 🔴 `📵 Sem leitura do WiFi do canteiro (roteador e netoverview fora), logo pode ser alguém estranho !!!` |

A 🟢 verdict also disarms the watch (someone from the crew arrived); a 🔴 leaves
it armed. A netoverview-sourced verdict carries "(leitura ±5 min)".

## Keeping it short

- **One message per episode.** The first detection while armed opens an
  episode at its `start_time`; every further detection within `EPISODIO_MIN`
  (15 min) of the episode joins it (`×N` in the header) and sends nothing…
- **…except the person after the car.** An episode opened by a **vehicle** gets a
  second message when a **person** is detected inside it — with the person's
  photo and its own verdict, header `pessoa (após carro HH:MM)` — so the group
  receives the car and then the person. Once per episode.
- **Caps.** `MAX_HORA` 6 / `MAX_DIA` 24; beyond them the service only logs and
  posts one "⏸️" note to Casa SmokeTests per day.
- The first round after a fresh start marks what is already in Frigate as seen
  (no alerts for the past); `state.json` makes a restart idempotent.

Weekday activity with the crew on site never alerts (disarmed). Weekday
reviews keep going through bnu Home Assistant's `frigate_whatsapp.py` (GenAI
narrative + clip, suppressed while any phone is online) — the two overlap only
in the 🔴 case, when the HA narrative follows the snapshot a few minutes later.
Someone sleeping at the canteiro on a weeknight keeps the watch disarmed as
long as their phone stays on the Wi-Fi; a phone that drops off for more than
`ARMAR_APOS_MIN` arms it, and their next walk past the camera is a 🔴 until the
phone reconnects (raise `ARMAR_APOS_MIN` if that happens).

## Where things run

- Container `canteiro-vigia` on bnu-raspberrypi, own image
  ([`../docker/canteiro-vigia/`](../docker/canteiro-vigia/): python 3.13-slim
  + grpcurl 1.9.1, no pip layer). Host network: WAHA on the bnu LAN
  (`10.1.1.126:3000`), Frigate and the ara Pi over the tailnet.
- Live layout `~/canteiro-vigia/`: `Dockerfile`, `compose.yml`,
  `canteiro-vigia.py`, `env/canteiro-vigia.env` (chmod 600, NOT in git — values
  from the repo-root `.env`: `BNU_WAHA_*`, `ARA_ALERTAS_GROUP_JID` = Casa Céu
  Azul, `SMOKETESTS_WHATSAPP_GROUP_JID`).
- State `/var/lib/canteiro-vigia/` (uid 1000): `state.json` (seen events, open
  episode, armed state, send counters), `vigia.jsonl` (one line per decided
  message — `tipo` episodio / seguimento — and per state change — `tipo`
  estado), `heartbeat.json` (also carries the armed state and the last Wi-Fi
  read).

## Modes

`MODE` in the env file — each step only with Eduardo's OK (`live` since
22/10/2026):

| `MODE` | effect |
|---|---|
| `log` | decides and writes `vigia.jsonl`, sends nothing |
| `shadow` | sends to Casa SmokeTests, caption prefixed "👁️ SOMBRA (iria ao Casa Céu Azul)" |
| `live` | sends to "Casa Céu Azul" (`GROUP_JID`) |

Change it, then `cd ~/canteiro-vigia && docker compose up -d`.

## Deploy (from this repo)

```bash
for f in docker/canteiro-vigia/Dockerfile docker/canteiro-vigia/compose.yml canteiro-vigia/canteiro-vigia.py; do
  MSYS_NO_PATHCONV=1 python scripts/devtool.py push bnu-raspberrypi \
    "scripts/raspberry-pi/bnu-raspberrypi/$f" "canteiro-vigia/$(basename $f)"
done
python scripts/devtool.py run bnu-raspberrypi "sudo mkdir -p /var/lib/canteiro-vigia && sudo chown 1000:1000 /var/lib/canteiro-vigia && cd ~/canteiro-vigia && sed -i 's/\r$//' canteiro-vigia.py && docker compose build && docker compose up -d"
```

(`sed` because this checkout is CRLF — see REMOTE_ACCESS.md, *Line endings*.
The build takes longer than devtool's read timeout the first time: run it
detached, `nohup sh -c 'docker compose build > build.log 2>&1' &`, and poll
`build.log`.)

## Test without pinging the family

```bash
python scripts/devtool.py run bnu-raspberrypi "docker exec canteiro-vigia python3 /app/canteiro-vigia.py --selftest"      # Frigate, router, netoverview, WAHA — no send
python scripts/devtool.py run bnu-raspberrypi "docker exec canteiro-vigia python3 /app/canteiro-vigia.py --status"        # armed? who is on the Wi-Fi? open episode?
python scripts/devtool.py run bnu-raspberrypi "docker exec canteiro-vigia python3 /app/canteiro-vigia.py --veredito"      # the Wi-Fi verdict for "now", printed
python scripts/devtool.py run bnu-raspberrypi "docker exec canteiro-vigia python3 /app/canteiro-vigia.py --test-alert"    # latest event, armed or not, "[TESTE]" → TEST_JID
```

`--test-alert <chatId>` targets any chat; without an argument it goes to
`TEST_JID` (Casa SmokeTests), never the family by accident. `FORCAR_ARMADO=1`
in the env keeps the watch armed with people on site (rehearsal only).

## Operate

```bash
python scripts/devtool.py run bnu-raspberrypi "docker logs --since 6h canteiro-vigia"
python scripts/devtool.py run bnu-raspberrypi "tail -5 /var/lib/canteiro-vigia/vigia.jsonl"
python scripts/devtool.py run bnu-raspberrypi "cat /var/lib/canteiro-vigia/heartbeat.json"
```

Failure modes: `heartbeat.json` with `ok: false` = Frigate unreachable
(`bnu-frigate:5000`) — the loop keeps retrying every `POLL_S`; a log line
"roteador sem resposta … tentando netoverview" = the ara relay is down
(Starlink blip or `router-proxy` stopped) — the armed state follows the
netoverview read (±5 min) until it returns; "Sem leitura do WiFi" in a message =
both ara paths were down at the verdict, and the armed state froze at its last
value. The watch never arms itself from a failed read.
