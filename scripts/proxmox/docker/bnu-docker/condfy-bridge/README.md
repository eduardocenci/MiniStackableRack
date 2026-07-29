# condfy-bridge

Bridges gate-access events from the **Condfy** condo portal (Céu Azul, the ARA
house site in Araquari) into Home Assistant over MQTT, and pings the ARA
WhatsApp group when a watched person passes a gate.

The portal keeps only a ~4-day rolling window of notifications and shows relative
times ("há uma hora"), so this service exists to give the events a durable,
queryable home and an automation hook. **No LLM here** — plumbing only.

## Where it runs

bnu-proxmox → LXC 101 `docker` → container `condfy-bridge`, on the `waha_default`
network next to `waha` (reached as `http://waha:3000`). Deployed dir:
`/opt/condfy-bridge/`. Nothing is served — no published ports; health is an MQTT
sensor in Home Assistant.

The subject is ARA but the host is BNU Docker, same split as `waha-listener`.

## How it works

```
Condfy API ──poll 60s──▶ condfy-bridge ──┬─▶ SQLite /data/condfy.db   (history + dedup)
                                          ├─▶ MQTT 10.1.1.124:1883    (HA discovery)
                                          └─▶ WAHA http://waha:3000   (watched people only)
```

`GET {base}/v1/user/notifications?page=0&size=15` returns the **account's** feed.
Access events are `typeName: CONTROLE_ACESSOS` with a rendered pt-BR sentence:

```
Altair Dalpra passou por portão grande utilizando tag
```

which is split into person / gate / method by a regex in `condfy.py`. When the
sentence does not match that shape it is stored verbatim with `parsed=0`, and
watchlist matching falls back to scanning the whole message — so an unusual
phrasing still alerts. `SELECT DISTINCT message FROM events WHERE parsed=0` is
the corpus for extending the regex.

The notification `id` is a stable integer and is used directly as the dedup key.
The service **never writes back to Condfy** — notifications are never marked read,
so the phone app's unread state is untouched.

### Login

`POST /v1/public/auth/login {username, password, deviceUuid}` — the route and body
were recovered from the SPA bundle. It answers 200 with the user profile and **no
token in the body**: the credential is a `Set-Cookie: csl=<jwt>` session cookie,
which `requests.Session` carries from then on. Two things worth knowing:

- **Browser headers are mandatory.** A bare python-requests call gets an nginx
  `403 Forbidden` HTML page from the edge before it ever reaches the application.
  `BROWSER_HEADERS` in `condfy.py` is what makes the API answer at all.
- **The cookie lives ~10 minutes.** Renewal goes through
  `POST /v1/public/auth/refreshToken {userId, deviceUuid}`, so a healthy bridge
  logs in once per restart rather than ~288 times a day against the endpoint the
  edge guards — which matters because the account has auditing enabled. A full
  login is the fallback whenever a refresh fails.

`deviceUuid` is generated once and kept in SQLite so the account does not collect a
new "dispositivo conectado" on every login. `CONDFY_LOGIN_PATH` pins the route if
Condfy moves it (otherwise a short candidate list is probed and remembered), and
`CONDFY_BOOTSTRAP_TOKEN` accepts a `csl` value copied out of a browser.

### Alerts never storm

Five independent guards, in order:

1. **First run seeds silently** — every event already in the feed is inserted
   pre-marked as handled, so a fresh database never fires a backlog of messages.
2. **Age gate** — nothing older than `MAX_ALERT_AGE_MIN` (60) alerts, ever.
3. **Per-person cooldown** — `ALERT_COOLDOWN_MIN` (10).
4. **One message per poll cycle** — several events collapse into one list.
5. **Hourly fuse** — `ALERT_MAX_PER_HOUR` (8).

An empty `GROUP_JID` disables WhatsApp entirely (the log records what *would*
have been sent), and the JID is shape-validated before any send.

## Configuration

`/opt/condfy-bridge/.env` on the LXC (never committed; `chmod 600`). Repo-root
`.env` holds the same values under prefixed names:

