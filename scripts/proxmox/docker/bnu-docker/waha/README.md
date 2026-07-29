# waha (WhatsApp HTTP API)

Sole WhatsApp gateway for **all regions**. Runs on bnu-proxmox → LXC 101
`docker` → container `waha`, published at **10.1.1.126:3000**. Deployed dir on
the LXC: `/opt/waha/` (compose + `.sessions` volume — the compose file there
hardcodes the credentials that the repo keeps in `.env` as `BNU_WAHA_*`).

- Engine: **NOWEB** (Baileys, no browser), image `devlikeapro/waha` (CORE).
- Session `default`, account +49 176 7239 2054, dashboard at
  `http://10.1.1.126:3000/dashboard`.
- Webhook `message` events → `waha-listener` (see [../waha-listener/](../waha-listener/README.md)).

## ⚠ TEMP PATCH active since 2026-07-28 — remove on next WAHA release

**Incident:** on 2026-07-28 WhatsApp raised the minimum accepted web-client
version. Every Baileys deployment with the old pinned version (WAHA ≤ 2026.7.1
announces `2.3000.1035920091`) got its login rejected with `405` in an endless
`Connection Failure` loop → session stuck `STARTING` → force-stopped →
`FAILED`. Fleet-wide event, not account-specific: see
[WhiskeySockets/Baileys#2733](https://github.com/WhiskeySockets/Baileys/issues/2733),
[devlikeapro/waha#2191](https://github.com/devlikeapro/waha/issues/2191),
[devlikeapro/waha#2192](https://github.com/devlikeapro/waha/issues/2192).
A QR re-scan does **not** help (pairing gets 405 too).

**Fix applied:** `/opt/waha/docker-compose.yml` overrides the entrypoint to
inject `version: [2, 3000, 1043857760]` into
`/app/dist/core/engines/noweb/session.noweb.core.js` (guarded/idempotent sed)
before starting WAHA. Session recovered with existing login, no re-scan.

**To remove** (once a WAHA release ships the Baileys dynamic-version fix):
delete the `entrypoint:` + `command:` overrides from
`/opt/waha/docker-compose.yml` (pre-incident copy saved on the LXC as
`docker-compose.yml.pre405fix`), then `docker compose pull && docker compose up -d`.

**Backups on LXC 101:** `/root/waha-sessions-backup-20260728-2122.tgz`
(`.sessions` store), `/opt/waha/docker-compose.yml.pre405fix`.

## Symptom cheat-sheet (session FAILED)

1. `GET /api/sessions?all=true` (header `X-Api-Key: $BNU_WAHA_API_KEY`) —
   status `FAILED`/`STARTING` while container is `Up` ⇒ session-level issue.
2. Turn on debug to see the real disconnect reason (generic log line only says
   `Connection Failure`): add `WAHA_LOG_LEVEL: "debug"` to the compose env,
   `docker compose up -d`, then look for
   `"lastDisconnect":{"error":{"data":{"reason":"..."` in `docker logs waha`.
   - `401` → logged out ⇒ re-scan QR via dashboard.
   - `405` → client version rejected ⇒ this incident again: bump the pinned
     version number in the compose entrypoint (find the current one in the
     Baileys/WAHA issue trackers) or update WAHA.
3. Smoke test send: `POST /api/sendText` to `SMOKETESTS_WHATSAPP_GROUP_JID`.
