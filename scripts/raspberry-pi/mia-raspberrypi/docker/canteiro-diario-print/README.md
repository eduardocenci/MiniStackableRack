# canteiro-diario-print — MIA print leg of the Diário de Obra ARA

Prints the day's one-page Diário de Obra (`CeuAzul/Diario/<D>/resumo.pdf`, produced on
bnu-raspberrypi by `canteiro-diario publish`) on the HP OfficeJet Pro 6970 at MIA
(192.168.2.74) through the host CUPS queue `HP_OfficeJet_Pro_6970_03F83E_`
(cups-browsed, driverless). Decisão Eduardo 09/09/2026. Full pipeline:
`scripts/raspberry-pi/bnu-raspberrypi/canteiro-diario/README.md`.

- Weekdays only, every 10 min 21:00→23:50 Brasília; marker `printed-<host>.json` in
  the day's Drive folder prevents a second print; catch-up 07:40 next morning.
- The printer defaults to Letter: `PRINT_OPTIONS=-o fit-to-page` scales the A4 page (~94%).
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

One-time: `env/canteiro-diario-print.env` from the example. Manual test:
`docker exec canteiro-diario-print python3 /app/canteiro-diario.py print --date 2026-09-08 --force`.
