# canteiro-presenca — daily obra presence report (WhatsApp)

Every day at **20:00 America/Sao_Paulo** the `canteiro-presenca` container
on bnu-raspberrypi (supercronic — see
[`../docker/canteiro-jobs/`](../docker/canteiro-jobs/); a systemd timer
until 2026-08-29) asks the ara Pi's netoverview who was on the canteiro
Starlink LAN since 00:00 (`GET http://ara-raspberrypi:5000/api/presence`)
and posts the summary to the WhatsApp group via WAHA — same LAN path, env
pattern and test convention as [`../canteiro-watchdog/`](../canteiro-watchdog/).

Everything on that LAN besides the fixed devices is somebody's phone or
laptop, so after excluding the fixed MACs/hostnames (Starlink router,
Intelbras iM9+ camera, the ara Pi itself) the device count approximates
**how many people were at the obra today**; each line shows first→last
sighting of the day ("00:00" = already online at midnight, "agora" =
still online at report time).

## Install (container since 2026-08-29)

Packaging, deploy, env layout and rollback live in
[`../docker/canteiro-jobs/`](../docker/canteiro-jobs/) — this folder owns
only the script and its env shape
([`canteiro-presenca.env.example`](canteiro-presenca.env.example), live
values in `~/canteiro-jobs/env/canteiro-presenca.env`, chmod 600).

`WAHA_URL/KEY/SESSION` are the same credentials canteiro-watchdog uses
(root `.env` `BNU_WAHA_*`); `GROUP_JID` is the family/canteiro group,
`TEST_JID` the Casa SmokeTests group. Unlike the old timer's
`Persistent=true`, supercronic does **not** catch up a run missed while
the Pi was off — a missed 20:00 report is simply skipped.

## Test without pinging the family

```
docker exec canteiro-presenca python3 /app/canteiro-presenca.py --test            # → TEST_JID (SmokeTests)
docker exec canteiro-presenca python3 /app/canteiro-presenca.py --test <chatId>   # → any chat
```

(Verified 2026-08-26 on systemd and 2026-08-29 in the container, HTTP 201.)

The message is the real report prefixed with `[TESTE]`.

## Giving devices real names

Names are now **automatic**: `starlink-names` on the ara Pi
([`../../ara-raspberrypi/starlink-names/`](../../ara-raspberrypi/starlink-names/))
syncs the Starlink router's client names into netoverview nicknames every
5 min, so a phone shows up as "Galaxy-A54-5G"/"A23-de-Edy" minutes after
it first joins the Wi-Fi. Phones use per-SSID random but **stable** MACs,
so the name sticks. Manual renames in the ara netoverview UI
(`http://ara-raspberrypi:5000`, MAC-keyed) always win — the sync never
overwrites them. (Names are display-only: the bnu HA Frigate gate
suppresses on ANY non-fixed device online, nicknamed or not — see
`frigate_whatsapp.py`.)
