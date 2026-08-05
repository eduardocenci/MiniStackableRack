# waha (WhatsApp HTTP API)

Sole WhatsApp gateway for **all regions**. Runs on bnu-proxmox → LXC 101
`docker` → container `waha`, published at **10.1.1.126:3000**. Deployed dir on
the LXC: `/opt/waha/` (compose + `.sessions` volume — the compose file there
hardcodes the credentials that the repo keeps in `.env` as `BNU_WAHA_*`).

- Engine: **NOWEB** (Baileys, no browser), image `devlikeapro/waha` (CORE).
- Session `default`, account +49 176 7239 2054, dashboard at
  `http://10.1.1.126:3000/dashboard`.
- Webhook `message` events → `waha-listener` (see [../waha-listener/](../waha-listener/README.md)).

## Incident log

### 2026-07-28 — 405 client-version rejection (RESOLVED 2026-08-05)

WhatsApp raised the minimum accepted web-client version; WAHA ≤ 2026.7.1
(Baileys pinned `2.3000.1035920091`) looped `405 Connection Failure`, session
`STARTING` → `FAILED`; QR re-scan did not help. See
[WhiskeySockets/Baileys#2733](https://github.com/WhiskeySockets/Baileys/issues/2733),
[devlikeapro/waha#2191](https://github.com/devlikeapro/waha/issues/2191).
A temp compose entrypoint injected a newer Baileys version number at start.
**Patch removed 2026-08-05**: WAHA **2026.7.2** ships the dynamic-version fix.
Compose restored from `docker-compose.yml.pre405fix`; rollback artifacts on
LXC 101: patched compose `docker-compose.yml.bak-lid-20260805`, old image
tagged `devlikeapro/waha:pre-lid-2026.7.1`, sessions backups
`/root/waha-sessions-backup-20260728-2122.tgz` and `-20260805.tgz`.

### 2026-08-02 — LID addressing silently killed webhook filtering (RESOLVED 2026-08-05)

WhatsApp flipped the ARA fin/obra group to **LID addressing**
(`addressingMode: "lid"` in `GET /api/{session}/groups`). From ~17:40 (02/08)
webhook payloads stopped matching waha-listener's `@g.us` chat filter, so the
group archive AND the `casa, financeiro` wake-word rule went dark for 3 days —
while the session stayed `WORKING` and outbound sends kept working. On top,
WAHA 2026.7.2 does not emit **fromMe** messages on the plain `message` event —
and every wake word IS fromMe (the session runs Eduardo's own account).
**Fix (both halves):** compose now subscribes
`WHATSAPP_HOOK_EVENTS: "message.any"`, and waha-listener canonicalizes
lid-form chat ids back to `@g.us`/`@c.us` (via the payload's
`key.remoteJidAlt`) and **INFO-logs every rejected chat id** — the silent
drop is what hid this incident. Verified 2026-08-05: fromMe test message
archived end-to-end. The lid↔phone map of a group lives in
`GET /api/{session}/groups` → `participants[].id` / `.phoneNumber`.

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
4. **Archive silent while session WORKING** → check waha-listener logs for
   `ignored chat` lines (addressing drift, LID-style) and confirm the hook
   still subscribes `message.any` — see incident 2026-08-02 above.
