# MiniStackableRack

## Folder Structure

**The folder hierarchy must mirror the system architecture.** A component that runs inside another component lives in a subfolder of it — not alongside it.

```
Root
├── 3d-models/                  3D print files for the physical rack enclosure
├── gitignore/                  Local-only files, never committed (gitignored)
├── netoverview/                Submodule — github.com/eduardocenci/netoverview (network overview tool)
├── globalnet/                  Submodule — github.com/eduardocenci/globalnet (private; multi-site dashboard)
└── scripts/
    ├── proxmox/                MiniPC runs Proxmox (hypervisor)
    │   ├── homeassistant/      Home Assistant OS runs as a VM on Proxmox
    │   │   ├── bnu-homeassistant/
    │   │   ├── ply-homeassistant/
    │   │   └── bg-homeassistant/
    │   ├── win11/              Windows 11 VM on Proxmox (Tailscale node, e.g. bnu-win11)
    │   │   ├── bnu-win11/
    │   │   ├── ply-win11/
    │   │   └── bg-win11/
    │   ├── docker/             Docker host VM on Proxmox (optional)
    │   └── ubuntu/             Ubuntu VM on Proxmox (optional)
    └── raspberry-pi/           Raspberry Pi — independent rack component (not a VM); runs Docker; serves as network monitoring node (device discovery, traffic analysis via ARP spoofing)
        ├── bnu-raspberrypi/
        │   └── docker/         Docker on bnu-raspberrypi
        │       └── netoverview/ netoverview deployment (see /netoverview submodule)
        ├── ply-raspberrypi/
        │   └── docker/
        │       └── netoverview/
        └── bg-raspberrypi/
            └── docker/
                └── netoverview/
```

Each deployment instance is named `<deployment>-<component>` (e.g. `bnu-homeassistant`, `ply-proxmox`).

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

## Credentials

- All credentials live in a single `.env` at the repo root, with keys prefixed and sectioned by region (`BG_`, `BNU_`, `PLY_`)
- Within each region section, keys are grouped by component (e.g. `# Home Assistant`, `# Raspberry Pi`)
- `.env` and `.env.*` are gitignored — never committed
- `.env.example` mirrors `.env` structure exactly but with placeholder values — **keep it in sync whenever `.env` changes** — it is committed to the repo
- The `gitignore/` folder is also gitignored and can hold any other local-only files

## Remote Access

**SSH tooling:** `plink` (PuTTY) is available at `C:/Program Files/PuTTY/plink.exe` and supports non-interactive password auth — use it for scripted/LLM SSH access. `sshpass` is not available on this Windows environment.

Each device has a priority-ordered list of access interfaces — one for LLM use, one for humans:

| Device | LLM priority | Human priority |
|---|---|---|
| Proxmox | SSH (`root`) | Web UI `https://<host>:8006` |
| Home Assistant | REST API (`HA_TOKEN`) → SSH add-on (`hassio`) | Web UI `http://<host>:8123` |
| Raspberry Pi | SSH (`eduardocenci`) | SSH |
| GL KVM | SSH (`root`) | Web UI `http://<host>` |

**LLM rule:** prefer the highest-priority interface that works; fall back down the list. Never open a browser unless all CLI/API options are exhausted.

## Rules

- Before adding a new script or config, place it under the component it belongs to
- If a component runs inside another (VM, container, add-on), its folder goes inside the parent's folder
- Independent rack components (Raspberry Pi, Remote KVM, Zigbee Gateway) sit at the top level of `scripts/`
- Keep system architecture representation up-to-date using Excalidraw (`systemarchitecture.excalidraw` at repo root) — use the Excalidraw skill to edit it directly
- In docs, reference devices by their bare component name (e.g. `proxmox`) when settings are uniform across all regions; list all three region-specific names (e.g. `bnu-proxmox`, `ply-proxmox`, `bg-proxmox`) only when providing per-region context or when settings differ between regions

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
