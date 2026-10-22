# canteiro-vigia — weekend watch of the ARA canteiro camera (WhatsApp)

Saturdays and Sundays (America/Sao_Paulo) every **person or vehicle** the bnu
Frigate (LXC 105) tracks on the `canteiro` camera becomes **one short message**
in the WhatsApp group **"Casa Céu Azul"**: the snapshot of the moment plus one
line saying whether a device joined the canteiro Wi-Fi in the minute after the
detection — a friendly sign, whoever has the Wi-Fi password may be there — or
whether nobody did, which raises the severity. Decisão Eduardo 22/10/2026.

```
🟢 *Canteiro · sáb 25/10 14:32* — veículo (carro)
📱 Entrou na rede: iPhone (Jorge) (14:32)
```
```
🔴 *Canteiro · dom 26/10 03:11* — pessoa
📵 Nenhum aparelho entrou na rede do canteiro no minuto seguinte.
```

## How it knows about "the following minute"

The Starlink router answers `wifi_get_clients` over local gRPC with
**`associatedTimeS`** per client — seconds since that phone associated to the
Wi-Fi. `now − associatedTimeS` is the exact instant it joined, so the verdict
does not depend on netoverview's 5-min ARP scan (kept as a ±5 min fallback
through `/api/presence`). The router is LAN-only at ara; the Pi's
[`router-proxy`](../../ara-raspberrypi/docker/router-proxy/) socat relay makes
it `ara-raspberrypi2:9000` on the tailnet (the dish already had the same
treatment, `starlink-proxy` on `:9200`).

Verdict for a detection at `t0`, read at `t0 + JANELA_S` (60 s):

| client associated at | counts as | line |
|---|---|---|
| `t0 − PRE_S` … `t0 + JANELA_S` | **joined now** → 🟢 | `📱 Entrou na rede: X (HH:MM)` |
| before `t0 − PRE_S` | already there → 🟢 | `📱 Já na rede: X, Y` |
| after `t0 + JANELA_S` (late evaluation) | joined later → 🟢 | `📱 Entrou na rede depois: X (HH:MM)` |
| none of the above | nobody → **🔴** | `📵 Nenhum aparelho entrou na rede …` |

Fixed gear never counts (router `74:24:9f`, iM9 camera `54:ba:d9`, the Pi
`88:a2:9e` — matched by OUI and name because the router masks client MACs to
the OUI). Names come from netoverview nicknames when the device has one
(`iPhone (Jorge)`), else the router's DHCP name. Router and netoverview both
unreachable → 🔴 with "Rede sem leitura." (fail towards the louder alert).

## Keeping it short

- **One message per episode.** The first weekend event opens an episode at its
  `start_time`; every further detection within `EPISODIO_MIN` (15 min) of the
  episode joins it (`×N` in the header) and sends nothing.
- **Friendly silence.** After a 🟢 message no other 🟢 for `SILENCIO_AMIGO_MIN`
  (60 min) — a crew member walking around all Saturday with his phone on the
  Wi-Fi is one message an hour, not one every 15 min. A 🔴 always goes out.
- **Caps.** `MAX_HORA` 6 / `MAX_DIA` 24; beyond them the service only logs and
  posts one "⏸️" note to Casa SmokeTests per day.
- `first` round after a fresh start marks what is already in Frigate as seen
  (no alerts for the past); `state.json` makes a restart idempotent.

Weekday reviews keep going through bnu Home Assistant's `frigate_whatsapp.py`
(GenAI narrative + clip, suppressed while any phone is online) — this service
does not touch weekdays, and on weekends the two overlap only in the 🔴 case
(the HA narrative then follows the snapshot a few minutes later).

## Where things run

- Container `canteiro-vigia` on bnu-raspberrypi, own image
  ([`../docker/canteiro-vigia/`](../docker/canteiro-vigia/): python 3.13-slim
  + grpcurl 1.9.1, no pip layer). Host network: WAHA on the bnu LAN
  (`10.1.1.126:3000`), Frigate and the ara Pi over the tailnet.
- Live layout `~/canteiro-vigia/`: `Dockerfile`, `compose.yml`,
  `canteiro-vigia.py`, `env/canteiro-vigia.env` (chmod 600, NOT in git — values
  from the repo-root `.env`: `BNU_WAHA_*`, `ARA_ALERTAS_GROUP_JID` = Casa Céu
  Azul, `SMOKETESTS_WHATSAPP_GROUP_JID`).
- State `/var/lib/canteiro-vigia/` (uid 1000): `state.json`, `vigia.jsonl`
  (one line per decided episode — verdict, source, names, destination, reason
  when not sent), `heartbeat.json`.

## Modes

`MODE` in the env file — each step only with Eduardo's OK:

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

(`sed` because this checkout is CRLF — see REMOTE_ACCESS.md, *Line endings*.)

## Test without pinging the family

```bash
python scripts/devtool.py run bnu-raspberrypi "docker exec canteiro-vigia python3 /app/canteiro-vigia.py --selftest"      # Frigate, router, netoverview, WAHA — no send
python scripts/devtool.py run bnu-raspberrypi "docker exec canteiro-vigia python3 /app/canteiro-vigia.py --veredito"      # the network verdict for "now", printed
python scripts/devtool.py run bnu-raspberrypi "docker exec canteiro-vigia python3 /app/canteiro-vigia.py --test-alert"    # latest event, any weekday, "[TESTE]" → TEST_JID
```

`--test-alert <chatId>` targets any chat; without an argument it goes to
`TEST_JID` (Casa SmokeTests), never the family by accident.

## Operate

```bash
python scripts/devtool.py run bnu-raspberrypi "docker logs --since 6h canteiro-vigia"
python scripts/devtool.py run bnu-raspberrypi "tail -5 /var/lib/canteiro-vigia/vigia.jsonl"
python scripts/devtool.py run bnu-raspberrypi "cat /var/lib/canteiro-vigia/heartbeat.json"
```

Failure modes: `heartbeat.json` with `ok: false` = Frigate unreachable
(`bnu-frigate:5000`) — the loop keeps retrying every `POLL_S`; a log line
"roteador sem resposta … tentando netoverview" = the ara relay is down
(Starlink blip or `router-proxy` stopped) — verdicts carry "(±5 min)" until it
returns; "Rede sem leitura." in a message = both ara paths were down at the
verdict.
