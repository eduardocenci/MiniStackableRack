# mia-docker — LXC 102 on mia-proxmox

Docker host of the mia rack, created **2026-10-29** for the `senior-*`
containers (see [`senior/`](senior/README.md)).
Counterpart of bnu's LXC 101 (`../bnu-docker/`), but **LAN-only and not on
the tailnet**.

## The LXC

| | |
|---|---|
| VMID / hostname | **102** / `mia-docker` |
| Host | `mia-proxmox` (PVE node name still `ply-proxmox` — rename deferred, see `REMOTE_ACCESS.md` § MIA site specifics) |
| Template | Debian 13.6 standard (trixie). Downloaded on **mia-desktop** and pushed to the host, because mia-proxmox cannot resolve public DNS |
| Type | unprivileged, `features: nesting=1,keyctl=1` (both needed for Docker in an unprivileged LXC) |
| CPU / RAM | 4 cores · 3072 MB + 1024 MB swap |
| Disk | rootfs `local-lvm:vm-102-disk-0`, 40 GB |
| Network | `net0` on `vmbr0`, **static `192.168.2.23/24`**, gw + nameserver `192.168.2.1` (UCG Max), MAC `BC:24:11:47:8C:FF` |
| Boot / TZ | `onboot: 1` · `America/Sao_Paulo` |

**RAM budget.** mia-proxmox has 16 GB, of which the host sees 13925 MiB
(`free -m`). The HA VM (4 GB) and the Win11 VM (4 GB — Eduardo: Win11 keeps
**at least 4 GB**) left about 3.7 GB free, so the LXC is capped at 3 GB.
After the first deploy the host showed 3303 MiB available (2026-10-29). The planned containers
must fit inside this cap, so check `free -m` inside the LXC before adding
any of them.

## Network: LAN-only

- **Not a tailnet node.** `mia-docker` does not resolve anywhere. Reach it:
  - shell: `python scripts/devtool.py guest mia 102 "<cmd>"` (`pct exec` through mia-proxmox);
  - its LAN services: `python scripts/devtool.py lan mia 192.168.2.23 "curl -sS http://192.168.2.23:3000/ping"`;
  - or directly from any machine on the mia LAN (mia-desktop `192.168.2.30` reaches `http://192.168.2.23:3000/` as-is).
- **Inside the LXC tailnet names do not resolve** (the bnu LXC 101 has the
  same limit). Services here must call other hosts by LAN IP.
- **IP plan.** `.23` sits in the Proxmox block (`.20–.29`, `.23+` = LXCs)
  and outside the UCG DHCP pool (`.100–.199`), so no lease can collide. The
  address is set statically in the LXC config. **No UCG reservation exists
  yet** (follow-up): add a fixed-IP client `mia-docker` → MAC
  `bc:24:11:47:8c:ff` → `192.168.2.23`, so the UCG stays the single record of
  the plan (`REMOTE_ACCESS.md` § Fleet IP plan).
- **The central globalnet (bnu-raspberrypi) has no route to the mia LAN.**
  Verified 2026-10-29: `ip route get 192.168.2.23` → `via 10.1.1.2` (the bnu
  EdgeRouter, i.e. the internet), and both ping and HTTP time out. So the
  dashboard never probes `192.168.2.23` directly. The LXC's LED comes from
  netoverview on mia-raspberrypi (MAC above), and its CPU/RAM bar comes from
  the mia-proxmox API (vmid 102). See the globalnet runbook
  `docs/runbooks/senior-acervo-bot.md`.

## Docker

[`install-docker.sh`](install-docker.sh) installs Docker CE from Docker's
official apt repo (trixie). It installed **Docker 29.9.2 + Compose v5.6.1**
on 2026-10-29. It runs once, as root, inside the LXC. The LXC resolves DNS
through the UCG, so apt works here even though it does not on the host.

```bash
# from the repo root on a tailnet machine
python scripts/devtool.py push mia-proxmox scripts/proxmox/docker/mia-docker/install-docker.sh /root/install-docker.sh
python scripts/devtool.py run mia-proxmox "pct push 102 /root/install-docker.sh /root/install-docker.sh"
python scripts/devtool.py guest mia 102 "nohup sh /root/install-docker.sh > /root/install-docker.log 2>&1 &"
python scripts/devtool.py guest mia 102 "cat /root/install-docker.log"
```

(Anything that runs longer than about a minute over `devtool` goes detached.
See `REMOTE_ACCESS.md` § MIA site specifics.)

## Layout: one folder per compose project

| Repo folder | On the LXC | What |
|---|---|---|
| `install-docker.sh` | — | Host preparation (Docker CE + Compose plugin) |
| [`senior/`](senior/README.md) | `/opt/senior/` | Compose project **`senior`**. Running: `senior-waha`, `senior-postgres`. Planned: `senior-n8n`, `senior-mcp`, `senior-sync`, `senior-whisper`, `senior-nocodb` |

Why this differs from `bnu-docker/`, which has one folder per container:
on LXC 101 each container is its own compose project in its own
`/opt/<name>/`. Here the `senior-*` containers form **one** compose project.
They share a network (`senior_default`), a `.env` and a lifecycle, and they
are deployed and rolled back as a unit. So the subfolder mirrors the deployed
directory, which is the compose project, and `senior/README.md` documents
each container in it. Any unrelated service that later moves into this LXC
gets its own sibling folder and its own `/opt/<name>/`.

## Monitoring and drift

- globalnet node `mia_docker` (child of `mia_prx`) has the docker children
  `mia_senior_waha` and `mia_senior_pg`. Each has a box and a Docs button. Their
  health LED stays grey because no probe from bnu can reach them (see
  above).
- **There is no container feed yet.** `containers_api` points to
  `http://192.168.2.23:5005/api/containers`, the port bnu's
  netoverview-agent uses on LXC 101. That agent is **not deployed here**, so
  `make fleet` prints "feed unreachable" for `mia_docker` and does not check
  drift. Until it is deployed, check drift by hand:

  ```bash
  python scripts/devtool.py guest mia 102 "docker ps -a --format '{{.Names}} {{.Image}} {{.Status}}'"
  ```

## Backups

**None scheduled.** mia-proxmox has no vzdump job (`/etc/pve/jobs.cfg`
absent, 2026-10-29), and nothing in the LXC dumps the database. The
application-level backups to set up (pg_dump to the NAS and a tarball of
the WAHA sessions) are listed in [`senior/README.md`](senior/README.md#backups).

## Follow-ups

1. UCG fixed-IP reservation for `.23` (above).
2. Backups (pg_dump → mia-nas, WAHA sessions tarball, optionally a vzdump
   job for LXC 102).
3. A container feed: netoverview-agent on `:5005`. To also give the bnu
   dashboard live rows and a real LED for `senior-waha`, choose one:
   - advertise `192.168.2.0/24` on the tailnet as a subnet route, with
     accept-routes on bnu-raspberrypi (the fleet IP plan already keeps every
     site's /24 distinct for this);
   - or add `systemd-socket-proxyd` forwards on mia-proxmox's tailnet
     address (the pattern already in `../../mia-proxmox/systemd/`).
