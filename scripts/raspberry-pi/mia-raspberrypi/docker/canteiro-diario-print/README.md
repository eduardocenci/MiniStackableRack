# canteiro-diario-print — MIA print leg of the Diário de Obra ARA

Prints the day's one-page Diário de Obra (`CeuAzul/Diario/<D>/resumo.pdf`, produced on
bnu-raspberrypi by `canteiro-diario publish`) on the HP OfficeJet Pro 6970 at MIA
(192.168.2.74) through the host CUPS queue **`HP_OfficeJet_IPP`** (direct
`ipp://192.168.2.74/ipp/print`, driverless `-m everywhere`, added 09/09/2026 — the
cups-browsed class `HP_OfficeJet_Pro_6970_03F83E_` still exists but is not used).
Decisão Eduardo 09/09/2026; since 10/09/2026 MIA is the only site that prints. Full
pipeline: `scripts/raspberry-pi/bnu-raspberrypi/canteiro-diario/README.md`.

- Weekdays only, every 10 min 21:00→23:50 Brasília; marker `printed-<host>.json` in
  the day's Drive folder prevents a second print (the host is called `raspberrypi`, so
  the marker is `printed-raspberrypi.json`); catch-up 07:40 next morning. A per-day
  `flock` in `STATE_DIR` also stops a manual `docker exec … print` from overlapping the
  cron tick (each run spends ~1½ min in rclone before `lp`).
- The printer defaults to Letter: `PRINT_OPTIONS=-o fit-to-page` scales the A4 page (~94%).
  Fridays `resumo.pdf` has 2 pages (day + "Resumo da semana") — both are printed.
- CUPS is reached through the host socket: the compose bind-mounts the **directory**
  `/run/cups` (see Troubleshooting for why not the `.sock` file).
- Failure alerts (rc≠0) go to Casa SmokeTests through the socat relay
  `bnu-proxmox:3001 → WAHA` over the tailnet (`WAHA_URL`/`WAHA_KEY`/`ALERT_JID` in the env,
  same transport as the ara timelapse alerts). Verify from inside the container with
  `python3 -c` + `urllib` against `$WAHA_URL/api/sessions/default` (no curl in the image).
- The script is the same `canteiro-diario.py` as bnu (`print` sub-command); copy it in
  from `scripts/raspberry-pi/bnu-raspberrypi/canteiro-diario/` when deploying.

## Deploy

```bash
cd MiniStackableRack
for f in docker/canteiro-diario-print/Dockerfile docker/canteiro-diario-print/compose.yml docker/canteiro-diario-print/crontab-diario-print; do
  MSYS_NO_PATHCONV=1 python scripts/devtool.py push mia-raspberrypi "scripts/raspberry-pi/mia-raspberrypi/$f" "canteiro-diario-print/$(basename $f)"
done
MSYS_NO_PATHCONV=1 python scripts/devtool.py push mia-raspberrypi scripts/raspberry-pi/bnu-raspberrypi/canteiro-diario/canteiro-diario.py canteiro-diario-print/canteiro-diario.py
python scripts/devtool.py run mia-raspberrypi "cd ~/canteiro-diario-print && docker compose build && docker compose up -d"
```

One-time: `env/canteiro-diario-print.env` from the example (values from the root `.env`,
chmod 600). Manual test:
`docker exec canteiro-diario-print python3 /app/canteiro-diario.py print --date 2026-09-08 --force`.
Long `docker exec` runs through `devtool run` (120 s read timeout): `nohup … > /tmp/x.log 2>&1 < /dev/null &`.

## Troubleshooting (what already broke)

- **Every print tick fails with `lp: Error - The printer or class does not exist.` while
  `lpstat -p HP_OfficeJet_IPP` on the host is fine** (10/09/2026, 21:30→22:30 BRT, first
  unattended day): `lpstat -r` inside the container said *scheduler is not running*. The
  host cupsd is restarted every midnight by logrotate (`/etc/logrotate.d/cups-daemon`,
  `postrotate invoke-rc.d cups restart`) and re-creates `/run/cups/cups.sock`; the compose
  bind-mounted the socket **file**, so the container kept the dead inode from the day before
  (it had worked on 09/09 only because the container was recreated after that midnight).
  `lp -d X` reports "does not exist" for any failure to look X up, including no scheduler.
  Fix: mount the directory `/run/cups:/run/cups` (`network_mode: host` would also allow
  `CUPS_SERVER=localhost:631`). `do_print` now appends the `lpstat -r` verdict to the alert.
  Verify: `docker exec canteiro-diario-print lpstat -r` → *scheduler is running*.
- **Failures were silent**: the env shipped with empty `WAHA_URL`/`ALERT_JID` ("alerta nao
  configurado" in `docker logs`) — MIA cannot reach the bnu LAN, but the tailnet relay
  `bnu-proxmox:3001` can (HTTP 200, session WORKING, 0.5 s from the container). Filled on
  10/09/2026 (regra Eduardo 04/09/2026: nothing fails silently).
- `docker compose build` on this 32-bit userland takes seconds when only the `COPY` layer
  changed; a full rebuild (apt layer) needs `nohup` + polling.
