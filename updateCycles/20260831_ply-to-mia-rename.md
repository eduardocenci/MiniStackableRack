# ply → mia site rename (Plymouth → Miami)

**Date planned:** 2026-08-31 · **Status:** PLANNED — not executed
**Scope:** the physical rack moved from Plymouth to Miami. Rename the site code
`ply` → `mia` and every device name across the whole architecture: 7 tailnet
nodes, this repo, globalnet, netoverview, GlobalNet.xlsx, `.env`, the
Excalidraw diagram, and the fleet-update skill.

---

## Naming decisions (proposed defaults)

| Old | New |
|---|---|
| site code `ply` / label `PLY` | `mia` / `MIA` |
| `ply-proxmox` | `mia-proxmox` |
| `ply-homeassistant` | `mia-homeassistant` |
| `ply-raspberrypi` | `mia-raspberrypi` |
| `ply-nas-ds918plus` (alias `ply-nas`) | `mia-nas-ds918plus` (alias `mia-nas`) |
| `ply-glkvm` | `mia-glkvm` |
| `ply-win11` | `mia-win11` |
| `ply-desktop` | `mia-desktop` |
| env prefix `PLY_` | `MIA_` |
| architecture.yaml ids `ply_*` (`ply_prx`, `ply_ha`, `ply_rpi`, `ply_nto`, `ply_zb`, `ply_nas`, `ply_desktop`, `ply_gw`, `ply_kvm`, `ply_w11`) | `mia_*` |
| fleet-update site order `fln→bg→ply→ara→bnu` | `fln→bg→mia→ara→bnu` |

**Rename rule:** living config and docs get renamed everywhere — including
dated lessons in REMOTE_ACCESS.md (the machine is the same machine, the lesson
still applies to it). Immutable history stays untouched: git log,
`updateCycles/20260828_wave1*`, `globalnet/20260415_Changes.md`.

## Cross-site dependencies discovered (the traps)

1. **bnu Frigate GenAI runs on ply-desktop.** `bnu-frigate/config.yml` has
   `genai.base_url: http://ply-desktop:11434`, and bnu-proxmox runs a socat
   relay `:11434 → ply-desktop:11434` used by the bnu HA digest scripts.
   Renaming ply-desktop breaks bnu event descriptions until both are updated.
2. **Birdseye cast chain:** ply-proxmox runs socket-activated
   `systemd-socket-proxyd` units (`192.168.0.21:8554/:1984 → bnu-frigate`)
   feeding ply HA's `camera.frigate_birdseye` + Apple TV cast. Units survive a
   hostname rename (they bind a LAN IP), but verify after the Proxmox reboot.
3. **`.env` values, not just key names:** `PLY_HA_URL` and `PLY_UNIFI_URL`
   contain hostnames (`ply-homeassistant`, `ply-proxmox:8443` socat relay).
4. **GlobalNet.xlsx** is the source for `devices.json`
   (`export_from_excel.py`) — the Excel itself has Ply rows and must be edited
   with Excel closed, then re-exported.
5. **WAN-quality history lives on each Pi** (netoverview ring buffers /
   local store) — nothing in globalnet is keyed `ply` on disk, so no data
   migration; renaming the Pi hostname keeps its local history.

## Risk table

| Step | Risk | Why | Mitigation |
|---|---|---|---|
| Proxmox node rename | **HIGH** | Guest configs live under `/etc/pve/nodes/<hostname>/`; a botched rename strands VM 100 (HA) + VM 101 (win11) | Backup gate (below); GL KVM as out-of-band recovery; guests stopped during rename |
| ply-desktop rename | **MED** | Breaks bnu Frigate GenAI + HA digest until relay/config updated | Update bnu-proxmox socat unit + frigate config in the same step, verify with a test event |
| HA hostname change | **MED** | Known ply lessons: Tailscale add-on is inbound-only; Supervisor is slow after host reboot | Use `ha host options`, allow settle time, verify REST API before proceeding |
| NAS rename | LOW | Copyparty binds the Tailscale IP — IP doesn't change | Verify share after rename |
| Pi / glkvm / win11 renames | LOW | Standard hostname changes; netoverview containers keep running | Per-step verify |
| Stale references left behind | MED | 2 600+ `ply` occurrences repo-wide (most in worktrees/build output) | Final grep gate (regex below) on both repos |
| Dashboard red during window | expected | Site unreachable by old names mid-rename | Do Phases 1–3 in one sitting |

---

## Phase 0 — Preflight & backup gates

