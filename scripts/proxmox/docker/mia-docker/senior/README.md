# senior — Senior acervo bot (compose project `senior`)

Infrastructure of the **WhatsApp assistant that answers questions from the
senior-sistemas knowledge base**. Its design lives in that **private**
repo, `eduardocenci/senior-sistemas`: decision 0021, `bot/ARQUITETURA.md`,
the n8n flows, the SQL schema, the access list and the bot's phone number.
This folder only covers how it runs on [`mia-docker`](../README.md)
(LXC 102, `192.168.2.23`) in `/opt/senior/`.

> This repo is **public**. The bot's number, the contacts allowlist and every
> secret stay out of it: the number is in senior-sistemas, the contacts are
> only on the server, and the secrets are in `.env` files.

## Containers

| Container | Image | Published | State |
|---|---|---|---|
| `senior-waha` | `devlikeapro/waha:noweb-2026.9.2` | `192.168.2.23:3000` (LAN) | running since 2026-10-29 |
| `senior-postgres` | `postgres:17.10-alpine` | — (compose network only) | running, healthcheck `pg_isready` |
| `senior-n8n` | — | — | planned: the bot's flows; target of the WAHA webhook |
| `senior-mcp` | — | — | planned: read-only knowledge-base tools |
| `senior-sync` | — | — | planned: `git pull` of senior-sistemas' public ring |
| `senior-whisper` | — | — | planned: faster-whisper, pt-BR, for voice notes |
| `senior-nocodb` | — | — | planned: viewer for the conversation history |

- **Every container carries the `senior-` prefix** (Eduardo's explicit naming
  request). It groups them in `docker ps` and on the globalnet dashboard, and
  keeps them apart from any future tenant of the LXC.
- **Images are pinned to releases at least 14 days old** (fleet update rule).
  WAHA `2026.10.1` was skipped on 2026-10-29 for that reason.
- Files: [`docker-compose.yml`](docker-compose.yml) and
  [`setup-senior.sh`](setup-senior.sh) are the deployed versions.
  `setup-senior.sh` here also contains the `chmod 755` fix for the init
  directory, which is newer than the copy that ran on 2026-10-29.

### senior-waha

- WAHA Core, **NOWEB** engine, session **`default`**, created by
  `setup-senior.sh` with the **NOWEB store enabled**
  (`config.noweb.store.enabled: true`, `fullSync: false`).
- Its WhatsApp account is a **dedicated number** (US), used only by the
  bot. It is separate from bnu's `waha` (LXC 101), which runs Eduardo's
  personal account and carries the fleet notifications. The two gateways
  share nothing.
- `WAHA_BASE_URL=http://192.168.2.23:3000`, so media URLs in events never
  say `localhost` (lesson from the bnu incident of 2026-10-03, below).
- **No webhook yet.** When the n8n flows exist, the session's webhook
  will point at `senior-n8n` with the `message.any` events, signed with HMAC.
- Dashboard: `http://192.168.2.23:3000/dashboard`. User `admin`; the password
  is `SENIOR_WAHA_DASHBOARD_PASSWORD`, which Swagger also uses. API header
  `X-Api-Key: <SENIOR_WAHA_API_KEY>`.
- Health without auth: `GET http://192.168.2.23:3000/ping` → `200`. Without
  credentials, `/dashboard` and `/api/*` answer `401`.
- Volumes: `./waha/sessions` → `/app/.sessions` (holds the pairing; losing
  it means pairing again), `./waha/media` → `/app/.media`.

### senior-postgres

- Database **`acervo_bot`**, user `senior`. It has no published port, so it
  is reachable only on the compose network (`senior-postgres:5432` from the
  other `senior-*` containers).
- Schema: `bot/sql/001_esquema.sql` in senior-sistemas, copied to
  `/opt/senior/postgres/init/`. Tables: `parametros`, `contatos`,
  `conversas`, `consultas`, `mensagens`, `feedback`, `rejeicoes`.
  **Triggers refuse `DELETE` and `TRUNCATE`** on `mensagens`, `consultas`,
  `feedback` and `rejeicoes`. The history is never deleted (Eduardo,
  2026-10-29). Do not try to "clean up" with `DELETE`; the trigger will
  refuse it.
