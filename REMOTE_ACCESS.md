# REMOTE_ACCESS.md — how to reach every device

**Single source of truth for reaching and managing anything in the fleet.**
If you are an LLM: read this file, then use `scripts/devtool.py`. Do not try
several methods until one works — every working method is recorded here, and
every method *not* listed here has already been tried and failed.

Last verified end-to-end: **2026-07-30** — `python scripts/devtool.py test all`
→ **25/25 OK**.

---

## 1. The one rule

```bash
python scripts/devtool.py test all
```

`scripts/devtool.py` encodes every credential lookup, username, and per-device
quirk below. Prefer it over hand-rolled `ssh`/`curl`. It is safe to run from any
directory.

| Need | Command |
|---|---|
| Check everything is reachable | `devtool.py test all` (or `test bnu`, `test bg-win11`) |
| Run a command on a device | `devtool.py run bnu-proxmox "qm list"` |
| Copy a file to a device | `devtool.py push bg-win11 ./x.ps1 C:\temp\x.ps1` |
| Copy a file from a device | `devtool.py pull ply-raspberrypi /etc/hostname ./h.txt` |
| Home Assistant REST call | `devtool.py ha bnu GET /api/states/sun.sun` |
| Inventory a rack's VMs/LXCs/containers | `devtool.py list bnu` |
| Run a command **inside** a VM or LXC | `devtool.py guest bnu 101 "docker ps"` |
| Reach a **LAN-only** device (§2) | `devtool.py lan bnu 10.1.1.132 "curl -sS http://10.1.1.132/"` |

## 2. Network layer — and what is *not* on it

Tailscale is the primary network. Tailnet nodes are named exactly
`<region>-<component>`, and this machine is itself a node, so **those devices are
reachable by bare name** (MagicDNS) with no VPN or port forwarding.

- Rack sites: **bnu, ply, bg, fln** (four — docs listing only three are stale).
  Home builds use the same naming (`ara-raspberrypi`) but are not rack sites.
  Personal clients (`cenci-surface9`, `cenci-macbook`, `iphone-…`) don't follow
  the pattern.
- `tailscale status` lists nodes and their `100.x` IPs. **A name resolving does
  not mean the device is up** — offline nodes still resolve.

### ⚠ Not every device is on the tailnet

This is the single most common cause of wasted attempts. **27 of 37 registered
devices are tailnet-addressed; 10 are LAN-only and their bare names do not
resolve at all.** Do not try `ssh bnu-docker` or `curl http://bnu-zigbee` —
those names do not exist.

| LAN-only device | Address | What it is |
|---|---|---|
| `bnu_docker` | `10.1.1.126` | **LXC 101** — hosts waha, waha-listener, condfy-bridge, netoverview-agent |
| `bnu_waha` / `bnu_listener` / `bnu_nta` | `10.1.1.126:3000/:8788/:5005` | the containers inside LXC 101 |
| `bnu_ollama` | `10.1.1.50:11434` | Ollama LXC 106 |
| `bnu_frigate` | `10.1.1.160` | Frigate LXC 105 — *also* a tailnet node (`bnu-frigate`), so either route works |
| `bnu_zb` | `10.1.1.132` | SLZB-06 Zigbee gateway (same pattern at other sites) |
| `bnu_nvr` | `192.168.0.22` | Hikvision NVR + 8 cameras |
| `bnu_doorbell` | `10.1.1.65` | Hikvision doorbell (ISAPI) |
| `bnu_gw` | `10.1.1.2` | EdgeRouter |

Reach them by hopping through the site's Proxmox host, which sits on both
networks — `devtool.py lan` does exactly that:

```bash
python scripts/devtool.py lan bnu 10.1.1.132 "curl -sS --max-time 10 http://10.1.1.132/"
python scripts/devtool.py lan bnu 10.1.1.126 "curl -sS http://10.1.1.126:8788/health"
```

Because these are LAN-addressed, their IPs are load-bearing and *do* appear in
config. Everywhere else, prefer tailnet names over IPs — names are stable, LAN
IPs are not.

**LAN-only devices cannot use tailnet names to reach each other.** MagicDNS
resolves only on tailnet nodes, so inside LXC 101 `getent hosts bnu-win11`
fails. When configuring one LAN-only service to call another host, use the LAN
IP and add a DHCP reservation — do not "improve" it to a tailnet name.

