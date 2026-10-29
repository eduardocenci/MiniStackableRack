# HANDOFF — cast the canteiro camera to the TV in Bento (bg)

Written 2026-10-29 by a cloud session (no tailnet, no `.env`) for the local
desktop session that takes over. **Nothing was executed against any device
and no code/config was drafted** — this file is the whole state. Delete it
once the work is merged/registered.

## Goal (Eduardo, verbatim)

1. *"voce acha que conseguiriamos implementar o mesmo cast da camera do
   canteiro que fazemos na TV em blumenau, na TV de bento? (com um link
   distinto, naturalmente)"*
2. *"a TV de bento é uma samsung de modelo muito similar, pesquise na rede e
   no HA de bg. se precisar instale a TV no HA."*
3. *"pare, vou retomar esta sessao localmente."*

So:

- **Camera:** the ARA canteiro camera (Intelbras iM9+ Full Color, PT lens
  `channel=1`, main stream HEVC 2304×1296), same feed bnu's 55" shows.
- **Screen:** a Samsung TV at the bg site (Bento Gonçalves), "very similar
  model" to bnu's 55" Neo QLED (`media_player.qn85f1443`). Exact model, IP and
  HA entity are **not yet known** (nothing in the repo mentions it).
- **When/how:** on demand, exactly like bnu — a one-click start/stop
  **bookmark link distinct from bnu's** (HA webhooks with new random ids). No
  schedule or automatic trigger was requested.
- **Authorized by Eduardo:** add the TV to bg HA if it is not there.

## How bnu does it today (read, not re-verified live)

Source: `scripts/raspberry-pi/bnu-raspberrypi/docker/go2rtc/README.md`,
`scripts/raspberry-pi/ara-raspberrypi/docker/canteiro-relay/README.md`.

```
iM9 cam ─RTSP─▶ canteiro-relay (mediamtx, ara-raspberrypi2 :8554/canteiro)
  ─tailnet, ONE copy─▶ go2rtc on bnu-raspberrypi (10.1.1.123 / 100.91.64.62)
     ├─ :8554/canteiro, canteiro_sub → bnu Frigate LXC 105
     ├─ 127.0.0.1:8554 → canteiro-hls (mediamtx) → /live page
     └─ http://10.1.1.123:1984/api/stream.mp4?src=canteiro → Cast → 55" TV
```

- bnu HA `script.canteiro_na_tv` casts the **HEVC passthrough progressive MP4**
  (`/api/stream.mp4?src=canteiro`) to `media_player.qn85f1443` (Samsung's
  native Google Cast receiver).
- Start/stop links: `automation.webhook_canteiro_na_tv_ligar` →
  `script.canteiro_na_tv`; `automation.webhook_canteiro_na_tv_parar` →
  `media_player.media_stop`. URLs in `.env` as `ARA_TV_WEBHOOK_ON/OFF`
  (blank HTTP 200 = normal).
- **The script and the two automations are NOT versioned in this repo** —
  read them live from bnu HA to clone them:
  `devtool.py ha bnu GET /api/config/script/config/canteiro_na_tv` and
  `GET /api/config/automation/config/<id>` (id from `/api/states` attrs).
- bnu go2rtc RTSP `:8554` and API `:1984` listen on all interfaces, no reader
  auth (LAN + tailnet trust) — bg can read them over the tailnet.

### Known pitfalls (all documented in the READMEs above)

- **Never pull from ara a second time.** Two main-stream readers saturated the
  canteiro Starlink upload on 2026-08-26 (relay "reader is too slow", TV froze).
  bg must read from **bnu's go2rtc**, not `ara-raspberrypi2`. bnu's upload
  then carries one extra copy only while the bg TV is casting.
- **Never cast `canteiro_h264`** (software HEVC decode, Pi 4 load ~6). If the bg
  TV can't decode HEVC, restream the camera substream (`canteiro_sub`, H.264
  640×480, zero transcode) instead.
- Cast + go2rtc **HLS** failed on the Samsung receiver (launches, never fetches
  segments) → use `stream.mp4`.