1. **Connectivity snapshot:** `python scripts/devtool.py test all`;
   `tailscale status` — all 7 ply nodes must be reachable from Miami before
   starting. (Note: `scripts/raspberry-pi/README.md` still says
   ply-raspberrypi is offline since 2026-08-04 — it's active again; fix that
   line in the docs sweep.)
2. **Backup gate (blocks Phase 1 step 7):** fleet has *zero* scheduled
   backups (audit 2026-08-29), so make manual ones now:
   - ply-proxmox: `tar czf /root/pve-etc-YYYYMMDD.tar.gz /etc/pve /etc/hostname /etc/hosts /etc/network/interfaces`, then `devtool.py pull` it off-host. Verify with `tar tzf`.
   - `vzdump 100` and `vzdump 101` to local storage (space permitting). Verify archives exist and are non-zero.
   - HA full backup via API/UI; pull it off the VM.
3. **Tailscale admin check:** confirm whether the 7 machines' tailnet names
   auto-follow the OS hostname or have admin-console overrides — that decides
   whether each rename needs an admin-console rename too.

## Phase 1 — Stage all edits (devices still `ply`)

Prepare every repo/globalnet/netoverview change locally **uncommitted** (or on
a branch) so the broken window in Phase 2 is minimal. Details per repo:

### MiniStackableRack (this repo)
- **`.env`:** section `PLY` → `MIA`; keys `PLY_*` → `MIA_*`
  (RASPBERRYPI_HOST_KEY, HA_URL, HA_TOKEN, NAS_SSH_LOGIN/PW,
  COPYPARTY_PASSWORD, UNIFI_URL/USER/PASSWORD); **values**:
  `MIA_HA_URL=http://mia-homeassistant:8123`,
  `MIA_UNIFI_URL=https://mia-proxmox:8443`.
- **`.env.example`:** same rename + reconcile pre-existing drift (`.env.example`
  has `PLY_WHATSAPP_GROUP_JID`, `PLY_COPYPARTY_BIND_IP/PUID/PGID` that `.env`
  lacks — decide which side is right while in there).
- **`scripts/devtool.py`:** `REGIONS`, `ONLY_AT = {"nas": ["mia"]}`, NAS cred
  key names, `ply-nas` alias handling, docstring examples.
- **Folder renames:** `git mv scripts/synology/ply-synology scripts/synology/mia-synology`;
  plain rename of untracked `scripts/proxmox/ply-proxmox` → `mia-proxmox`.
  Fix internal refs: `copyparty/copyparty.conf`, `copyparty/docker-compose.yml`,
  `copyparty/README.md`, `cast-birdseye.sh`, systemd unit comments.
- **Docs sweep:** `CLAUDE.md`, `README.md`, `REMOTE_ACCESS.md` (sites line,
  "PLY site specifics" → MIA, device tables, §5 drift list),
  `scripts/synology/README.md`, `scripts/raspberry-pi/README.md`,
  `scripts/proxmox/frigate/bnu-frigate/README.md` + `config.yml`
  (comment lines AND `genai.base_url` → `http://mia-desktop:11434`),
  bnu HA `frigate_digest.py` / `frigate_scene_check.py` /
  `frigate_digest.yaml` comments.
- **Diagram:** `systemarchitecture.excalidraw` PLY labels → MIA
  (excalidraw-diagram skill; render to validate). `systemarchitecture_V1.png`
  is a stale snapshot — regenerate or delete.
- **fleet-update skill:** `~/.claude/skills/fleet-update/SKILL.md` and the
  `updateCycles/fleet-update.SKILL.md` copy — site order → `fln→bg→mia→ara→bnu`.

### globalnet
- **`architecture.yaml`:** site `code: ply` → `mia`, name, all `ply_*` ids →
  `mia_*`, `host:`/`web_url:`/`ssh_url:`/`containers_api:`/`gpu_exporter:`
  values, labels/details; **also the bnu section** (Ollama relay labels
  "socat :11434 → mia-desktop").
- **Code:** `app.py` (`WAN_QUALITY_NODES` map, agents map, `PLY_UNIFI_*` env
  prefix usage, comments), `docs/generate.py` `SITE_NAMES`,
  `dashboard-src/src/constants.ts` + `types.ts`,
  `architecture-src/src/WanQuality.jsx` (`WAN_SITES`, `SITE_LABELS`).
- **Tests:** `tests/test_api.py` (site lists, `ply_*` ids, `PLY_HA_TOKEN`
  monkeypatches), `tests/test_architecture.py`, `tests/test_docs.py`.
- **`GlobalNet.xlsx`:** Ply rows → Mia (Region + hostnames), Excel closed,
  then `python export_from_excel.py` → `devices.json`.
