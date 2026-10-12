# canteiro-presenca — daily obra presence report (WhatsApp)

Every day at **20:00 America/Sao_Paulo** the `canteiro-presenca` container
on bnu-raspberrypi (supercronic — see
[`../docker/canteiro-jobs/`](../docker/canteiro-jobs/); a systemd timer
until 2026-08-29) asks the ara Pi's netoverview who was on the canteiro
Starlink LAN since 00:00 (`GET http://ara-raspberrypi2:5000/api/presence`)
and posts the summary to the WhatsApp group via WAHA — same LAN path, env
pattern and test convention as [`../canteiro-watchdog/`](../canteiro-watchdog/).

Everything on that LAN besides the fixed devices is somebody's phone or
laptop, so after excluding the fixed MACs/hostnames (Starlink router,
Intelbras iM9+ camera, the ara Pi itself) the device count approximates
**how many people were at the obra today**; each line shows first→last
sighting of the day ("00:00" = already online at midnight, "agora" =
still online at report time). Devices outside `LAN_CIDR`
(default `192.168.1.0/24`) are dropped too — defense in depth against
the Docker-bridge `172.17.0.x` ghosts the netoverview scanner used to
register during a `docker build` on the ara Pi (root-fixed in
netoverview `d7f312a`, 31/08/2026).

## After-hours block (since 2026-10-11)

Part of the crew has been sleeping at the canteiro since the week of
05/10/2026 (first night on the network: 07→08/10, 4 devices), so the report
also carries a **🌙 Fora do expediente** block: the devices seen on the LAN
during the last *complete* night window — default **20:00 → 05:00**
(`NIGHT_START_H` / `NIGHT_END_H`, BRT hours), the same strict window the
Diário de Obra pack uses (`prev_night_20_05`), so late leavers (crew is gone
by ~17:00) and 06:00 arrivals don't count. Tonight's window only closes
tomorrow at 05:00, so the 20:00 report shows **last night** plus a preview
line: devices seen between 19:00 and 20:00 today.

Per device it only says *already on at 20:00* or *arrived HH:MM* — never
until when: sleeping phones drop and rejoin the Wi-Fi all night (night of
08→09/10: 6, 9 and 11 online/offline transitions on 3 of the 4 devices),
and `/api/presence` clamps `last_seen` to the window edge.

```
🌙 *Fora do expediente — noite de 08→09/10, 20:00 às 05:00*
*4* aparelhos de pessoas ficaram na rede do canteiro:
• A23-de-Edy — já na rede às 20:00
• Redmi-13C — já na rede às 20:00
…
Na rede entre 19:00 e 20:00 de hoje: *0* — prévia da noite de hoje
```

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

To only look at the report without sending anything (no WAHA credentials
needed — it just reads `/api/presence`):

```
docker exec canteiro-presenca python3 /app/canteiro-presenca.py --dry-run
```

## Giving devices real names

Names are now **automatic**: `starlink-names` on the ara Pi
([`../../ara-raspberrypi/starlink-names/`](../../ara-raspberrypi/starlink-names/))
syncs the Starlink router's client names into netoverview nicknames every
5 min, so a phone shows up as "Galaxy-A54-5G"/"A23-de-Edy" minutes after
it first joins the Wi-Fi. Phones use per-SSID random but **stable** MACs,
so the name sticks. Manual renames in the ara netoverview UI
(`http://ara-raspberrypi2:5000`, MAC-keyed) always win — the sync never
overwrites them. (Names are display-only: the bnu HA Frigate gate
suppresses on ANY non-fixed device online, nicknamed or not — see
`frigate_whatsapp.py`.)
