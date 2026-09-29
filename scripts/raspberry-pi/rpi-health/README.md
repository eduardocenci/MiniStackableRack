# rpi-health — Pi temperature, throttling and SD health on the dashboard

Fleet-wide host service (identical on every Pi, so it lives here rather than
under one `<site>-raspberrypi/`). It feeds the **health row** on each
Raspberry Pi card of the globalnet dashboard:

```
cpu ▬▬░░ 2% of 4c   ram ▬▬▬░░ 1.1/3.8 GiB   sd ▬▬▬░░ 9.5/29 GB
● soc 54°C · pmic 48°C · 11 brownouts/24h · 7h ago                    ↻1d
```

and the WhatsApp alert globalnet sends when a Pi's SD card starts failing
(root filesystem read-only / device errors). Semantics, LED colours and the
alert are documented in globalnet `docs/runbooks/monitoring.md` →
*Raspberry Pi health* and *Fleet alerts*.

## What the Pi already exposed, and what this adds

prometheus-node-exporter (`:9100`, installed on every Pi 2026-07-03) already
carries the **SoC temperature** (`node_thermal_zone_temp{type="cpu-thermal"}`),
root-FS size/free/read-only/device-error, boot time, ARM clock, swap, OOM
kills, NTP sync and eth0 link/error counters — verified on exporter 1.1.2
(mia, bullseye) and 1.9.0 (bg/bnu, trixie). globalnet scraped it for the
cpu/ram bars and dropped the rest; now it parses all of that.

Two things only `vcgencmd` can read (firmware mailbox, `/dev/vchiq`, out of
reach of the netoverview container): the **PMIC temperature** and the
**`get_throttled` flags**. `rpi-health.sh` samples them and writes
`/var/lib/prometheus/node-exporter/rpi.prom` — the exporter's textfile
collector is already active on the Debian package (that is where
`apt.prom` comes from), so nothing else changes.

| Metric | Meaning |
|---|---|
| `rpi_pmic_temp_celsius` | `vcgencmd measure_temp pmic` |
| `rpi_throttled_raw` | `get_throttled` as an integer (bits 0-3 now, 16-19 since boot) |
| `rpi_throttle_active{cause}` | 1 while active: `under_voltage`, `freq_capped`, `throttled`, `soft_temp_limit` |
| `rpi_throttle_since_boot{cause}` | firmware sticky bit |
| `rpi_throttle_last_event_timestamp_seconds{cause}` | epoch of the last event (0 = none on record) |
| `rpi_throttle_events_24h{cause}` / `_7d{cause}` | event counts — the dashboard's 24 h LED. `under_voltage` = brownouts; the other three = **thermal** throttling only: a throttle bit raised while under-voltage is active is the firmware's brownout response and is not logged separately (the card shows `9 brownouts/24h`, not `throttled ×9, undervoltage ×9`) |
| `rpi_health_sampled_timestamp_seconds` | last write; globalnet greys the LED when > 5 min old |

**How the 24 h window works.** The script polls `get_throttled` every 2 s
(the kernel's own hwmon driver polls the same flag at the same cadence). A
rising edge of a "now" bit is an event; a "since boot" bit that appears
without its "now" bit ever being seen is an event shorter than one poll.
Events go to `/var/lib/rpi-health/events` (`<epoch> <cause>`, pruned after
8 days), so counts survive reboots and service restarts. At start it also
seeds under-voltage events from `journalctl -k -b` ("Undervoltage detected!",
spelled "Under-voltage" on kernels before 6.6), which is how bg's 59 brownouts
of the week before install were visible from the first scrape. A since-boot
bit that is already set when the service starts and has no journal entry
(bnu's `0x50000` at install) is **not** an event — it cannot be dated — and
only shows as `since_boot` in the card's tooltip; the 24 h counts hold dated
events only, which is what the LED promises.

Not available on a Pi 4, so not attempted: watts/current (`pmic_read_adc`
is Pi 5 only), RAM temperature (only via the undocumented `vcgencmd readmr`),
PSI pressure (the kernel does not expose it), USB-controller temperature.

## Install / update

```bash
python scripts/raspberry-pi/rpi-health/deploy.py bg mia bnu fln   # rack Pis, via devtool
python scripts/raspberry-pi/rpi-health/deploy.py ara              # plain ssh (ara-raspberrypi2)
```

`deploy.py` tars this folder (LF-normalised), unpacks it to `/tmp/rpi-health`
over SSH stdin and runs `install.sh` as root (`sudo -S` + `RASPBERRYPI_PW` on
fln). Manual equivalent on the Pi: `sudo bash install.sh` from a copy of the
folder. Files land at `/usr/local/sbin/rpi-health` and
`/etc/systemd/system/rpi-health.service`; state in `/var/lib/rpi-health/`.

## Verify

```bash
python scripts/devtool.py run bg-raspberrypi "systemctl is-active rpi-health; curl -s localhost:9100/metrics | grep ^rpi_"
python scripts/devtool.py run bg-raspberrypi "journalctl -u rpi-health -n 20 --no-pager; cat /var/lib/rpi-health/events | tail"
```

The dashboard row appears on the next 30 s poll; `curl -s
http://bnu-raspberrypi:5001/api/node-stats` shows the parsed `health` block
per site.

## Rollout log

| Pi | Installed | Notes |
|---|---|---|
| bg-raspberrypi | 2026-09-29 | 11 under-voltage events / 24 h at install, all ~4 s at `*/5` cron marks → PSU/cable is marginal |
| mia-raspberrypi | 2026-09-29 | clean |
| bnu-raspberrypi | 2026-09-29 | sticky under-voltage bit with nothing in the retained journal → one-off |
| fln-raspberrypi | pending | offline on the tailnet at rollout |
| ara-raspberrypi2 | 2026-09-29 | the replacement Pi had **no node-exporter at all** (only the old unit got it on 2026-08-26) — `apt install prometheus-node-exporter` first, then `deploy.py ara`; clean (`0x0`), PMIC 35 °C |
