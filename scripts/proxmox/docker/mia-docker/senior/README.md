# senior — compose project `senior` on mia-docker

How the `senior-*` containers run on [`mia-docker`](../README.md)
(LXC 102, `192.168.2.23`) in `/opt/senior/`. This page covers how to deploy,
reach, pair and back them up.

> This repo is **public**. Secret values and the server-only seed file stay
> out of it: the secrets live in `.env` files, and the seed exists only on
> the server.

## Containers

| Container | Image | Published | State |
|---|---|---|---|
| `senior-waha` | `devlikeapro/waha:noweb-2026.9.2` | `192.168.2.23:3000` (LAN) | running since 2026-10-29 |
| `senior-postgres` | `postgres:17.10-alpine` | — (compose network only) | running, healthcheck `pg_isready` |

Planned, not deployed: `senior-n8n`, `senior-mcp`, `senior-sync`,
`senior-whisper`, `senior-nocodb`.

- **Every container carries the `senior-` prefix** (Eduardo's naming
  request).
- **Images are pinned to releases at least 14 days old** (fleet update rule).
  WAHA `2026.10.1` was skipped on 2026-10-29 for that reason.
- Files: [`docker-compose.yml`](docker-compose.yml) and
  [`setup-senior.sh`](setup-senior.sh) are the deployed versions. They read
  and generate the `MIA_SENIOR_*` names.

### senior-waha

- WAHA Core, **NOWEB** engine, session **`default`**, created by
  `setup-senior.sh` with the **NOWEB store enabled**
  (`config.noweb.store.enabled: true`, `fullSync: false`).
- It has its **own dedicated WhatsApp number**, separate from bnu's `waha`
  (LXC 101), which runs Eduardo's personal account and the fleet
  notifications. The two gateways share nothing. Do not send fleet alerts
  through this one.
- `WAHA_BASE_URL=http://192.168.2.23:3000`, so media URLs in events never
  say `localhost` (bnu incident of 2026-10-03, below).
- **No webhook yet.** It will be set once `senior-n8n` exists, using
  `message.any` and HMAC.
- Dashboard: `http://192.168.2.23:3000/dashboard`, user `admin`, password
  `MIA_SENIOR_WAHA_DASHBOARD_PASSWORD`. Swagger uses the same login. API
  header: `X-Api-Key: <MIA_SENIOR_WAHA_API_KEY>`.
- Health check without auth: `GET http://192.168.2.23:3000/ping` → `200`.
  Without credentials, `/dashboard` and `/api/*` answer `401`.
- Volumes: `./waha/sessions` → `/app/.sessions`, `./waha/media` →
  `/app/.media`. The sessions directory holds the pairing; losing it means
  pairing again.

### senior-postgres

- Database **`acervo_bot`**, user `senior`. There is no published port, so
  the database is reachable only on the compose network
  (`senior-postgres:5432` from the other `senior-*` containers).
- The init scripts live in `/opt/senior/postgres/init/`: the schema
  (`001_esquema.sql`) and a **server-only seed** (`900_contatos_locais.sql`,
  never committed to any repo). Postgres runs them **only when the data
  directory is empty**, so a later schema change needs a migration run with
  `psql`, not a re-run of `/docker-entrypoint-initdb.d`.
- `TZ: UTC` in this container; `senior-waha` runs in `America/Sao_Paulo`.

## Secrets

Secrets use the **same names** in `/opt/senior/.env` (root, mode 600) and in
the repo-root `.env` (MIA section). `.env.example` lists them. Never print
the values.

| Key | Used for | Origin |
|---|---|---|
| `MIA_SENIOR_WAHA_API_KEY` | WAHA `X-Api-Key` | generated on the LXC by `setup-senior.sh` |
| `MIA_SENIOR_WAHA_DASHBOARD_PASSWORD` | dashboard and Swagger, user `admin` | generated on the LXC by `setup-senior.sh` |
| `MIA_SENIOR_POSTGRES_PASSWORD` | Postgres user `senior` | generated on the LXC by `setup-senior.sh` |
| `MIA_SENIOR_ANTHROPIC_API_KEY` | — (not on the LXC yet) | created by Eduardo; copied to `/opt/senior/.env` when `senior-n8n` is deployed |

The values generated on the LXC were created there, so if the two copies
drift, the LXC copy wins.

Rotation: edit `/opt/senior/.env`, then run `docker compose up -d`, which
recreates WAHA with the new key or password. Postgres reads
`POSTGRES_PASSWORD` **only when it initialises an empty data directory**,
so a new Postgres password also needs
`docker exec senior-postgres psql -U senior -d acervo_bot -c "ALTER USER senior PASSWORD '…'"`.
Update the root `.env` in the same session.

## Deploy (first time)

1. Set up the LXC and Docker: [`../README.md`](../README.md) and `install-docker.sh`.
2. Stage the files in `/root/senior-stage/` on the LXC. Route: mia-desktop →
   `devtool.py push mia-proxmox …` → `pct push 102 …`.
   - `docker-compose.yml` and `setup-senior.sh` (this folder);
   - `001_esquema.sql` (schema, kept outside this repo);
   - `900_contatos_locais.sql`, written by hand **on the server only**. The
     script copies it without checking, so the file must exist (an empty
     file is fine).