- **Docs:** `docs/runbooks/{whatsapp,tailscale,monitoring,frigate}.md`,
  `docs/index.md`, `docs/devices/index.md`, `CLAUDE.md`.
- **Diagram copy:** resync `static/legacy/systemarchitecture.excalidraw` from
  the repo-root diagram (the `classic` make target / manual copy).
- **Builds:** `dashboard-src` + `architecture-src` `npm run build`,
  `make docs`. Don't push yet.

### netoverview
- `deploy_all.ps1`: host-key map key `"ply-raspberrypi"` → `"mia-raspberrypi"`
  (same SHA256 value — the machine key doesn't change).

## Phase 2 — Device renames (one at a time, verify before next)

Order: least critical first, Proxmox last. After each rename: confirm the new
tailnet name appears in `tailscale status`, MagicDNS resolves, SSH works.

1. **ply-glkvm → mia-glkvm** — GL web UI or SSH (dropbear, root).
2. **ply-win11 → mia-win11** — `Rename-Computer -NewName mia-win11 -Restart`
   over SSH (shell is PowerShell); after it's back, `qm set 101 --name mia-win11`
   on the Proxmox host.
3. **ply-desktop → mia-desktop** — Windows rename + reboot. **Immediately
   after:** update the bnu-proxmox socat unit target → `mia-desktop:11434` and
   restart it; deploy the staged bnu-frigate `config.yml` (new `base_url`) and
   restart Frigate. Verify: `curl http://mia-desktop:11434` from bnu-frigate
   and one GenAI-described test event.
4. **ply-nas-ds918plus → mia-nas-ds918plus** — DSM Control Panel hostname;
   Synology Tailscale package should re-advertise. Verify SSH + copyparty
   (binds the Tailscale IP, which doesn't change).
5. **ply-raspberrypi → mia-raspberrypi** — `hostnamectl set-hostname`,
   `/etc/hosts`, restart tailscaled. Verify `http://mia-raspberrypi:5000`
   (netoverview) and `docker ps` (containers unaffected).
6. **ply-homeassistant → mia-homeassistant** — `ha host options --hostname
   mia-homeassistant` via the SSH add-on (password auth, paramiko). Tailscale
   add-on should pick it up (restart add-on if not). Allow Supervisor settle
   time (known-slow after host changes at this site). Verify
   `http://mia-homeassistant:8123` + REST API with the token.
7. **ply-proxmox → mia-proxmox** — the HIGH-risk step, gated on Phase 0
   backups. Standalone node procedure:
   - stop VMs 100 and 101
   - edit `/etc/hostname` + `/etc/hosts` (IP stays `192.168.0.21`); reboot
   - move guest configs `/etc/pve/nodes/ply-proxmox/qemu-server/*.conf` →
     `/etc/pve/nodes/mia-proxmox/qemu-server/`
   - check `storage.cfg` / `jobs.cfg` for node-pinned entries; drop old-node rrd
   - start guests; verify web UI `:8006`, `qm list`, both socket-proxyd relay
     units (birdseye `:8554`/`:1984`, UniFi `:8443`)
   - recovery path if SSH is lost: **mia-glkvm** (renamed in step 1)
8. **Tailscale admin cleanup** — confirm no stale `ply-*` machine entries or
   name overrides remain.

## Phase 3 — Commit, push, deploy

1. Root repo: commit the staged rename (`feat(mia): rename ply site to mia`).
2. globalnet: run the gate — `make docs && make check && make fleet` —
   `make fleet` now diffs against the **renamed live devices** and must exit
   clean. Push `main` → CI → DockerHub → Pi cron redeploys dashboard+docs
   (≤5 min). Bump the globalnet submodule pointer in the root repo.
3. netoverview: push the `deploy_all.ps1` change (setup-only script; no
   fleet impact).

## Phase 4 — Live copies & externals (off-repo)

- **NAS copyparty live compose/env** on mia-nas: if the live files use
  `PLY_COPYPARTY_*` names, update + `sudo -S /usr/local/bin/docker-compose up -d`.
  Root `.env` stays authoritative; update the §5 drift list if anything changed.
- **WhatsApp group** (a `PLY_WHATSAPP_GROUP_JID` exists in `.env.example`) —
  human action: rename the group if it's named PLY; the JID itself is stable,
  only the env key name changes.
- **UniFi UCG Max** display/site name "PLY" — cosmetic, human, optional.
- **mia HA location name** in the HA UI — cosmetic, optional.

## Phase 5 — Whole-architecture verification

- `python scripts/devtool.py test all` — all sites green, `mia` listed.
- `python scripts/devtool.py list mia` — VMs + containers enumerate.
- Dashboard: MIA card green, WAN-quality strip shows MIA, container rows named
  (no italic nameless rows); `/docs` shows `architecture/mia` and no ply page.
- `make fleet` clean (again, post-deploy).
- bnu Frigate GenAI description on a fresh event (via mia-desktop).
- Birdseye cast from mia HA to the Apple TV.
- **Final grep gate**, both repos + skills, expecting hits only in immutable
  history files:
  `rg -i '(^|[^A-Za-z])ply([^A-Za-z]|$)' --glob '!static/**' --glob '!.claude/worktrees/**'`

## Phase 6 — Post

- Update Claude memory: fleet-update site order (`fln→bg→mia→ara→bnu`),
  backup-posture memory references to ply.
- REMOTE_ACCESS.md same-session rule: record anything learned during the
  renames (especially the Proxmox node-rename procedure and the HA/Tailscale
  add-on behavior).
- Append execution log below this line as steps complete.

---

## Execution log

Unattended overnight run, night of 2026-08-30→31 (~00:20 EDT onward), Claude
on mia-desktop (this machine — confirmed: OS hostname `desktop`, tailnet
`ply-desktop`).

**Key discovery that changed the plan:** the `ply-*` tailnet names are NOT the
devices' OS hostnames (those are generic: `desktop`, `raspberrypi`,
`NAS_DS918plus`, `glkvm`, `DESKTOP-82PM8U0`) — they are Tailscale
**admin-console name pins**. Exception: ply-proxmox, whose OS hostname really
was `ply-proxmox` and was never pinned. Consequence: NO OS renames, NO
reboots, NO risky PVE node surgery needed anywhere. The whole cutover is a
console rename of 6 pinned machines.

### Done (unattended)

- **Preflight:** `devtool test all` 21/25 — the 4 fails are the known
  fleet-wide win11 key-auth breakage (guest agent route works). All ply
  devices reachable from Miami.
- **Backups (gate satisfied):** `/etc/pve` + hostname/hosts/interfaces
  tarball pulled off-host to `gitignore/pve-etc-backup-20260831.tar.gz`
  (verified listing); HA full backup `pre-mia-rename-20260831` (30 MB,
  HA + 4 add-ons) verified via `ha backups list`. vzdump skipped — no
  risky node rename in the final plan, `.env` file backup blocked by
  permission rules (original content preserved in session transcript).
- **ply-proxmox → mia-proxmox: LIVE.** `tailscale set --hostname
  mia-proxmox` flipped MagicDNS immediately (no console pin). ply-proxmox no
  longer resolves; mia-proxmox works (devtool password fallback — key auth
  was never authorized from this machine, same as bnu).
- **Advertised hostnames set to mia-*** (future-proofing; DNS still pinned):
  raspberrypi, glkvm, nas (via `/volume1/@appstore/Tailscale/bin/tailscale`,
  piped sudo), win11 (via guest agent), desktop (local). HAOS hostname set to
  `mia-homeassistant` via `ha host options` (add-on re-advertises on next
  restart).
- **VM display names:** `qm set 100 --name mia-homeassistant`, `qm set 101
  --name mia-win11`.
- **Live unit descriptions:** frigate-birdseye/unifi relay units on
  mia-proxmox and `ollama-proxy.service` on bnu-proxmox (its socat target is
  the tailscale **IP** 100.119.15.101, so the desktop rename breaks nothing).
- **Repo sweep: 362 boundary-guarded replacements** (ply→mia / PLY→MIA /
  Ply→Mia / Plymouth→Miami) across 39 files in MiniStackableRack + globalnet
  + netoverview + fleet-update skill (both copies). Folders renamed:
  `ply-synology`→`mia-synology` (git mv), `ply-proxmox`→`mia-proxmox`.
  `.env.example` renamed (incl. `PVE_TOKEN_MIA`). GlobalNet.xlsx: 13 cells;
  `devices.json` regenerated. Excalidraw JSONs validated; devtool/app/tests
  py-compile OK; architecture.yaml YAML-loads.
- **globalnet gates:** pytest **56/56 green** (after cleaning a stale
  generated `docs/architecture/ply.md`); dashboard-src and architecture-src
  npm builds OK; strict Sphinx build into `static/docs` OK (mia pages
  generated, ply pages gone).
- **Live-mirror recon:** bnu-raspberrypi `~/globalnet/.env` has
  `PVE_TOKEN_PLY`, `PLY_HA_TOKEN`, `PLY_UNIFI_URL/USER/PASSWORD` (URL value
  contains ply-proxmox) → rename at deploy. NAS copyparty local `.env` has
  `PLY_COPYPARTY_BIND_IP/PUID/PGID` beside compose; password lives in
  `copyparty.local.conf`.

### Blocked on Eduardo (asked via question in session)

1. **Tailscale console rename of the 6 pinned machines** (glkvm,
   raspberrypi, homeassistant, nas-ds918plus, win11, desktop): needs a
   browser-extension choice the tooling requires the user to make.
2. **Root `.env`**: file is protected from Claude edits by permission
   settings — needs the PLY→MIA key/value rename (one-liner provided).

### Unblocked and executed (~01:15–01:50 EDT, Eduardo answered from phone)

- **Tailscale console renames: DONE (6/6).** Signed into
  login.tailscale.com in Eduardo's Chrome (Google SSO + phone-prompt 2FA by
  Eduardo), renamed the pinned machines: mia-homeassistant, mia-raspberrypi,
  mia-desktop, mia-glkvm, mia-nas-ds918plus, mia-win11. All seven mia-* names
  resolve; every ply-* name is dead.