- Init scripts run **only when the data directory is empty** (Postgres
  entrypoint). A later schema change needs a migration run with `psql`,
  not a re-run of `/docker-entrypoint-initdb.d`.
- **Contacts seed is server-only.** `/opt/senior/postgres/init/900_contatos_locais.sql`
  holds phone numbers. It is never committed, in this repo or in
  senior-sistemas, and you add it to the stage directory by hand.
- The container runs with `TZ: UTC`, while `senior-waha` runs with
  `America/Sao_Paulo`.

## Secrets

`/opt/senior/.env` (root, mode 600) is generated **once** by
`setup-senior.sh` from `/dev/urandom` and is never printed. The repo-root
`.env` mirrors it under MIA-prefixed names. The LXC is where the values
were created, so on drift the LXC copy is the source.

| `/opt/senior/.env` (LXC) | Repo-root `.env` (mirror) | Used for |
|---|---|---|
| `SENIOR_WAHA_API_KEY` | `MIA_SENIOR_WAHA_API_KEY` | WAHA `X-Api-Key` |
| `SENIOR_WAHA_DASHBOARD_PASSWORD` | `MIA_SENIOR_WAHA_DASHBOARD_PASSWORD` | dashboard + Swagger, user `admin` |
| `SENIOR_POSTGRES_PASSWORD` | `MIA_SENIOR_POSTGRES_PASSWORD` | Postgres user `senior` |

Rotation: edit `/opt/senior/.env`, then `docker compose up -d` (this
recreates WAHA with the new key or password). Postgres reads
`POSTGRES_PASSWORD` **only when it initialises an empty data directory**,
so a new Postgres password also needs
`docker exec senior-postgres psql -U senior -d acervo_bot -c "ALTER USER senior PASSWORD '…'"`.
Update the root `.env` mirror in the same session.

## Deploy (first time)

1. LXC and Docker: [`../README.md`](../README.md) and `install-docker.sh`.
2. Stage the files in `/root/senior-stage/` on the LXC. Route: mia-desktop →
   `devtool.py push mia-proxmox …` → `pct push 102 …`.
   - `docker-compose.yml` and `setup-senior.sh` (this folder);
   - `001_esquema.sql` (senior-sistemas `bot/sql/`);
   - `900_contatos_locais.sql`, written by hand **on the server only**. The
     script copies it without checking, so the file must exist (an empty
     file is fine).
3. Run `setup-senior.sh` detached (`devtool` cuts sessions after about a
   minute):

   ```bash
   python scripts/devtool.py guest mia 102 "nohup sh /root/senior-stage/setup-senior.sh > /root/setup-senior.log 2>&1 &"
   python scripts/devtool.py guest mia 102 "cat /root/setup-senior.log"
   ```

   The script is idempotent. It creates the directories, copies the staged
   files, writes `.env` only if it is missing, runs `docker compose pull`
   and `docker compose up -d`, and waits for WAHA. It then creates session
   `default` with the store on and prints the session state, the WAHA
   version, the containers and a count of contacts and parameters.

## Redeploy / update

- **Compose change:** stage the new `docker-compose.yml`, copy it to
  `/opt/senior/`, then run `cd /opt/senior && docker compose up -d`. Only
  the services that changed are recreated.