**Reaching LXC 101's HTTP services off-LAN (finance-hangar wa-sweep off-site).**
`devtool.py lan` is one-shot; a client that makes MANY HTTP calls
(`finance.listener_client` pulling a WhatsApp archive + media) needs a real
port-forward through `bnu-proxmox`. Plain `ssh -L` may fail (key auth — see
Tooling constraints), so use a **paramiko forwarder** (SSHClient with
`PROXMOX_PW` fallback + `transport.open_channel("direct-tcpip", …)` behind a
ThreadingTCPServer) forwarding BOTH ports — `18788 → 10.1.1.126:8788`
(waha-listener) **and** `13000 → 10.1.1.126:3000` (WAHA gateway: media-rescue
fallback AND `waha_send` group replies both hit it) — then run the pipeline
with `BNU_WAHA_LISTENER_URL=http://127.0.0.1:18788
BNU_WAHA_API_URL=http://127.0.0.1:13000` (`finance.config.cfg` lets env vars
override `.env`). Forwarding only 8788 makes every media fetch hang ~2 min in
the WAHA fallback before failing (seen 2026-08-26 from ply-desktop). Google
Sheets/Drive APIs need no tunnel. Note the Drive-for-Desktop mount and the
ms365 MCP are NOT available on every machine — a run without them files
sheet+local archive and leaves Drive uploads/share links as checklist items
(see the finance-hangar/ingest skills' degraded modes).

### ARA (home build — not a rack site, not in devtool.py)

`ara-raspberrypi` is a tailnet node (the "computadorzinho" in the canteiro
shed): reach it with plain `ssh eduardocenci@ara-raspberrypi` (key auth since
2026-08-24; COMMON `RASPBERRYPI_LOGIN/PW` is the password fallback via
paramiko). `devtool.py` does **not** know ara — `devtool.py lan` cannot hop
here; hop manually through the Pi for the ara LAN-only devices. The Pi runs
the standard netoverview container (since 2026-08-26): what is on the house
LAN is visible at `http://ara-raspberrypi:5000` without SSH:

| ARA LAN-only device | Address | What it is |
|---|---|---|
| Intelbras iM9+ Full Color camera | `192.168.1.56` | dual-lens canteiro camera — RTSP `:554` (Digest, `admin` + `ARA_CANTEIRO_CAM_KEY`), ONVIF/CGI `:80`, relayed to the tailnet by `mediamtx` on the Pi (`rtsp://ara-raspberrypi:8554/canteiro`). Each lens also has a 640×480 substream (`subtype=1`, not relayed yet). ONVIF **events work** (PullPoint, probed 2026-08-26): topics are motion/tamper/scene-change only — **no person/vehicle classification locally** (that stays in the Imou cloud/Mibo app; CGI remains 401) |
| Starlink router | `192.168.1.1` | house LAN gateway (DHCP for the whole `192.168.1.0/24`) |

### Re-authentication
Key expiry is disabled on every node, and every Windows node runs Tailscale in
**unattended mode** so the tunnel survives reboot without a desktop login
(`tailscale set --unattended`; verify with `tailscale debug prefs` →
`"ForceDaemon": true`). Without it a Windows node comes back `Logged out`. See §6.

## 3. Access matrix

Credentials are key names from the repo-root `.env` (gitignored) — never values.
"Key auth" means this machine's `~/.ssh/id_ed25519` is authorized on the device,
so plain `ssh` works non-interactively.

| Device | Host name | LLM interface (in priority order) | User | Auth | Notes |
|---|---|---|---|---|---|
| Proxmox | `<region>-proxmox` | SSH → web `https://<host>:8006` | `root` | **key**, else `PROXMOX_PW` | SFTP OK. Gateway to all guests (§4) |
| Home Assistant | `<region>-homeassistant` | **REST API** → SSH add-on → web `:8123` | `hassio` | REST: `<REGION>_HA_TOKEN`; SSH: `HA_SSH_PW` **password only** | Add-on SSH has **no key auth** and **no SFTP**; `/config` needs `sudo` → `push` uses `base64 -d \| sudo tee` |
| Windows 11 VM | `<region>-win11` | SSH → guest agent (§4) → RDP | `eduardocenci` | **key** | Default shell is **PowerShell**. No SFTP — `push`/`pull` go through base64 |
| Raspberry Pi | `<region>-raspberrypi` | SSH | `eduardocenci` | **key**, else `RASPBERRYPI_PW` | SFTP OK. `sudo` is passwordless on bnu/bg but **asks a password on fln** (seen 2026-08-26) — plain `docker` works everywhere (user in `docker` group); for root-only cmds on fln pipe the password: `devtool.ssh_run(dev, "sudo -S <cmd>", input_bytes=(ENV["RASPBERRYPI_PW"]+"\n").encode())` |
| GL-KVM | `<region>-glkvm` | SSH → web `http://<host>` | `root` | **key**, else `GLKVM_PW` | Runs **dropbear**: keys live in `/etc/dropbear/authorized_keys`, not just `~/.ssh`. SFTP may fail → devtool falls back to base64 |
| Synology NAS | `ply-nas-ds918plus` (alias `ply-nas`) | SSH → DSM web `:5000` | `PLY_NAS_SSH_LOGIN` | **key**, else `PLY_NAS_SSH_PW` | Only at ply. Docker still needs root: `echo $PW \| sudo -S docker …`; compose is v1 at `/usr/local/bin/docker-compose` |

**Rule:** use the highest-priority interface that works, and fall back down the
list. Never open a browser unless every CLI/API option is exhausted.

### Tooling constraints on this machine
- `plink` and `sshpass` are **not installed** — do not use them.
- OpenSSH (`ssh`, via Git Bash) works for key auth; **paramiko** (installed) is
  the only way to do non-interactive password auth. `devtool.py` handles both.
- **Key auth to `bnu-proxmox` FAILS from ply-desktop** (2026-08-26: plain
  `ssh root@bnu-proxmox` → "Permission denied (publickey,password)" — the
  pubkey is not in its authorized_keys). `devtool.py` still reports OK because
  paramiko silently falls back to `PROXMOX_PW`. Re-authorize the key or keep
  using the password path; `ssh -L` tunnels need the paramiko forwarder (§2).
- Finance-pipeline Python deps (`gspread`, `google-api-python-client`, `msal`,
  `faster-whisper`) installed on ply-desktop 2026-08-26 — local finance/ingest
  runs work here, but this machine has NO Drive-for-Desktop mount (no `G:`)
  and no ms365 MCP: Drive uploads and OneDrive share links defer to a
  mounted/Graph-capable run.
- Plain `ssh`/`scp` fail with "Host key verification failed" for hosts not yet
  in Git Bash's `known_hosts` (seen 2026-08-24 with `bnu-proxmox`); `devtool.py`
  is immune (paramiko `AutoAddPolicy`). Use `devtool.py pull`, not `scp`, to
  copy files off a device.
- Git Bash mangles remote paths starting with `/` — prefix with
  `MSYS_NO_PATHCONV=1` when passing them to a remote command.
- `push` (paramiko SFTP) can stall or drop mid-batch on a loaded host — seen
  2026-08-09 on bnu-proxmox at load ~22, where three files landed and the
  fourth hung until timeout. Reliable fallback for text files: base64 the
  content into a `run` command (`echo <b64> | base64 -d > /path`), which goes
  over the already-open exec channel instead of opening an SFTP subsystem.
- Non-ASCII in remote output (accents, emoji) used to crash `devtool.py` on
  this machine's cp1252 stdout. Fixed 2026-08-09: `main()` reconfigures
  stdout/stderr to UTF-8 with `errors="replace"`, so no `PYTHONIOENCODING`
  prefix is needed. Undrawable glyphs render as `?` instead of losing the
  command's whole output.

## 4. Guests: VMs, LXCs and containers

Guests other than the Win11 VMs (and `bnu-frigate`) have **no tailnet name of
their own** — reach them through their Proxmox host.

```bash
python scripts/devtool.py list bnu              # VMs + LXCs + docker in each LXC
python scripts/devtool.py guest bnu 101 "docker ps"      # LXC  -> pct exec
python scripts/devtool.py guest bnu 103 "Get-Process"    # VM   -> QEMU guest agent
```

- **LXC** → `pct exec <vmid> -- …` from the host.
- **VM** → **QEMU guest agent**, enabled fleet-wide on 2026-07-30. `devtool.py
  guest` picks PowerShell for Windows guests and `/bin/sh` for Linux ones, and
  polls `qm guest exec-status` for commands that outlive `qm`'s wait window.
- LXCs do **not** use the QEMU agent — Proxmox talks to containers directly.
- Docker containers live inside LXC 101 on bnu (`waha`, `waha-listener`,
  `condfy-bridge`, `netoverview-agent`) and on each Raspberry Pi.

Current guest inventory (2026-07-30):

| Rack | VMs | LXCs |
|---|---|---|
| bnu | 100 homeassistant, 102 ubuntu (stopped), 103 Win11 | 101 docker, 104 watchyourlan (stopped), 105 frigate, 106 ollama (stopped) |
| ply | 100 ply-homeassistant, 101 Win11 | — |
| bg | 100 haos, 101 ubuntu, 103 Win11 | 102 plex |
| fln | 100 haos, 103 Win11 | — |

## 5. Credentials

All values live in the repo-root `.env` (gitignored); `.env.example` mirrors its
structure with placeholders and must be kept in sync.

- **Shared-by-device-type keys are unprefixed** and sit in a `COMMON` section:
  `PROXMOX_LOGIN/PW`, `RASPBERRYPI_LOGIN/PW`, `GLKVM_LOGIN/PW`, `HA_SSH_LOGIN/PW`.
  The same credential works on that device type at every site.
- **Site-specific keys are region-prefixed**: `BNU_`, `PLY_`, `BG_`, `FLN_`
  (e.g. `BNU_HA_TOKEN`). ARA (the house build) has its own section.
- Do not guess key names from the region convention alone — the most basic
  logins are in the unprefixed COMMON section.

**Credential mirrors** (drift points — the root `.env` is authoritative, but
these hold live copies): `bnu-raspberrypi:~/globalnet/.env`, HA `secrets.yaml`,
the NAS compose `.env` + `copyparty.local.conf`, on LXC 101
`/opt/waha/docker-compose.yml` (hardcoded), `/opt/waha-listener/.env`,
`/opt/condfy-bridge/.env`, and `ara-raspberrypi:/etc/mediamtx/mediamtx.yml`
(camera `ARA_CANTEIRO_CAM_KEY` embedded in the source URLs).

## 6. Tailscale: preventing re-authentication

Two independent things can force a re-login. Both are now handled:

1. **Node key expiry** — a node's key expires (default 180 days) and the node
   drops off until someone re-authenticates. Fixed by disabling key expiry
   per-machine in the admin console (Machines → ⋯ → *Disable key expiry*).
   It is a **per-node** setting: the account-level default does not retroactively
   apply, so check new nodes as you add them.
2. **Windows user-session binding** *(this is what hit fln-win11)* — on Windows,
   Tailscale runs in the interactive user's session by default, so after a
   reboot with nobody logged into the desktop, `tailscaled` comes up profileless
   and reports `Logged out` **even with expiry disabled**. Fixed with unattended
   mode, set on all four Win11 VMs on 2026-07-30:

```bash
ssh eduardocenci@bnu-win11 "& 'C:\Program Files\Tailscale\tailscale.exe' set --unattended"
```

Verify: `tailscale debug prefs` → `"ForceDaemon": true`.
Linux nodes run `tailscaled` as a system service and are unaffected.

Audit expiry across the fleet:

```bash
tailscale status --json | python -c "import json,sys; d=json.load(sys.stdin); [print(p.get('HostName'), p.get('KeyExpiry') or 'disabled') for p in d['Peer'].values()]"
```

**For new nodes:** join with a reusable, non-expiring auth key
(`tailscale up --auth-key …`) and disable key expiry on the machine immediately,
before it is relied upon.

## 7. Known gaps

- **HA add-on SSH has no key auth** — password (`HA_SSH_PW`) via paramiko is the
  only non-interactive route. Left as-is deliberately: REST is the preferred
  interface, so changing add-on config buys little.
- **fln has no folders under `scripts/`** — the site exists in `devtool.py`,
  `.env`, and `globalnet/architecture.yaml`, but not in the docs tree.

Closed on 2026-07-30: `FLN_HA_URL`/`FLN_HA_TOKEN` added (fleet test is now
25/25), and `FINANCE_NOTIFY_URL` on LXC 101 repointed from the dead
`10.1.1.48` to bnu-win11.

> **Why that one is an IP and not a tailnet name:** LXC 101 is not a tailnet
> node, so MagicDNS names do **not** resolve inside it — `getent hosts
> bnu-win11` fails. LAN-only hosts must address each other by LAN IP
> (`http://10.1.1.127:8799/`, verified from inside the `waha-listener`
> container). The "prefer tailnet names" rule applies only where the tailnet is
> actually reachable. If bnu-win11's DHCP lease ever moves, this breaks again —
> a DHCP reservation is the durable fix.

## 8. Related documents

| Topic | File |
|---|---|
| Repo/folder conventions, deployment, credentials policy | `CLAUDE.md` |
| Fleet dashboard, per-node runbooks, `architecture.yaml` | `globalnet/` |
| Synology/DSM specifics, Copyparty | `scripts/synology/README.md` |
| WhatsApp gateway, listener, condfy bridge | `scripts/proxmox/docker/bnu-docker/*/README.md` |
