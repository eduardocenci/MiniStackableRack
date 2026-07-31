# waha-listener

Generic WhatsApp event platform for the shared bnu WAHA gateway. Receives WAHA
`message` webhook events, archives messages + media per chat, and matches them
against rules (`rules.yaml`). A matching rule queues a **pending job** and
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
      WHATSAPP_HOOK_EVENTS: "message"
```

## Deploy / update

From this folder (files land in `/opt/waha-listener/` inside LXC 101):

```
scp app.py Dockerfile docker-compose.yml rules.yaml root@bnu-proxmox:...  # or via pct push
pct exec 101 -- sh -c 'cd /opt/waha-listener && docker compose up -d --build'
```

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
(it is bind-mounted, so re-`scp` + nothing else).

## API (all except /health and /webhook need `X-Listener-Token`)

| Route | Purpose |
|---|---|
| `POST /webhook?token=` | WAHA events in (auth via query token) |
| `GET /health` | liveness + loaded rule names |
| `GET /pending?rule=finance` | queued jobs |
| `POST /ack/{rule}/{job_id}` | mark job done (moves to processed/) |
| `GET /messages?chat=<jid>&since=<unix_ts>&limit=` | archived messages |
| `GET /media/{msg_id}` | downloaded media file |
| `GET /chats` | archived chat list |

## Data (volume `./data`, ~29 GB free on the LXC)

```
data/chats/<jid>/messages.jsonl   message archive
data/media/<msg_id>.<ext>         media files
data/pending/<rule>/<job>.json    waiting for a consumer
data/processed/<rule>/<job>.json  acked
```

No automatic retention — prune `data/media/` manually if it ever grows large.
