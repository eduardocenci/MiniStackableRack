# MiniStackableRack

## Folder Structure

**The folder hierarchy must mirror the system architecture.** A component that runs inside another component lives in a subfolder of it — not alongside it.

```
Root
├── 3d-models/                  3D print files for the physical rack enclosure
├── gitignore/                  Local-only files, never committed (gitignored)
├── netoverview/                Submodule — github.com/eduardocenci/netoverview (network overview tool)
├── globalnet/                  Submodule — github.com/eduardocenci/globalnet (private; multi-site dashboard)
├── homes/                      One submodule per home build — construction documentation, not rack sites
│   └── ara/                    Submodule — github.com/eduardocenci/home-ara (private; House Hangar, Araquari SC)
└── scripts/
    ├── devtool.py              THE entry point for reaching any device (see REMOTE_ACCESS.md)
    ├── proxmox/                MiniPC runs Proxmox (hypervisor)
    │   ├── <site>-proxmox/     Host-level config (lxc/, udev/)
    │   ├── homeassistant/      Home Assistant OS runs as a VM on Proxmox
    │   │   └── <site>-homeassistant/
    │   ├── docker/             Docker host on Proxmox — one folder per deployment, one subfolder per container
    │   │   └── bnu-docker/     LXC 101: waha, waha-listener, condfy-bridge, netoverview-agent
    │   ├── frigate/            Frigate NVR in an LXC (bnu: LXC 105)
    │   ├── ollama/             Ollama LLM host in an LXC (bnu: LXC 106)
    │   ├── plex/               Plex media server in an LXC (bg: LXC 102)
    │   └── win11/              Windows 11 VM (Tailscale node, e.g. bnu-win11) — folder not created yet
    ├── raspberry-pi/           Raspberry Pi — independent rack component (not a VM); runs Docker; serves as network monitoring node (device discovery, traffic analysis via ARP spoofing)
    │   └── <site>-raspberrypi/
    │       └── docker/         Docker deployments on that Pi (e.g. globalnet, netoverview)
    └── synology/               Synology NAS — independent rack component (ply only)
        └── ply-synology/
            └── docker/         e.g. copyparty
```

Each deployment instance is named `<deployment>-<component>` (e.g. `bnu-homeassistant`, `ply-proxmox`).

`<site>` is one of **bnu, ply, bg, fln**. Only folders for sites that have
site-specific config exist — the absence of a folder does not mean the device is
absent (fln has a full rack but no folders yet). The authoritative device
inventory is `globalnet/architecture.yaml`; the authoritative way to reach any of
them is `REMOTE_ACCESS.md`.

## Deployment

### netoverview (Raspberry Pi Docker container)

**`git push` to `netoverview/` is the only deployment step needed.**

Each Raspberry Pi runs a cron job every 5 minutes that pulls the latest image from DockerHub and restarts the container if it changed:

```bash
*/5 * * * * cd ~/netoverview && docker compose pull && docker compose up -d
```

Sequence on a code change:
1. Push commits to `netoverview/` → GitHub Actions builds `cenci/netoverview:latest` (multi-arch: amd64, arm64, arm/v7)
2. Within ≤5 min, each Pi's cron job detects the new image → pulls → restarts container automatically
3. No SSH, no `deploy_all.ps1`, no manual intervention required

> `deploy_all.ps1` is for **first-time setup only** (installing Docker and dropping the compose file on a new Pi). Do not use it as a routine update mechanism — the cron handles that.

### New Docker container — registration checklist

Every Docker container is created through Claude, so this checklist **is** the
gate: creating, renaming, or retiring a container on **any** host is not done
until, in the same session:

1. **Folder** — compose file + README under the host's folder per the
   hierarchy rule (e.g. `scripts/proxmox/docker/bnu-docker/<container>/`).
2. **Register** — declare it as a `kind: docker` child of its host node in
   `globalnet/architecture.yaml` (`container:` = exact Docker name;
   `check_url` if it serves HTTP; `doc:` slug).
3. **Runbook** — `globalnet/docs/runbooks/<name>.md`, added to the toctree in
   `globalnet/docs/runbooks/index.md`.
4. **Verify** — `cd globalnet && make docs && make check && make fleet`;
   `make fleet` diffs every host's live containers against architecture.yaml
   (both directions) and must exit clean.
