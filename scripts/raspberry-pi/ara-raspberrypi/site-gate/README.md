# site-gate — the ara stack stays dormant until the Pi is on the ara LAN

Added 2026-09-28 for the replacement unit (`ara-raspberrypi2`: OS hostname
and tailnet name), staged on the **mia** LAN after the
original ara Pi died (offline since ~2026-09-21). Every image is
pulled/built and every config and credential is in place, but **no
container is ever started off-site**:

- netoverview would scan a foreign LAN into the presence DB. That DB feeds
  the 20:00 obra-presence report and the bnu Frigate WhatsApp gate.
- canteiro-timelapse would page Casa SmokeTests about a camera that isn't
  there.
- canteiro-relay coming up on the tailnet would flip the bnu
  `canteiro-watchdog` and the go2rtc chain up and down.

## How it arms

`ara-site-gate.timer` runs `ara-site-gate` 1 min after boot and every 2 min
after that. The gate arms only when both hold:

1. there is a default route via **`192.168.1.1`**;
2. a **Starlink gRPC** port answers: the router at `192.168.1.1:9000` or the
   dish at `192.168.100.1:9200`.

No other site uses `192.168.1.0/24` with a Starlink kit (fleet IP plan).

On arming it:

1. runs `docker compose up -d` for the six stacks, in this order:
   canteiro-relay, dvrip-bridge, starlink-proxy, netoverview,
   starlink-names, canteiro-timelapse;
2. installs the fleet netoverview update cron from `../../README.md`
   (installed only now, so the cron's `up -d` can't start netoverview
   off-site);
3. re-seeds the three hand-set nicknames that died with the old
   netoverview DB: the router, the camera and `iPhone (Jorge)`;
4. writes `/var/lib/ara-site/armed` and disables its own timer;
5. posts one line to **Casa SmokeTests**: ✅ with the running containers, or
   ⚠️ with any stack that failed. It uses the same WAHA path and creds as
   the timelapse failure alerts (`~/canteiro-timelapse/env/alerts.env`).

After that the containers' `restart: unless-stopped` owns them: shed power
cuts need nothing from the gate.

| File | Live copy |
|---|---|
| [`ara-site-gate.sh`](ara-site-gate.sh) | `/usr/local/sbin/ara-site-gate` (755) |
| [`ara-site-gate.service`](ara-site-gate.service) | `/etc/systemd/system/` (oneshot; `ConditionPathExists=!/var/lib/ara-site/armed`) |
| [`ara-site-gate.timer`](ara-site-gate.timer) | `/etc/systemd/system/` (enabled while unarmed) |

## Operation

```bash
ssh eduardocenci@ara-raspberrypi2 "sudo ara-site-gate --check"        # decision only, changes nothing
ssh eduardocenci@ara-raspberrypi2 "journalctl -u ara-site-gate -n 30"  # what it saw / did
ssh eduardocenci@ara-raspberrypi2 "cat /var/lib/ara-site/armed"        # armed timestamp
```

`sudo ara-site-gate --force` arms without the LAN check. It is for
**staging rehearsals only**, with the Pi off the tailnet, so nothing
reaches the bnu consumers or WhatsApp. Run `sudo tailscale down` first,
re-stage right after (below), then `sudo tailscale up`.

Rehearsed on 2026-09-28 on the mia LAN, then reset. All six stacks came up,
the cron was installed, the three nicknames landed in `nicknames2`, and the
marker and timer flip worked. The notify failed only because
`bnu-proxmox` doesn't resolve off the tailnet. A reboot afterwards stayed
dormant with no failed units.

To re-stage the Pi off-site, for example as a spare again:

1. `docker compose down` in each stack dir;
2. `sudo crontab -u eduardocenci -r`;
3. wipe the netoverview volume (`docker volume rm netoverview_netoverview_data`);
4. `sudo rm /var/lib/ara-site/armed && sudo systemctl enable --now ara-site-gate.timer`.
