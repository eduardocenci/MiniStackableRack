# canteiro-presenca — daily obra presence report (WhatsApp)

Every day at **20:00 America/Sao_Paulo** this systemd timer on
bnu-raspberrypi asks the ara Pi's netoverview who was on the canteiro
Starlink LAN since 00:00 (`GET http://ara-raspberrypi:5000/api/presence`)
and posts the summary to the WhatsApp group via WAHA — same LAN path, env
pattern and test convention as [`../canteiro-watchdog/`](../canteiro-watchdog/).

Everything on that LAN besides the fixed devices is somebody's phone or
laptop, so after excluding the fixed MACs/hostnames (Starlink router,
Intelbras iM9+ camera, the ara Pi itself) the device count approximates
**how many people were at the obra today**; each line shows first→last
sighting of the day ("00:00" = already online at midnight, "agora" =
still online at report time).

## Install (as deployed 2026-08-26)

```
canteiro-presenca.py      → /usr/local/bin/canteiro-presenca.py   (chmod +x)
canteiro-presenca.service → /etc/systemd/system/
canteiro-presenca.timer   → /etc/systemd/system/    (enable --now)
canteiro-presenca.env.example → values into /etc/canteiro-presenca.env (chmod 600)
```

`WAHA_URL/KEY/SESSION` are the same credentials canteiro-watchdog uses
(root `.env` `BNU_WAHA_*`); `GROUP_JID` is the family/canteiro group,
`TEST_JID` the Casa SmokeTests group. `Persistent=true` on the timer means
a report missed while the Pi was off goes out at the next boot.

## Test without pinging the family

```
sudo bash -c 'set -a; . /etc/canteiro-presenca.env; set +a; canteiro-presenca.py --test'            # → TEST_JID (SmokeTests)
sudo bash -c 'set -a; . /etc/canteiro-presenca.env; set +a; canteiro-presenca.py --test <chatId>'   # → any chat
```

(`sudo` because `/etc/canteiro-presenca.env` is mode 600 root — systemd
reads it itself for the timer runs; verified 2026-08-26, HTTP 201.)

The message is the real report prefixed with `[TESTE]`.

## Giving devices real names

Names are now **automatic**: `starlink-names` on the ara Pi
([`../../ara-raspberrypi/starlink-names/`](../../ara-raspberrypi/starlink-names/))
syncs the Starlink router's client names into netoverview nicknames every
5 min, so a phone shows up as "Galaxy-A54-5G"/"A23-de-Edy" minutes after
it first joins the Wi-Fi. Phones use per-SSID random but **stable** MACs,
so the name sticks. Manual renames in the ara netoverview UI
(`http://ara-raspberrypi:5000`, MAC-keyed) always win — the sync never
overwrites them. A nicknamed non-fixed device is also what the bnu HA
Frigate gate counts as a "known phone" (see `frigate_whatsapp.py`).