5. **Ship** — push globalnet `main` (deploys dashboard + docs via DockerHub →
   Pi cron), then bump the globalnet submodule pointer in this repo.

The dashboard renders an unregistered container as a nameless italic row — no
box, no health LED, no Docs button. Treat that row, or a failing `make fleet`,
as an unfinished deploy, not cosmetics. (Details: `DOCS_WORKFLOW.md`.)

## Credentials

- All credentials live in a single `.env` at the repo root
- Keys shared by a **device type** across every site are **unprefixed**, in the `COMMON` section (`PROXMOX_LOGIN/PW`, `RASPBERRYPI_LOGIN/PW`, `GLKVM_LOGIN/PW`, `HA_SSH_LOGIN/PW`) — the same credential works on that device type at every site. Do not guess these from the region convention
- Site-specific keys are prefixed and sectioned by region (`BG_`, `BNU_`, `PLY_`, `FLN_`), plus an `ARA` section for the house build
- Within each region section, keys are grouped by component (e.g. `# Home Assistant`, `# Raspberry Pi`)
- Live copies exist outside `.env` (LXC 101 service `.env`s, HA `secrets.yaml`, NAS compose) — the root `.env` is authoritative; see `REMOTE_ACCESS.md` §5 for the drift list
- `.env` and `.env.*` are gitignored — never committed
- `.env.example` mirrors `.env` structure exactly but with placeholder values — **keep it in sync whenever `.env` changes** — it is committed to the repo
- The `gitignore/` folder is also gitignored and can hold any other local-only files

## Remote Access

**→ Full access map: [`REMOTE_ACCESS.md`](REMOTE_ACCESS.md). Read it before
reaching any device, and use `scripts/devtool.py` rather than hand-rolled
`ssh`/`curl`. Do not try several methods until one works — every method that
works is recorded there, and the ones that don't have already been tried.**

```bash
python scripts/devtool.py test all       # verify connectivity, all sites
python scripts/devtool.py run bnu-proxmox "qm list"
python scripts/devtool.py list bnu       # VMs + LXCs + containers of a rack
python scripts/devtool.py guest bnu 101 "docker ps"   # inside an LXC/VM
```

**Tailscale is the network layer.** Tailnet devices are nodes named exactly after their `<deployment>-<component>` name (e.g. `bnu-proxmox`, `ply-nas-ds918plus`). The machine Claude runs on is itself a tailnet node, so **those devices are directly reachable by their bare name** (MagicDNS) — no VPN setup, no port forwarding. Run `tailscale status` locally to list all nodes and their `100.x` IPs. Services bound to a device's Tailscale IP (e.g. Copyparty on the NAS) are reachable only through the tailnet, by design.

**But not every device is on the tailnet.** The bnu docker LXC (`10.1.1.126` — waha, waha-listener, condfy-bridge, netoverview-agent), ollama, the Zigbee gateways, the Hikvision NVR and doorbell, and the routers are **LAN-only**: their bare names do not resolve. Reach them by hopping through the site's Proxmox host — `python scripts/devtool.py lan bnu 10.1.1.132 "curl -sS http://10.1.1.132/"`. Full list in `REMOTE_ACCESS.md` §2. Outside those devices, prefer tailnet names over LAN IPs in new work.

**Sites are `bnu`, `ply`, `bg`, `fln`** — four, not three. `fln` has no folders under `scripts/` yet, but it is a full rack in `.env`, `devtool.py` and `globalnet/architecture.yaml`.

**SSH tooling:** this machine's `~/.ssh/id_ed25519` is authorized on every device except the Home Assistant SSH add-on, so plain OpenSSH (`ssh`, in PATH via Git Bash) works non-interactively. Password auth (HA add-on, or key-auth fallback) requires Python `paramiko` (installed). `plink` and `sshpass` are **not** installed — do not use them.

Priority-ordered access interfaces — LLM vs human (details, users and auth in `REMOTE_ACCESS.md`):

