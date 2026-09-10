# canteiro-diario — Diário de Obra ARA (daily site report)

Decisão Eduardo 09/09/2026 ("execute, deploy"), after the hand-made test round of
08/09/2026 (desforma dos baldrames). Every weekday the obra gets a one-page
"Diário de Obra" (PDF + JPG) and a multi-page "completo": what the plan said,
what the cameras show, who was there, what came in, what the group said.

## Pipeline — three stages, two machines and the cloud

```
 20:00  ara-pi   rclone move → Drive Timelapse/ (existing)
 20:10  bnu-pi   canteiro-sunset-compare → Dia de Trabalho montage (existing)
 20:15  bnu-pi   canteiro-diario collect   → Drive CeuAzul/Diario/<D>/pack/ + manifest (public links)
 20:45  cloud    Claude Code routine "Diário de Obra ARA" (home-ara repo, skill
                 .claude/skills/diario-de-obra) reads the pack, writes docs/diario/<D>.md + .json
                 in home-ara and Drive Diario/<D>/diario.json
 20:50→ bnu-pi   canteiro-diario publish (every 10 min): render resumo/completo (Chromium),
                 upload, WhatsApp image → obra group (Cenci Céu Azul Casa-Hangar); no BNU print; marker sent.json
 21:00→ mia-pi   canteiro-diario-print: lp resumo.pdf → HP OfficeJet 6970 (MIA); marker printed-<host>.json
 Fridays         diario.json carries a `semana` block (home-ara skill §7): the renderer adds a one-page
                 "Resumo da semana" — semana.jpg goes to the group as a 2nd image, resumo.pdf becomes
                 daily + weekly (2 pages, both printed in MIA), the completo gets it as last page
```

Weekdays only (`1-5` in every crontab). A weekday holiday still produces a page;
the routine marks it "sem atividade" and `publish` skips WhatsApp/print when the
day has no person events.

Rules that came out of the review (baked into the routine skill, repeated here):
never infer lodging from Wi-Fi presence (the presence API clamps `last_seen` to
the window edge — the pack carries a strict 20:00→05:00 window for that); no
group commentary on the one-pager; the one-pager is exactly one A4 page.

## Files

| File | Role |
|---|---|
| `canteiro-diario.py` | `collect` / `publish` / `print` sub-commands (see module docstring) |
| `diario_render.py` | diario.json + pack → resumo.html/pdf/jpg, completo.html/pdf (Chromium + poppler); Fridays also semana.html/pdf/jpg (`semana` block) |
| `canteiro-diario.env.example` | env template (live: `~/canteiro-jobs/env/canteiro-diario.env`) |
| `../docker/canteiro-jobs/{Dockerfile,compose.yml,crontab-diario}` | container packaging (bnu) |
| `../../mia-raspberrypi/docker/canteiro-diario-print/` | print-only container (mia) |

## Deploy / update (bnu)

```bash
cd MiniStackableRack
for f in canteiro-diario/canteiro-diario.py canteiro-diario/diario_render.py \
         docker/canteiro-jobs/Dockerfile docker/canteiro-jobs/compose.yml docker/canteiro-jobs/crontab-diario; do
  MSYS_NO_PATHCONV=1 python scripts/devtool.py push bnu-raspberrypi "scripts/raspberry-pi/bnu-raspberrypi/$f" "canteiro-jobs/$(basename $f)"
done
python scripts/devtool.py run bnu-raspberrypi "cd ~/canteiro-jobs && docker compose build && docker compose up -d canteiro-diario"
```

One-time: `env/canteiro-diario.env` (from the example + root `.env`), `env/gsa.json`
(the Sheets service-account key, `ARA_FIN_GSA_KEYFILE`), `sudo mkdir -p
/var/lib/canteiro-diario && sudo chown 1000:1000 /var/lib/canteiro-diario`.

## Manual runs (SmokeTests only)

```bash
python scripts/devtool.py run bnu-raspberrypi "docker exec canteiro-diario python3 /app/canteiro-diario.py collect --date 2026-09-08 --force"
python scripts/devtool.py run bnu-raspberrypi "docker exec canteiro-diario python3 /app/canteiro-diario.py publish --date 2026-09-08 --test --force"
python scripts/devtool.py run bnu-raspberrypi "docker exec canteiro-diario python3 /app/canteiro-diario.py alerttest"
```

`--test` sends the image to `TEST_JID`; `--force` ignores weekday/marker checks.
Logs: `docker logs canteiro-diario`. Local state: `/var/lib/canteiro-diario/collect-<D>.json`
(manifest links). Failures → SmokeTests (`ALERT_JID`) with the reason, rc≠0.

## Drive layout (`CeuAzul/Diario/`)

```
manifest-latest.json          stable file (overwritten daily) — the routine's entry point
<D>/manifest.json             counts + file index with public download links
<D>/pack/…                    evidence (see module docstring)
<D>/diario.json               written by the cloud routine (Drive connector)
<D>/resumo.{html,pdf,jpg}  <D>/completo.{html,pdf}
<D>/sent.json  <D>/printed-<host>.json   idempotency markers
```

## Troubleshooting (what already broke)

- **`chromium rc=1: Failed to create headless user data directory container`** (1st automatic
  publish, 09/09/2026 21:13): the container runs as uid 1000, which has no passwd entry in the
  image, so Docker sets `HOME=/` — not writable — and Chromium's new headless mode cannot create
  its temporary profile under `~/.config/chromium`. Fixed twice over: `HOME: /tmp` in
  `compose.yml` and an explicit, throw-away `--user-data-dir` in `diario_render.chromium_pdf`
  (plus a writable HOME for the subprocess). Reproduce/verify inside the container:
  `docker exec canteiro-diario chromium --headless=new --no-sandbox --print-to-pdf=/tmp/t.pdf about:blank`.
- **Cron tick vs manual run**: `publish` takes a per-day `flock` in `STATE_DIR`
  (`publish-<D>.lock`); a second `publish` of the same day logs "outro publish em andamento" and
  exits 0 instead of sending/printing twice. Safe to `docker exec … publish` at any time.
- **Failure alerts**: every rc≠0 posts to `ALERT_JID` (Casa SmokeTests) — check there first;
  WAHA's request log on LXC 101 (`docker logs waha | grep sendText`) is the proof of delivery.
- **Cloud routine cannot `curl` the pack**: the routine's egress proxy blocks
  `drive.google.com` (CONNECT 403); it reads the pack through the Google Drive connector
  instead (recipe in the home-ara skill `diario-de-obra` §1). The public links are still the
  right thing for humans and for the Pi.

## Printers (recorded 09/09/2026)

- **Only MIA prints the Diário** (decisão Eduardo 09/09/2026, from 10/09): `PRINTER=` is empty in
  the bnu env, so `publish` skips `lp` (`sent.json` shows `"print": {"skipped": …}`). The BNU
  queues below stay for manual `lp`.
- BNU: HP Smart Tank 580-590 (10.1.1.143) — CUPS queue `HP_Smart_Tank_580_590_series_ACD97F`
  on bnu-raspberrypi (cups-browsed, driverless). Native formats PCLm/URF/JPEG only — PDFs
  must go through CUPS (`lp`), never raw to :9100.
- MIA: HP OfficeJet Pro 6970 (192.168.2.74) — queue `HP_OfficeJet_Pro_6970_03F83E_` on
  mia-raspberrypi; default media Letter (A4 page printed fit-to-page).