3. Run `setup-senior.sh` detached, because `devtool` cuts sessions after
   about a minute:

   ```bash
   python scripts/devtool.py guest mia 102 "nohup sh /root/senior-stage/setup-senior.sh > /root/setup-senior.log 2>&1 &"
   python scripts/devtool.py guest mia 102 "cat /root/setup-senior.log"
   ```

   The script is idempotent. It creates the directories, copies the staged
   files and writes `.env` only if it is missing. It then runs
   `docker compose pull` and `docker compose up -d`, waits for WAHA, creates
   session `default` with the store on, and prints the session state, the
   WAHA version and the containers.

## Redeploy / update

- **Compose change:** stage the new `docker-compose.yml`, copy it to
  `/opt/senior/`, then run `cd /opt/senior && docker compose up -d`. Only
  the services that changed are recreated.
- **Image bump** (2-week rule): change the tag, then run
  `docker compose pull && docker compose up -d`. **Back up the sessions before
  a WAHA upgrade** (see Backups). A WAHA or WhatsApp change can break a
  NOWEB session (bnu's 405 incident).
- Re-running `setup-senior.sh` is safe. It keeps `.env` and the data
  directories, and session creation only reports a non-2xx code when
  `default` already exists.
- **Never remove** `/opt/senior/waha/sessions` (the pairing) or
  `/opt/senior/postgres/data` (the database).

## Pairing the WhatsApp number

Session `default` waits in `SCAN_QR_CODE` until it is paired. Do this from
a machine on the mia LAN:

- **Dashboard:** open `http://192.168.2.23:3000/dashboard`, log in as
  `admin` with `MIA_SENIOR_WAHA_DASHBOARD_PASSWORD`, open session `default`
  and show the QR. On the phone that owns the number, go to WhatsApp →
  *Linked devices* → *Link a device* and scan it.
- **API:**

  ```bash
  curl -s -H "X-Api-Key: $MIA_SENIOR_WAHA_API_KEY" \
    "http://192.168.2.23:3000/api/default/auth/qr?format=image" -o qr.png
  ```

  Open `qr.png` and scan it. The QR rotates about every 20 s; fetch it again
  if it expires.
- **Check:** `GET /api/sessions/default` returns `status: WORKING`, and `me`
  is the paired number.
- The paired phone (the primary device) stays at mia, on Wi-Fi and on
  power. WhatsApp drops linked devices after about 14 days without the
  primary device online.

## WAHA lessons from the bnu incidents

Source: [`../../bnu-docker/waha/README.md`](../../bnu-docker/waha/README.md)
§ Incident log, and the waha-listener README.

| bnu incident | What to do here | Status |
|---|---|---|
| 2026-08-02: WAHA does not emit **fromMe** messages on the plain `message` event | Subscribe the webhook to **`message.any`**. The consumer must skip `fromMe` itself, so it does not react to its own sends. | when the webhook is set |
| 2026-08-02: **LID addressing** silently broke the `@g.us`/`@c.us` filters | **Canonicalise ids** before matching them against any filter. Map a `…@lid` id back to the phone JID: use payload `key.remoteJidAlt`, or for groups `GET /api/default/groups` → `participants[].id`/`.phoneNumber`. | in the consumer |
| The silent drop hid that incident for 3 days | **Log every rejected id.** | in the consumer |
| 2026-08-05: `MessageCounterError` (stale sender keys), with no store to recover from | **NOWEB store on from day one.** If `MessageCounterError` shows in `docker logs senior-waha`, back up the sessions, then log out and pair again. | done |
| 2026-10-03: media URLs said `localhost:3000` | **`WAHA_BASE_URL=http://192.168.2.23:3000`** in the compose file | done |
| 2026-07-28: `405` client-version rejection after a WhatsApp minimum-version bump | Keep WAHA current within the 2-week rule. Set `WAHA_LOG_LEVEL: "debug"` to see the real disconnect reason. | ongoing |

## Backups

**None scheduled yet (2026-10-29).** To set up:

| What | How | Where to |
|---|---|---|
| Postgres `acervo_bot` | daily `docker exec senior-postgres pg_dump -U senior -Fc acervo_bot > acervo_bot-$(date +%F).dump` | mia-nas-ds918plus (LAN `192.168.2.15`); retention to decide |
| WAHA sessions | `tar czf waha-sessions-$(date +%F).tgz -C /opt/senior/waha sessions`, before every WAHA upgrade and periodically | NAS. Losing the sessions means pairing again |
| `/opt/senior/.env` | already in the repo-root `.env` (MIA section) | — |
| Whole LXC (optional) | a vzdump job for VMID 102 on mia-proxmox (that host has no vzdump job for any guest) | NAS |

## Quick checks

```bash
python scripts/devtool.py guest mia 102 "cd /opt/senior && docker compose ps"
python scripts/devtool.py lan mia 192.168.2.23 "curl -sS -o /dev/null -w '%{http_code}\n' http://192.168.2.23:3000/ping"
python scripts/devtool.py guest mia 102 "docker logs --tail 50 senior-waha"
python scripts/devtool.py guest mia 102 "docker exec senior-postgres pg_isready -U senior -d acervo_bot"
```
