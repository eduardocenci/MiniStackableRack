---
name: fleet-update
description: Run a full MiniStackableRack fleet update wave — plan with risk-tiered tables and the 2-week age rule, execute site-by-site (fln→bg→mia→ara→bnu) with backup gates, per-step verification, live dashboards, and an append-only log; finish with summary, lessons, and wave-2 scheduling. Use when Eduardo asks to update the fleet, HA instances, or run an update wave/cycle.
---

# Fleet Update Wave

Reference run: `updateCycles/20260828_wave1.md` (+ `20260828_wave1_timings.json` — use its actuals as the
next cycle's estimates). This file is the canonical skill; an identical copy belongs at
`.claude/skills/fleet-update/SKILL.md` so it is invocable.

## 1. Plan (present, then wait for explicit "go")

1. Survey the WHOLE architecture: 4× HA (core/OS/supervisor/add-on/HACS update entities via
   `devtool.py ha <site> POST /api/template` over `states.update`), 4× Proxmox hosts (`pveversion`, apt),
   5× Pis (apt, docker, tailscale), LXC apps (Frigate, Plex, WAHA stack), NAS, GL-KVMs, Win11 (QEMU guest
   agent — SSH key auth to win11 is broken). Capture config-entry states per HA site = verification baseline.
2. Web-research EXACT release dates + changelogs for every pending item. **2-week minimum age rule**: only
   install releases ≥14 days old; younger ones go to a dated Wave 2. Read every changelog against the site's
   integrations; eligible-but-buggy versions may be skipped with the reason highlighted (e.g. fr24 v2.1.0
   recorder bug → jump to v2.1.1).
3. Tables: current→target with release ages → risk≥medium (own table) → low risk → Wave 2 (dated) →
   excluded items (each with its reason).
4. Dashboards — 2 artifacts: main page + 32:9 wallboard (Samsung Odyssey Neo G9 nominal res, everything
   visible without scrolling). Conventions:
   - globalnet `architecture.yaml` node-id tag on every step (`bnu_ha`, `bg_plex`, `mia_pi`…)
   - per-step start time + est vs actual minutes; **Elapsed = wall-clock since go**, never sum of actuals
   - yellow only WHILE a step misbehaves; once recovered → green dot + yellow text remark
   - live feed window ONLY inside the actively running step's box (one per step → ready for parallel runs);
     source is the MONITOR's own stdout via `stream_tail.py --file` (covers reboot windows); local-time
     stamps at line start; site window below = command history
   - state in `run_state.json`, injected by `patch_dash.py` between `/*STATE*/…/*ENDSTATE*/` markers in both
     HTMLs; republish both artifacts after every step (~90s cycles during long steps); 60s page auto-refresh
5. Log `updateCycles/YYYYMMDD_<name>.md`: Summary placeholder at TOP (total time, issues, components w/
   versions — filled at the end), append-only Execution log LAST. Copy `run_state.json` to
   `updateCycles/YYYYMMDD_<name>_timings.json` when done.

## 2. Execute (unattended: fix, retry, fall back; roll back only the failed step; never leave a site broken; don't stop to ask)

Site order **fln → bg → mia → ara → bnu** (bnu last — heart of the architecture).
Per site: backup gate → HA core → HAOS → add-ons/HACS one at a time → site apps (LXCs) → Pi → host LAST.

**Backup gate (verified to exist before anything updates):** `ha backups new` FIRST, then
`qm snapshot 100 pre-w1`. Order matters — the snapshot's fsfreeze hook can freeze Supervisor and block the
backup ("system is not running - freeze"); if it happens: `ha backups thaw`, retry. `pct snapshot <id>`
before each LXC app. All rollback points live on local-lvm.

**Helper scripts** (recreate in scratchpad from the wave-1 session if lost): `pi_upgrade.py` (detached w1apt
unit → reboot → verify, 3 tries), `host_upgrade.py` (same for Proxmox + guests/HA verify),
`stream_tail.py` (journal `-o short-unix` or `--file` monitor-tail → step feed → patch → publish),
`w1frig.sh` pattern for in-LXC app tarball swaps with `.bak` retention.

**Hard-won rules (each cost real time in wave 1):**
- Long `ha` CLI ops: FIRE-AND-POLL — the SSH channel recv-times-out ~2 min while the server-side op
  continues. `ha` needs `bash -lc` (SUPERVISOR_TOKEN comes from the login shell); REST tokens 401 on
  `/api/hassio/*`; strip the "addons→apps" deprecation banner (parse from first `{`) and use `raw_decode`.
- `ha os update` only STAGES the new slot. NEVER reboot until Supervisor raises the **reboot_required**
  issue — slow WANs stage late (mia rebooted early once and came back on the old OS).
- After any `ha host reboot`, Supervisor blocks add-on/OS ops for ~5 min ("startup") — wait, then retry.
- apt on hosts/Pis: detached `systemd-run --unit=w1apt` ALWAYS (ssh/tailscale restarts can't kill dpkg);
  `DEBIAN_FRONTEND=noninteractive` + confdef/confold; password-sudo compound commands must be wrapped
  `sudo bash -c "…"`; apt can upgrade `sudo` itself mid-run and break passwordless sudo → monitors always
  use piped-password `sudo -S -p ''`.
- Monitors: retry transient SSH errors (banner drops / WinError 10054 under apt load, 3×); catch
  **BaseException** — devtool `sys.exit()`s (SystemExit) on connect timeout and a bare `except Exception`
  lets the monitor die mid-reboot; poll deadline ≥60 min for 400+-pkg Pis; resume-aware firing
  (`is-active` precheck; treat "already exists"/"already loaded" as running → attach). Background work ONLY
  via tracked mechanisms — never shell `&` (orphan monitors double-fire reboots).
- Pi kernels can need a SECOND reboot to activate — verify `uname -r`, reboot again if stale.
- Before a host reboot, check guest `onboot` flags: every running guest =1; stopped guests intentionally 0.
- mia WAN is the fleet's slowest (~2GB HAOS core image ≈ 10 min, host apt ≈ 20 min DL) — pre-download
  (`apt-get -d`, fire `ha core update` early) during other sites' windows next time.
- WhatsApp tests ONLY to the SmokeTests group (`SMOKETESTS_WHATSAPP_GROUP_JID`); send paths:
  bnu `rest_command.alertablu_send_whatsapp`, bg `rest_command.send_whatsapp_bg`.
- Sanitize U+FFFD (`�`) out of anything flowing into artifact HTML — the deploy 400s on it.
- Known blocker: **Frigate ≥0.17.2 bare-metal needs Python ≥3.12** (LXC 105 has 3.11 — asyncio.SubprocessError
  crash); release tarballs also lack the build-generated `frigate/version.py`. Until the LXC is migrated to
  a py3.12 base (or Frigate dockerized), Frigate updates fail → snapshot + keep old tree at `.bak`, roll
  back on failure, mark step red with note.

**Per-step verify (before marking done, no exceptions):** HA core → API version + config-entry diff vs
baseline (pre-existing failures stay excluded) + `ha core logs`; HAOS → version + active boot slot;
add-ons → running + function probe (ingress page / tracker data / device scan); Plex → `:32400/identity`;
WAHA stack → containers Up + SmokeTests delivery on both paths; Frigate → `/api/version` + `/api/stats`
detector fps; Pi → containers auto-restarted + tailscale up + kernel; host → pveversion, kernel, expected
guests running, HA API answering.

## 3. Parallelization plan (observed in wave 1 — apply next cycle)

- All 4 backup gates upfront, in parallel (they don't touch what other sites update).
- Each Pi in parallel with its own site's HA stack (independent devices).
- Overlap two site chains: start next site's HA stack while current site is in its host-upgrade phase;
  bnu still strictly last.
- Pre-stage mia's slow downloads during earlier sites' windows.
- Wave-1 sequential total 4h00m → projected **~1h45m**.

## 4. Close

Fill the log Summary (total + per-site times, components with final versions, issues, parallelization
notes) · copy timings JSON · final dashboard state (run pill green "Run finished", feeds cleared, final
console line) · snapshots kept 48h soak — deletion only on Eduardo's explicit ok · announce Wave 2 (items
that were <14 days old, with the dates they become eligible) · record any new access/behavior learnings in
`REMOTE_ACCESS.md` and memory in the same session.

## Wave-1 timing reference (est → actual, minutes)

fln 45→42 · bg 55→68 (417-pkg Pi apt + fsfreeze incident) · mia 55→64 (slow WAN + early-reboot redo) ·
ara 10→10 · bnu 75→55. Per-step actuals: `20260828_wave1_timings.json`.