- Samsung **DLNA** (60" AU8000) does not play endless live streams — dead end.
  The bg TV must have a **Google Cast receiver**.
- Cold power-on over the network needs the TV setting **"Power On with
  Mobile"** (else the Cast entity is `unavailable` in deep standby).
- Cast receivers can't resolve names → the HA script must use a **LAN IP** URL.

### Cross-site precedent: mia already does this with a bnu stream

`scripts/proxmox/mia-proxmox/README.md`: mia HA casts bnu Frigate's
`birdseye` to the Samsung QN90F (`media_player.qn90f9745`) through
socket-activated `systemd-socket-proxyd` units on **mia-proxmox** that forward
`bnu-frigate:8554/:1984` onto the mia LAN (`systemd/frigate-birdseye-*`).
Reason: mia HA's Tailscale add-on is inbound-only, so HA can't reach the
tailnet. Expect the same at bg (unverified).

## Proposed design for bg (not yet decided by Eduardo)

TV (bg LAN `192.168.0.0/24`, Claro HUMAX `.1`) can't reach bnu's LAN or the
tailnet, so something on the bg LAN must re-serve bnu's
`/api/stream.mp4?src=canteiro`. Two options:

- **A (recommended — lighter, proven at mia):** a socket-activated
  `systemd-socket-proxyd` on **bg-proxmox** (on LAN and tailnet) listening on
  `<bg-proxmox-LAN-IP>:1984` → `100.91.64.62:1984` (bnu-raspberrypi go2rtc).
  TV URL: `http://<bg-proxmox-LAN-IP>:1984/api/stream.mp4?src=canteiro`.
  No new Docker container → the container registration checklist does not
  apply; host config goes in `scripts/proxmox/bg-proxmox/systemd/` (new folder)
  plus a README. bg-proxmox already relays WAHA to bnu (`:3001`).
- **B:** a go2rtc container on bg-raspberrypi (image `alexxit/go2rtc:1.9.14`,
  `canteiro: rtsp://bnu-raspberrypi:8554/canteiro`). Mirrors bnu, but it is a
  new container → full CLAUDE.md checklist (folder, `architecture.yaml`,
  runbook, `make fleet`, globalnet push + submodule bump). Only worth it if bg
  wants local fan-out to several screens.

Either way, bg HA gets:

- `script.canteiro_na_tv` cloned from bnu, `media_content_id` = the bg LAN
  URL, `media_content_type: video/mp4`, target = the bg TV's Cast entity.
- Two webhook automations with **new random ids** (the "link distinto") →
  root `.env` BG section, `# Home Assistant` group, as
  `BG_TV_WEBHOOK_ON` / `BG_TV_WEBHOOK_OFF`; mirror placeholders in
  `.env.example`.
- Versioned copy in the repo, e.g.
  `scripts/proxmox/homeassistant/bg-homeassistant/packages/canteiro_na_tv.yaml`
  (bg HA already uses `packages/`; consider versioning bnu's too).

## What was tried here, and results

| Attempt | Result |
|---|---|
| `getent hosts bg-homeassistant bg-raspberrypi` / `curl http://bg-homeassistant:8123` from the cloud container | **Failed** — names don't resolve (container is not a tailnet node, no `tailscale` binary) |
| Read root `.env` | **Absent** (gitignored, not in the fresh clone) |
| Attach Eduardo's computer to this session via Remote Control | **No folders offered** — nothing to attach |
| Search repo for the bg TV (`grep` TV/Samsung/Cast/media_player in docs, HA packages, `.env.example`) | **Nothing** about a bg TV; bg HA folder has only `packages/whatsapp.yaml` |

**Blocker:** no network path to bg from the cloud. That is the only reason
this stopped; the work is unblocked from the local machine.

## Next steps (in order)

1. **Find the TV in bg HA:** `devtool.py ha bg GET /api/states`, filter
   `media_player.*`; websocket `config_entries/get` for `cast` / `samsungtv`
   entries; websocket `config_entries/flow/progress` for pending discoveries.
2. **Find it on the bg LAN** (from bg-raspberrypi or bg-proxmox): scan
   `192.168.0.0/24` for `:8009` (Cast) and `:8001` (Tizen). Identify by asking
   the TV, not the router: `curl http://<ip>:8001/api/v2/` → `modelName`, year
   (see REMOTE_ACCESS.md "Identifying a Samsung Tizen device").
3. **If missing in HA, add it (authorized):** confirm the `cast` discovery
   flow with `{}`, or `POST /api/config/config_entries/flow {"handler":"cast"}`
   (REST config-flow mechanics in REMOTE_ACCESS.md, HA row). Note the entity id.
4. **Smoke test Cast + HEVC** with a public HEVC MP4 via
   `media_player.play_media` (`video/mp4`) before building anything. Check
   "Power On with Mobile" (entity must stay available in standby).
5. **Check the bnu→bg tailnet path:** `tailscale ping bnu-raspberrypi` from
   bg-proxmox (or bg-raspberrypi) — direct vs DERP. DERP jitter may stutter a
   progressive MP4; bnu's own TV doesn't have this second internet hop.
   Also check whether bg HA can reach the tailnet (expect not, like mia).
6. **Ask Eduardo** option A vs B (recommend A), then build: relay on bg,
   HA script + webhooks, `.env`/`.env.example` keys, end-to-end test
   (start link → TV plays; stop link → stops; check bnu go2rtc
   `/api/streams` shows the extra consumer and the ara relay still has one).
7. **Record**: TV IP/model/entity + any new access method in
   `REMOTE_ACCESS.md`; reserve TV and relay-host IPs on the HUMAX
   (bg reservations not applied yet — REMOTE_ACCESS.md IP plan); update the
   bnu go2rtc README consumer list (bg is a new reader); update
   `systemarchitecture.excalidraw`.

## Open questions for Eduardo

- Option A (proxy on bg-proxmox) vs B (go2rtc container on bg-raspberrypi)?
- Is one extra main-stream copy on bnu's upload while bg casts acceptable?
  (It is not Starlink, but it is bnu's WAN.)
- Same stop behaviour as bnu (`media_stop`), or also turn the TV off?