- **bnu-frigate GenAI cutover: DONE.** Live `/config/config.yml` in LXC 105
  patched (`base_url: http://mia-desktop:11434`), Frigate restarted (active),
  `curl http://mia-desktop:11434` from the LXC → "Ollama is running".
- **Pushes: DONE.** netoverview `a5557e1`, globalnet `a50a431`
  (56/56 tests green pre-push), root repo `6d6ae64` (includes submodule
  bumps + this plan/log + mia-proxmox folder now tracked).
- **Live mirrors: DONE.** bnu-raspberrypi `~/globalnet/.env`: PLY_*→MIA_*,
  `PVE_TOKEN_MIA`, UniFi URL → mia-proxmox (2 leftover *comment* lines keep
  "PLY" — classifier blocked printing the lines; cosmetic). NAS copyparty
  `.env`+compose → MIA_COPYPARTY_*, `docker-compose up -d` → "up-to-date"
  (zero downtime).
- **Gates:** `make fleet` CLEAN (mia_rpi polled via new name; bnu_docker feed
  unreachable from this machine = documented warn). `devtool test all`:
  **19/25** — 4× win11 = pre-existing fleet-wide key-auth issue (guest agent
  route), 2× mia (nas ssh, HA api) = waiting only on the root `.env`
  PLY_→MIA_ one-liner (Eduardo).
