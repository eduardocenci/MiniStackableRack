# waha-listener

Generic WhatsApp event platform for the shared bnu WAHA gateway. Receives WAHA
`message.any` webhook events, archives messages + media per chat, and matches
them against rules (`rules.yaml`). A matching rule queues a **pending job** and
POSTs a **notify URL** — for the `finance` rule that is the PC trigger
endpoint ([scripts/finance_trigger.py](../../../../finance_trigger.py)), which
spawns a headless Claude run of the `finance-hangar` skill.

**No LLM here** — plumbing only. Adding a WhatsApp automation = adding a rule,
not a service.

## Where it runs

bnu-proxmox → LXC 101 `docker` → container `waha-listener`, on the
`waha_default` docker network next to `waha` (so WAHA reaches it as
`http://waha-listener:8000`). Published on the LXC at **10.1.1.126:8788**.
Deployed dir: `/opt/waha-listener/`.

## Wiring (WAHA side)

`/opt/waha/docker-compose.yml` needs (then `docker compose up -d`):

```yaml
      WHATSAPP_HOOK_URL: "http://waha-listener:8000/webhook?token=<LISTENER_TOKEN>"
      WHATSAPP_HOOK_EVENTS: "message.any"
```

**`message.any`, not `message`** (since 2026-08-05 / WAHA 2026.7.2): the plain
`message` event no longer carries **fromMe** messages, and the finance wake
word IS fromMe — the session runs Eduardo's own account. `message.any` covers
incoming + outgoing; subscribe it alone (subscribing both would duplicate
incoming events). The listener accepts either event name, canonicalizes
lid-form chat ids to `@g.us`/`@c.us` (LID rollout broke the chat filter
silently on 2026-08-02 — see ../waha/README.md incident log), records the
sender's `push_name`, and INFO-logs every rejected chat id so a filter
mismatch can never be silent again.

## Media durability