| Device | LLM priority | Human priority |
|---|---|---|
| Proxmox | SSH (`root`, key) | Web UI `https://<host>:8006` |
| Home Assistant | REST API (`<REGION>_HA_TOKEN`) → SSH add-on (`hassio`, password only) | Web UI `http://<host>:8123` |
| Windows 11 VM | SSH (`eduardocenci`, key; shell is PowerShell) → QEMU guest agent via Proxmox | RDP |
| Raspberry Pi | SSH (`eduardocenci`, key) | SSH |
| GL KVM | SSH (`root`, key; dropbear) | Web UI `http://<host>` |
| Synology NAS | SSH (key, `PLY_NAS_SSH_LOGIN`; see `scripts/synology/README.md`) | Web UI (DSM) `http://<host>:5000` |
| VMs / LXCs / containers | `devtool.py list` + `devtool.py guest` through the Proxmox host | Proxmox web console |

**LLM rule:** prefer the highest-priority interface that works; fall back down the list. Never open a browser unless all CLI/API options are exhausted.

## Rules

- Before adding a new script or config, place it under the component it belongs to
- If a component runs inside another (VM, container, add-on), its folder goes inside the parent's folder
- Independent rack components (Raspberry Pi, Remote KVM, Zigbee Gateway) sit at the top level of `scripts/`
- Keep system architecture representation up-to-date using Excalidraw (`systemarchitecture.excalidraw` at repo root) — use the Excalidraw skill to edit it directly
- In docs, reference devices by their bare component name (e.g. `proxmox`) when settings are uniform across all regions; list the region-specific names (e.g. `bnu-proxmox`, `ply-proxmox`, `bg-proxmox`, `fln-proxmox`) only when providing per-region context or when settings differ between regions
- When you learn a new way to reach or manage a device — or find that a documented way no longer works — record it in `REMOTE_ACCESS.md` in the same session. That file is the fleet's memory of what works; leaving it stale is what causes the next session to fail through several methods before finding the right one
- A home build (`homes/<code>`, e.g. `homes/ara`) uses a region code but is **not** a globalnet site until it has a rack — its data lives in its own `home-<code>` repo (single-source `house.yaml` + registries), its cockpit is globalnet `/house/<code>`, and its document intake is the `ingest-home-docs` skill inside the home repo (see `homes/ara/CLAUDE.md`)

## Image Generation (Nano Banana)

Claude Code can generate images via the **Nano Banana** skill (`~/.claude/skills/nano-banana/`), which calls the Gemini CLI's nanobanana extension.

**How it works:** Claude runs `gemini --yolo "/generate 'prompt'"` via Bash → Gemini CLI → nanobanana MCP server → Gemini image model (`gemini-2.5-flash-image`). Images are saved to `./nanobanana-output/` in the current directory.

**Setup (already done):**
- Gemini CLI: installed globally (`npm install -g @google/gemini-cli`)
- nanobanana extension: cloned and built at `~/.gemini/extensions/nanobanana/`
- `GEMINI_API_KEY` and `NANOBANANA_API_KEY`: set in `~/.bashrc`
- Skill: `~/.claude/skills/nano-banana/SKILL.md`

**Usage examples:**
- "Generate a blog header image about home automation"
- "Create an app icon for a monitoring dashboard"
- "Draw a flowchart of the Proxmox + Home Assistant architecture"

**Available commands:** `/generate`, `/edit`, `/restore`, `/icon`, `/diagram`, `/pattern`, `/story`

## Excalidraw Diagrams

Claude Code can create and edit `.excalidraw` files (JSON format) directly using the **excalidraw-diagram skill** (`~/.claude/skills/excalidraw-diagram/`). The **Excalidraw MCP** (`https://mcp.excalidraw.com`) is also connected for interactive diagram generation.

**How it works:** The skill teaches Claude the Excalidraw JSON schema and design principles. Claude edits `.excalidraw` files directly with `Write`/`Edit` tools. A Playwright renderer validates the result visually.

**Setup (already done):**
- Skill: `~/.claude/skills/excalidraw-diagram/` (includes JSON schema, element templates, color palette)
- Playwright + Chromium: installed at `~/.claude/skills/excalidraw-diagram/references/` via `uv`
- Excalidraw MCP: configured in `~/.claude/mcp.json` → `https://mcp.excalidraw.com`

**To render/validate a diagram:**
```bash
cd ~/.claude/skills/excalidraw-diagram/references && uv run python render_excalidraw.py <path-to-file.excalidraw>
```

**Key file:** `systemarchitecture.excalidraw` at repo root — keep it in sync with the system architecture.