- Docs/memory refreshed: REMOTE_ACCESS (rename mechanics, key-auth list +
  mia-proxmox, last-verified line), raspberry-pi README stale offline note,
  memory files (site order fln→bg→mia→ara→bnu; backup-posture names).

### Remaining

1. **Eduardo: run the `.env` one-liner** (renames PLY_→MIA_ keys + 2 hostname
   values) → then `devtool.py test all` should be 21/25 (win11-only fails).
2. Dashboard redeploy verification (DockerHub build + Pi cron, ~10–20 min
   after push): MIA card green at http://bnu-raspberrypi:5001, `/api/wan-quality`
   includes `mia`.
3. Optional/cosmetic follow-ups: PVE node rename ply-proxmox→mia-proxmox
   (attended; procedure in Phase 1 step 7 above — backups already taken);
   restart mia HA Tailscale add-on so it advertises mia-homeassistant;
   2 comment lines on bnu Pi `.env`; WhatsApp group + UniFi display names
   (human); `systemarchitecture_V1.png` stale snapshot (untracked) — delete
   or regenerate.

1. Console renames (6 machines) → verify old names dead, new names resolve.
2. `.env` rename → `devtool.py test all` green as mia.
3. Push bnu-frigate config (`base_url: mia-desktop`) via pct pull/push +
   restart Frigate; verify GenAI on a fresh event.
4. Commit root repo + globalnet + netoverview; push globalnet main (CI →
   DockerHub → Pi cron redeploy); bump submodule pointers.
5. bnu Pi `~/globalnet/.env`: PLY_*→MIA_* + ply-proxmox→mia-proxmox in URL;
   restart dashboard container.
6. NAS copyparty local `.env`+compose var rename + `docker-compose up -d`
   (expect no-op recreate).
7. `make fleet` + `devtool test all` + dashboard/docs spot-check.
8. Memory updates; note WhatsApp-group/UniFi cosmetic renames for Eduardo.
9. Mark Tailscale key-expiry check on renamed nodes (per §6 discipline).