WAHA (NOWEB, `WHATSAPP_DOWNLOAD_MEDIA` default on) downloads each received
file to its own disk, puts `payload.media.url` in the event, and **deletes the
file `WHATSAPP_FILES_LIFETIME` = 180 s later** (WAHA default — the compose sets
neither). After that, only WAHA's messages API can bring it back
(`GET /api/<session>/chats/<jid>/messages/<id>?downloadMedia=true`, which
downloads it from WhatsApp's CDN again) — and only while the CDN still has it,
which varies **per file**, not by a fixed age: on 2026-10-03 the first repair
pass (30 days) saved files up to 29.7 days old, while 39 others in the same
window already answered `410 Gone` / `404 Not Found` (a probe 33 days back got
`403 Forbidden`), and 7 were missing from WAHA's own store.

So the listener persists every media itself:

1. **At webhook time** (own 3-thread pool, not FastAPI `BackgroundTasks`): try
   1 reads WAHA's copy through `media.url` **with the host rewritten to
   `WAHA_BASE_URL`** — WAHA has no `WAHA_BASE_URL` of its own, so it advertises
   `http://localhost:3000/api/files/…`, which inside this container is the
   listener itself. Tries 2–6 (after 3/10/30/60/120 s) ask the messages API
   for a fresh download. A CDN `403/404/410` stops the schedule early.
2. **Repair pass** — 30 s after startup and every `MEDIA_REPAIR_HOURS`: every
   archived media of the last `MEDIA_BACKFILL_DAYS` still missing gets two
   tries; after `MEDIA_MAX_PASSES` failed passes it is left alone (`force`
   overrides). Also on demand: `POST /media/backfill`.
3. **On demand** — `GET /media/{msg_id}` for a media not archived yet makes one
   rescue try before answering 404 (the 404 detail says why).

Each file is `data/media/<msg_id>.<ext>`; its sidecar `_meta/<msg_id>.json`
records the **served file name** — the sender's name for documents
(`2026.10.02_Biroerre (NF).pdf`), else WAHA's `<key-id>.<ext>`
(`3EB09F0E7996E1764B2A46.jpeg`, `…oga`) — the same names, and the same bytes
(sha256), that the home-ara ingest already filed through the WAHA rescue path,
so its dedupe keeps matching.

**Incident 2026-10-03:** the listener had **never** archived a media —
`data/media/` (created 2026-07-16) was empty, and the container log held 519
`media download failed … [Errno 111] Connection refused` lines: one per media
message archived since the 2026-08-05 rebuild (the `localhost` URL above).
Consumers survived on their own WAHA fallback, inside the CDN window. The
first repair pass after the fix recovered what the CDN still had.

## Deploy / update

Files land in `/opt/waha-listener/` inside LXC 101. From the repo root (Git
Bash; `MSYS_NO_PATHCONV=1` keeps it from rewriting the `/tmp`/`/opt` paths):

```bash
export MSYS_NO_PATHCONV=1
L=scripts/proxmox/docker/bnu-docker/waha-listener
python scripts/devtool.py guest bnu 101 "cd /opt/waha-listener && cp -p app.py app.py.bak-$(date +%Y%m%d) && docker tag waha-listener-waha-listener:latest waha-listener-waha-listener:rollback"
python scripts/devtool.py push bnu-proxmox $L/app.py /tmp/wl-app.py
python scripts/devtool.py run bnu-proxmox "pct push 101 /tmp/wl-app.py /opt/waha-listener/app.py && rm /tmp/wl-app.py"
python scripts/devtool.py guest bnu 101 "cd /opt/waha-listener && docker compose build -q && docker compose up -d"
```

Build first, then `up -d`: the webhook gap is just the container swap. A
changed `Dockerfile` re-runs `pip` — if the build outlives devtool's 120 s,
run it under `nohup` and poll (REMOTE_ACCESS.md §3). Dependencies are pinned
in the Dockerfile (an unpinned rebuild would pull whatever Starlette major is
current). Rollback: copy `app.py.bak-<date>` back, or retag the `rollback`
image as `latest` and `docker compose up -d`.

`/opt/waha-listener/.env` on the LXC (never committed) provides
`WAHA_API_KEY`, `LISTENER_TOKEN` (repo `.env`: `BNU_WAHA_API_KEY`,
`BNU_WAHA_LISTENER_TOKEN`) and `FINANCE_NOTIFY_URL` (the PC trigger URL,
repo `.env`: `ARA_FIN_PC_TRIGGER_URL`).

`FINANCE_NOTIFY_URL` is **`http://10.1.1.127:8799/`** — the finance trigger on
bnu-win11 (set 2026-07-30, replacing a dead `10.1.1.48` that had been silently
severing the real-time trigger). It must stay a **LAN IP**: this LXC is not a
tailnet node, so MagicDNS names do not resolve inside it (`getent hosts
bnu-win11` fails). Give bnu-win11 a DHCP reservation rather than switching to a
name. Changing it requires recreating the container (env vars are read at
start): `docker compose up -d`.

Rule tweaks do **not** need a restart — `rules.yaml` is re-read per event
(it is bind-mounted, so re-push + nothing else).

Media knobs (code defaults; the compose sets none): `MEDIA_BACKFILL_DAYS`
(30), `MEDIA_REPAIR_HOURS` (6; 0 = no repair thread), `MEDIA_MAX_PASSES` (3),
`WAHA_SESSION` (`default` — an event's own `session` wins).

## API (all except /health and /webhook need `X-Listener-Token`)

| Route | Purpose |
|---|---|
| `POST /webhook?token=` | WAHA events in (auth via query token) |
| `GET /health` | liveness + loaded rule names + media counters since start (`saved`, `failed`, `last_error`) |
| `GET /pending?rule=finance` | queued jobs |
| `POST /ack/{rule}/{job_id}` | mark job done (moves to processed/) |
| `GET /messages?chat=<jid>&since=<unix_ts>&limit=` | archived messages |
| `GET /media/{msg_id}` | the media file — `Content-Type` from WhatsApp, `Content-Disposition: attachment; filename*=UTF-8''<name>; filename="<ascii>"` (RFC 6266; the ASCII fallback comes LAST so a naive "text after the last `filename=`" parser still works); one rescue try if not archived yet, else 404 with the reason |
| `GET /media` | archive health: files stored, media not archived yet (`_failed/` records), last repair pass |
| `POST /media/backfill?days=30&force=false` | start a repair pass now (409 while one runs) |
| `GET /chats` | archived chat list |

Checking it from the LXC (curl is installed there; the token never leaves it):

```bash
python scripts/devtool.py guest bnu 101 'T=$(sed -n "s/^LISTENER_TOKEN=//p" /opt/waha-listener/.env); curl -s -H "X-Listener-Token: $T" http://127.0.0.1:8788/media | head -c 800'
python scripts/devtool.py guest bnu 101 "docker logs waha-listener 2>&1 | grep -E 'media|backfill' | tail -20"
```

The access log masks the webhook `?token=` and drops the dashboard's
20-second `GET /health` 200s — media and rule lines are readable again.

## Data (volume `./data`; LXC disk 15 GB free on 2026-10-03)

```
data/chats/<jid>/messages.jsonl    message archive
data/media/<msg_id>.<ext>          media files
data/media/_meta/<msg_id>.json     sidecar: served filename, mimetype, size, sha256, source
data/media/_failed/<msg_id>.json   not archived yet: passes, last error
data/media/_tmp/                   partial downloads (cleared at startup)
data/pending/<rule>/<job>.json     waiting for a consumer
data/processed/<rule>/<job>.json   acked
```

No automatic retention — prune `data/media/` manually if it ever grows large
(the obra group runs ~250 media/month).