- **Image bump** (2-week rule): change the tag, then run
  `docker compose pull && docker compose up -d`. **Before a WAHA upgrade, back
  up the sessions** (see Backups). A WAHA or WhatsApp change can break a
  NOWEB session (bnu's 405 incident).
- Re-running `setup-senior.sh` is safe. It keeps `.env` and the data
  directories, and session creation only reports a non-2xx code when
  `default` already exists.
- **Never remove** `/opt/senior/waha/sessions` (the pairing) or
  `/opt/senior/postgres/data` (the history).

## Pairing the WhatsApp number

Session `default` exists and waits in `SCAN_QR_CODE` until it is paired.
Do this from a machine on the mia LAN:

- **Dashboard:** open `http://192.168.2.23:3000/dashboard` and log in as
  `admin` with `MIA_SENIOR_WAHA_DASHBOARD_PASSWORD`. Open session `default`
  and show the QR. On the bot's phone, go to WhatsApp → *Linked devices* →
  *Link a device* and scan it.
- **API:**

  ```bash
  curl -s -H "X-Api-Key: $MIA_SENIOR_WAHA_API_KEY" \
    "http://192.168.2.23:3000/api/default/auth/qr?format=image" -o qr.png
  ```

  Open `qr.png` and scan it. The QR rotates every 20 s or so; fetch it again
  if it expires.
- **Check:** `GET /api/sessions/default` should return `status: WORKING`, with
  `me` being the bot's number.
- The bot's phone (the primary device) stays at mia, on Wi-Fi and on power.
  WhatsApp drops linked devices after about 14 days without the primary
  device online.

## Lessons from the bnu WAHA incidents

Source: [`../../bnu-docker/waha/README.md`](../../bnu-docker/waha/README.md)
§ Incident log, and the waha-listener README.

| bnu incident | What it means here | Status |
|---|---|---|
| 2026-08-02: WAHA does not emit **fromMe** messages on the plain `message` event | Subscribe the webhook to **`message.any`**. Messages typed by hand on the bot's phone are then archived too. The flow must skip `fromMe` itself, or the bot answers its own replies. | when the webhook is set |
| 2026-08-02: **LID addressing** silently broke the `@g.us`/`@c.us` filters | **Canonicalise ids** before checking the contacts allowlist: map a `…@lid` id back to the phone JID (payload `key.remoteJidAlt`; for groups, `GET /api/default/groups` → `participants[].id`/`.phoneNumber`). Otherwise allowed people are silently rejected. | in the senior-n8n flows |
| The silent drop hid that incident for 3 days | **Log every rejected id.** The schema has the `rejeicoes` table for this. | schema ready |
| 2026-08-05: `MessageCounterError` (stale sender keys), and no store to recover from | **NOWEB store on from day one.** If `MessageCounterError` appears in `docker logs senior-waha`, pair again (logout + QR) after backing up the sessions. | done |
| 2026-10-03: media URLs said `localhost:3000` | **`WAHA_BASE_URL=http://192.168.2.23:3000`** in the compose file | done |
| 2026-07-28: `405` client-version rejection after WhatsApp raised the minimum version | Keep WAHA current within the 2-week rule. To see the real disconnect reason, set `WAHA_LOG_LEVEL: "debug"`. | ongoing |

## Backups

**None scheduled yet (2026-10-29).** To set up:

| What | How | Where to |
|---|---|---|
| Postgres `acervo_bot` | `docker exec senior-postgres pg_dump -U senior -Fc acervo_bot > acervo_bot-$(date +%F).dump`, daily | mia-nas-ds918plus (LAN `192.168.2.15`), retention to decide. The history is append-only by design, so this dump is its only second copy. |
| WAHA sessions | `tar czf waha-sessions-$(date +%F).tgz -C /opt/senior/waha sessions`, before every WAHA upgrade and periodically | NAS. Losing the sessions means pairing again. |
| `/opt/senior/.env` | already mirrored in the repo-root `.env` (MIA section) | — |
| Whole LXC (optional) | a vzdump job for VMID 102 on mia-proxmox (none exists for any guest there) | NAS |

## Quick checks

```bash
python scripts/devtool.py guest mia 102 "cd /opt/senior && docker compose ps"
python scripts/devtool.py lan mia 192.168.2.23 "curl -sS -o /dev/null -w '%{http_code}\n' http://192.168.2.23:3000/ping"
python scripts/devtool.py guest mia 102 "docker logs --tail 50 senior-waha"
python scripts/devtool.py guest mia 102 "docker exec senior-postgres pg_isready -U senior -d acervo_bot"
```
