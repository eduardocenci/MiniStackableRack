# senior — compose project `senior` on mia-docker

How the `senior-*` containers run on [`mia-docker`](../README.md)
(LXC 102, `192.168.2.23`) in `/opt/senior/`. This page covers how to deploy,
reach, pair, update and back them up.

> This repo is **public**. Secret values and the server-only seed file stay
> out of it: the secrets live in `.env` files, and the seed exists only on
> the server.

## Containers

| Container | Image | Published | State |
|---|---|---|---|
| `senior-waha` | `devlikeapro/waha:noweb-2026.9.2` | `192.168.2.23:3000` (LAN) | running since 2026-10-29 |
| `senior-postgres` | `postgres:17.10-alpine` | — (compose network only) | running, healthcheck `pg_isready` |
| `senior-n8n` | `n8nio/n8n:2.43.7` | `192.168.2.23:5678` (LAN) | running since 2026-11-01, 6 workflows published |
| `senior-whisper` | `onerahmet/openai-whisper-asr-webservice:v1.10.0` | — | running since 2026-11-01 (faster-whisper `small`, int8, CPU) |
| `senior-mcp` | `senior-acervo:local` | — | running since 2026-11-01 |
| `senior-agente` | `senior-acervo:local` | — | running since 2026-11-01 |
| `senior-painel` | `senior-acervo:local` | `192.168.2.23:8080` (LAN) | running since 2026-11-01 |
| `senior-sync` | `senior-acervo:local` | — | running since 2026-11-01 |

Planned, not deployed: `senior-nocodb`.

