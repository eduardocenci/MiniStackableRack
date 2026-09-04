# mia-raspberrypi · netoverview

Network overview for the MIA rack LAN, `http://mia-raspberrypi:5000`.

- Compose lives at `~/netoverview/docker-compose.yml` on the Pi; this copy is the
  committed source. Push it after editing:
  `python scripts/devtool.py push mia-raspberrypi scripts/raspberry-pi/mia-raspberrypi/docker/netoverview/docker-compose.yml netoverview/docker-compose.yml`
  then `python scripts/devtool.py run mia-raspberrypi "cd ~/netoverview && docker compose up -d"`.
- Image updates need nothing: the Pi cron pulls `cenci/netoverview:latest`
  every 5 minutes.

## IP plan (192.168.2.0/24)

`IP_PLAN` gives netoverview the last-octet blocks so every device row carries a
**Category** and the plan shows as chips at the top of the page. The blocks are
the fleet standard; only the site prefix differs (ara `192.168.1`, bg
`192.168.0`, bnu `10.1.1`, mia `192.168.2`, fln `192.168.3`).

| Block | Category | Fixed slots |
|---|---|---|
| `.1` | Gateway | UCG Max |
| `.2–.9` | Network gear | `.2` USW Flex 2.5G, `.3` USW Ultra, `.4` U7 Pro |
| `.10–.19` | Rack | `.10` Pi, `.11` GL KVM, `.12` SLZB-06 Zigbee, `.15` NAS, `.16` NAS VM |
| `.20–.29` | Proxmox | `.20` host, `.21` HA VM, `.22` win11 VM, `.23+` LXCs |
| `.30–.39` | Computers | `.30` desktop wired, `.31` desktop Wi-Fi, `.32` MacBook, `.33` Surface |
| `.40–.49` | Cellphones | `.40` iPhone 17 Pro Max, `.44` Kindle (others need MAC randomization off first) |
| `.50–.59` | IoT power | `.50` HS300 rack, `.51` HS300 #2, `.52` Tapo P316M |
| `.60–.69` | IoT lights | `.60/.61` WLED, `.62` Atomi, `.63` DanceLight |
| `.70–.79` | IoT other | `.70` Venstar, `.71` Apollo AIR-1, `.72` Bambu X1C, `.73` Roborock, `.74` HP printer |
| `.80–.89` | Media | `.80` Apple TV, `.81` QN90F, `.82` Sonos Arc Ultra, `.83/.84` Sonos |
| `.90–.99` | Spare | |
| `.100–.199` | DHCP pool | guests, unreserved phones, anything new |
| `.200–.254` | Retired | not in the pool |

Rule: a device that gets an HA integration, a script or a doc reference by IP
gets a name and a reservation in its block on the UCG first. Reservations are
the single source of truth; no static IPs on the devices themselves (the three
UniFi devices are the exception).