| LXC `.env` | repo `.env` | notes |
|---|---|---|
| `CONDFY_EMAIL` / `CONDFY_PASSWORD` | `ARA_CONDFY_EMAIL` / `ARA_CONDFY_PASSWORD` | portal login |
| `CONDFY_BASE_URL` | `ARA_CONDFY_BASE_URL` | `https://api.condfy.com.br/api/cwa` |
| `CONDFY_LICENSE_ID` | `ARA_CONDFY_LICENSE_ID` | `9358` = Céu Azul |
| `CONDFY_LOGIN_PATH` | `ARA_CONDFY_LOGIN_PATH` | optional; empty = probe |
| `CONDFY_BOOTSTRAP_TOKEN` | `ARA_CONDFY_BOOTSTRAP_TOKEN` | optional escape hatch |
| `WATCH_NAMES` | `ARA_CONDFY_WATCH_NAMES` | comma-separated |
| `POLL_SECONDS`, `ALERT_*`, `MAX_ALERT_AGE_MIN` | `ARA_CONDFY_*` | tuning |
| `MQTT_*` | `BNU_MQTT_*` | Mosquitto add-on on bnu-homeassistant |
| `WAHA_API_KEY` / `WAHA_SESSION` | `BNU_WAHA_API_KEY` / `BNU_WAHA_SESSION` | shared gateway |
| `GROUP_JID` | `ARA_WHATSAPP_GROUP_JID` | ARA house group; empty = WhatsApp off |

Config-only changes need no rebuild: edit `.env`, then `docker compose up -d`.

## MQTT topics

| Topic | Payload | Retained |
|---|---|---|
| `condfy/ara/status` | `online` / `offline` (LWT) | yes |
| `condfy/ara/event` | one publish per new event | **no** |
| `condfy/ara/last_event` | newest event (state snapshot) | yes |
| `condfy/ara/person/<slug>/state` | last pass by that person | yes |
| `condfy/ara/gate/<slug>/state` | last pass through that gate | yes |
| `condfy/ara/bridge/state` | health JSON, every poll | yes |
| `condfy/ara/notice` | last non-access notice (e.g. platform expiry) | yes |

`condfy/ara/event` is deliberately **not** retained: a retained event replays on
every HA reconnect and would re-fire automations. `last_event` is the retained
state that is *meant* to be replayed.

Home Assistant entities are created by MQTT discovery under one device,
**Condfy — Céu Azul (ARA)** — no package YAML to deploy. HA derives the entity_id
from the device name, so they land as:

```
sensor.condfy_ceu_azul_ara_ultimo_acesso              quem passou por último
sensor.condfy_ceu_azul_ara_<pessoa>_visto_em          um por pessoa monitorada
binary_sensor.condfy_ceu_azul_ara_bridge_online       liveness (LWT)
sensor.condfy_ceu_azul_ara_ultima_coleta              heartbeat + health attrs
binary_sensor.condfy_ceu_azul_ara_problema_de_login   3+ falhas de login
```

## Deploy / update

Files land in `/opt/condfy-bridge/` inside LXC 101, via bnu-proxmox
(`scripts/devtool.py push` then `pct push`):

```
pct exec 101 -- sh -c 'cd /opt/condfy-bridge && docker compose up -d --build'
```

Checks:

```
docker logs --tail 80 condfy-bridge
docker compose run --rm condfy-bridge python app.py --once       # one poll, no alerts
docker compose run --rm condfy-bridge python app.py --selftest   # one canned WhatsApp
```

## Tests

Run both before deploying a change — they need only `requests` and take a second:

```
python3 test_bridge.py          # 41 offline end-to-end checks, exits non-zero on failure
python3 -m doctest condfy.py    # parsing, normalisation and timestamp helpers
```

`test_bridge.py` stubs paho and WAHA and points the service at a throwaway SQLite
file, so it touches nothing outside a temp dir and sends nothing. It covers the
behaviours that are expensive to get wrong in production: silent first-run
seeding (no alert storm), dedup on the notification id, the per-person cooldown,
the age gate, unparseable sentences still matching the watchlist, the Home
Assistant discovery payloads, `condfy/ara/event` staying un-retained, and events
being held rather than lost while the broker is down.

Neither is wired into CI — this service is deployed by hand to the LXC, so they
are a pre-deploy step, matching the waha-listener convention of plain scripts
plus doctests rather than a pytest suite.

## Data (volume `./data`)

`data/condfy.db` — SQLite (WAL). `events` is the history and the dedup set;
`state` holds the token, the discovered login path, the seeded flag, poll health
and per-person alert cooldowns. Roughly 400 bytes per event at a few events per
day, so no retention job.

**Privacy:** the feed is account-wide, so this database records the comings and
goings of every resident and visitor of the condo, not only the watched people.
It is the same data the portal already shows the account holder, it never leaves
LXC 101, and it is what makes the "último acesso" sensor and history possible —
but it is worth being deliberate about.