`senior-acervo:local` is **built on the LXC** from the application's service code (staged as `servicos/`, see
Deploy); the four containers run the same image with different commands. Memory on 2026-11-01: ~1.8 GB of the 3 GB
limit, half of it `senior-whisper` with the model loaded.

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
- **Webhook** (session config, set by `setup-senior.sh`): `message.any` →
  `http://senior-n8n:5678/webhook/senior-waha`, with header
  `X-Senior-Token: <MIA_SENIOR_WEBHOOK_TOKEN>`; the n8n Webhook node checks
  it (no header → `403`). Retries: exponential, 5 attempts.
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
- Also holds database **`n8n`** (n8n's own state). Migrations for an existing
  `acervo_bot` are idempotent `psql -f` runs (`002_fase1.sql`).

### senior-n8n

- Editor: `http://192.168.2.23:5678/`. Owner account created by Eduardo on
  the first visit (2026-11); its login is `MIA_N8N_USER` / `MIA_N8N_PW` in the
  repo-root `.env` only, not on the LXC.
- `N8N_ENCRYPTION_KEY` = `MIA_SENIOR_N8N_ENCRYPTION_KEY`: the credentials are
  encrypted with it; without it they cannot be restored (it travels in the
  nightly backup).
- Three credentials, created by `setup-senior.sh` from `.env`: Postgres
  (`acervo_bot`), WAHA API key, and the webhook header.
- Workflows are imported from staged JSON and published with the CLI. In
  n8n 2.43, `n8n execute`/`publish:workflow` inside the running container
  need `-e N8N_RUNNERS_BROKER_PORT=5690` (the server holds 5679), and a
  restart for the change to take effect:

  ```bash
  docker exec senior-n8n n8n import:workflow --input=/home/node/.n8n/import/fluxos/<file>.json
  docker exec -e N8N_RUNNERS_BROKER_PORT=5690 senior-n8n n8n publish:workflow --id=<id>
  docker restart senior-n8n
  ```

- `EXECUTIONS_DATA_MAX_AGE=720` (30 days). A workflow set to *not* save
  successful runs leaves them shown as `running` forever (seen 2026-11-01);
  keep saving them.

### senior-painel

- `http://192.168.2.23:8080/`, HTTP basic auth: user `admin`, password
  `MIA_SENIOR_WAHA_DASHBOARD_PASSWORD` (same as the WAHA dashboard). Read-only
  on `acervo_bot`. Health without auth: `GET /saude`.

### senior-sync

- Pulls a private git repo with a **read-only deploy key**
  (`/opt/senior/sync/chave/`, generated on the LXC; `github.com` host key
  pinned to GitHub's published ED25519 fingerprint). Every 30 min; each new
  commit becomes `/opt/senior/acervo/snap-<commit>` and the `atual` link
  swaps atomically. `senior-mcp` and `senior-agente` mount `/opt/senior/acervo`
  read-only.

### senior-whisper, senior-mcp, senior-agente

- No published port. Health: `senior-mcp:8000/saude`, `senior-agente:8001/saude`,
  `senior-whisper:9000/docs` (compose network). The n8n workflow *Saúde*
  checks them every 5 min and alerts over WhatsApp on a state change.
- `senior-agente` is the only container that talks to a service outside the
  LAN besides WAHA and git: the Anthropic API (`MIA_SENIOR_ANTHROPIC_API_KEY`).

## Secrets

Secrets use the **same names** in `/opt/senior/.env` (root, mode 600) and in
the repo-root `.env` (MIA section). `.env.example` lists them. Never print
the values.

| Key | Used for | Origin |
|---|---|---|
| `MIA_SENIOR_WAHA_API_KEY` | WAHA `X-Api-Key` | generated on the LXC by `setup-senior.sh` |
| `MIA_SENIOR_WAHA_DASHBOARD_PASSWORD` | dashboard and Swagger, user `admin` | generated on the LXC by `setup-senior.sh` |
| `MIA_SENIOR_POSTGRES_PASSWORD` | Postgres user `senior` | generated on the LXC by `setup-senior.sh` |
| `MIA_SENIOR_N8N_ENCRYPTION_KEY` | n8n credential encryption | generated on the LXC by `setup-senior.sh` |
| `MIA_SENIOR_WEBHOOK_TOKEN` | header `X-Senior-Token`, WAHA → n8n | generated on the LXC by `setup-senior.sh` |
| `MIA_SENIOR_ANTHROPIC_API_KEY` | `senior-agente` | created by Eduardo (own workspace, monthly limit); copied to `/opt/senior/.env` from the root `.env` on 2026-11-01 |
| `MIA_N8N_USER`, `MIA_N8N_PW` | n8n editor login (owner) | created by Eduardo in the n8n first-visit form; repo-root `.env` only |

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
2. Stage the files in `/root/senior-stage/` on the LXC (mia-desktop →
   `devtool.py push mia-proxmox …` → `pct push 102 …`; for many files, one
   tarball, extracted with `tar --no-same-owner` — the unprivileged LXC
   cannot chown to Windows uids):
   - `docker-compose.yml`, `setup-senior.sh`, `backup-senior.sh` (this folder);
   - from the application repo (kept outside this one): `001_esquema.sql`,
     `002_fase1.sql`, `servicos/` (with `sistema.md`), `n8n/*.json`;
   - `900_contatos_locais.sql`, written by hand **on the server only** (an
     empty file is fine);
   - optionally `anthropic.env`, the one `MIA_SENIOR_ANTHROPIC_API_KEY=` line
     from the root `.env` (the script appends it and deletes the file).
3. Run `setup-senior.sh` detached, because `devtool` cuts sessions after
   about two minutes:

   ```bash
   python scripts/devtool.py guest mia 102 "nohup sh /root/senior-stage/setup-senior.sh > /root/setup-senior.log 2>&1 &"
   python scripts/devtool.py guest mia 102 "cat /root/setup-senior.log"
   ```

   It is idempotent: directories, missing secrets, image build, deploy key
   (printed once — add it read-only to the repo), n8n database and migration,
   `docker compose up -d`, the WAHA session with its webhook, the n8n
   credentials and workflows, the backup cron.
4. Pair the WhatsApp number (below) if the session is new, and create the n8n
   owner account on the first visit.

The stack was brought up piece by piece on 2026-10-29 and 2026-11-01; the script gathers those steps but has not been
run end to end on an empty LXC.

## Redeploy / update

- **Compose change:** stage the new `docker-compose.yml`, copy it to
  `/opt/senior/`, then run `cd /opt/senior && docker compose up -d`. Only
  the services that changed are recreated.
- **Service code change** (`servicos/`): stage it, copy to
  `/opt/senior/build/servicos`, `docker build -t senior-acervo:local
  /opt/senior/build/servicos`, then `docker compose up -d` (recreates the four
  containers on that image).
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

[`backup-senior.sh`](backup-senior.sh), cron `/etc/cron.d/senior-backup`, **every night at 03:30** (log
`/var/log/senior-backup.log`, last success in `/opt/senior/backups/.ultimo-ok`). First run and a restore test on
2026-11-01.

| What | How |
|---|---|
| Postgres `acervo_bot` and `n8n` | `pg_dump -Fc` |
| WAHA sessions | `tar czf waha-sessions.tgz` (losing them means pairing again) |
| n8n workflows | `n8n export:workflow --all --separate` |
| `/opt/senior/.env` | copied as `env` (holds `MIA_SENIOR_N8N_ENCRYPTION_KEY`) |

- Kept **14 days** in `/opt/senior/backups/<date>/` (mode 600) and copied to
  **mia-nas-ds918plus** `/volume1/NetBackup/mia-docker-senior/<date>/`
  (kept 180 days) over SSH as user `globalnet`, key `/root/.ssh/senior-backup`,
  authorised on the NAS with `from="192.168.2.23"` and no forwarding/pty; NAS
  host key pinned in `/root/.ssh/known_hosts_nas`. The NetBackup share's
  inherited ACL is world-readable, so the script closes the folder to its
  owner (`chmod 700`) on every run.
- Restore: `pg_restore -U senior -d <db> --clean <file>.dump`;
  `tar xzf waha-sessions.tgz -C /opt/senior/waha`; put `env` back as
  `/opt/senior/.env` before starting `senior-n8n`.
- Not yet: a vzdump job for VMID 102 (mia-proxmox has none for any guest).

## Quick checks

```bash
python scripts/devtool.py guest mia 102 "cd /opt/senior && docker compose ps"
python scripts/devtool.py lan mia 192.168.2.23 "curl -sS -o /dev/null -w '%{http_code}\n' http://192.168.2.23:3000/ping"
python scripts/devtool.py guest mia 102 "docker logs --tail 50 senior-waha"
python scripts/devtool.py guest mia 102 "docker exec senior-postgres pg_isready -U senior -d acervo_bot"
python scripts/devtool.py guest mia 102 "tail -3 /var/log/senior-backup.log; cat /opt/senior/backups/.ultimo-ok"
python scripts/devtool.py lan mia 192.168.2.23 "curl -sS http://192.168.2.23:8080/saude"
```
