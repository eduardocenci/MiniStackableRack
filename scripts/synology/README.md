# Synology NAS

One NAS in the fleet: **`ply-nas-ds918plus`** (Synology DS918+, PLY rack).
It is a tailnet node — reachable from any tailnet device (including the machine
Claude runs on) by its bare MagicDNS name:

| Service | Address | Notes |
|---|---|---|
| DSM Web UI | `http://ply-nas-ds918plus:5000` | Human interface |
| SSH | `ply-nas-ds918plus:22` | **LLM interface** — enabled |
| Copyparty | `http://ply-nas-ds918plus:3923` | See `ply-synology/docker/copyparty/` |

## LLM access — SSH

Credentials live in the repo-root `.env`, PLY section:

```
PLY_NAS_SSH_LOGIN=...     # DSM user (must be in the administrators group)
PLY_NAS_SSH_PW=...        # for paramiko password auth
# optional, preferred once set up:
PLY_NAS_SSH_KEY=gitignore/ply-nas_id_ed25519
```

- **Key auth (preferred):** plain `ssh -i <key> $PLY_NAS_SSH_LOGIN@ply-nas-ds918plus '<cmd>'`
  is fully non-interactive.
- **Password auth (works today):** use Python `paramiko` — `plink`/`sshpass` are
  NOT installed on the Claude host (see CLAUDE.md → Remote Access).
- **Docker needs root:** run docker via `sudo` (pipe the password from `.env` to
  `sudo -S`), or install the one-time sudoers drop-in below to make it
  passwordless.

## One-time setup (already done / how to redo)

### 1. Enable SSH
DSM → Control Panel → Terminal & SNMP → **Enable SSH service** (port 22).
Status: **done** (port 22 confirmed open over Tailscale).

### 2. SSH user
DSM only permits SSH for users in the **administrators** group. Either reuse the
main admin account, or (cleaner) create a dedicated one so LLM access can be
audited/revoked independently:

1. DSM → Control Panel → User & Group → **Create** → e.g. `claude`
2. Add to group **administrators** (required for SSH)
3. Deny access to all shared folders except the ones it must manage
   (e.g. `copyparty`, `docker`) — admin group membership does NOT auto-grant
   share access; per-share permissions still apply
4. Put login + password into `.env` (`PLY_NAS_SSH_LOGIN` / `PLY_NAS_SSH_PW`)

### 3. Key auth (upgrade from password — DSM has quirks)

```sh
# a) On the Claude host — generate a key into the gitignored folder:
ssh-keygen -t ed25519 -f gitignore/ply-nas_id_ed25519 -N "" -C "claude@rack"

# b) Enable user homes or ~ won't exist:
#    DSM → Control Panel → User & Group → Advanced → ✓ Enable user home service

# c) On the NAS (ssh in with the password once):
mkdir -p ~/.ssh
cat >> ~/.ssh/authorized_keys   # paste gitignore/ply-nas_id_ed25519.pub, Ctrl-D

# d) Permissions — DSM's sshd silently rejects keys if these are loose:
chmod 755 "$HOME"               # home must not be group/other-writable
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys

# e) If keys still don't work, check /etc/ssh/sshd_config as root:
#    PubkeyAuthentication yes        (uncomment)
#    AuthorizedKeysFile .ssh/authorized_keys
#    then restart: DSM → Terminal & SNMP → toggle SSH off/on

# f) Verify from the Claude host:
ssh -i gitignore/ply-nas_id_ed25519 -o BatchMode=yes \
    "$PLY_NAS_SSH_LOGIN@ply-nas-ds918plus" 'uname -a && id'
# then set PLY_NAS_SSH_KEY in .env
```

### 4. Passwordless docker (optional)
DSM has no `docker` group; docker requires root. One-time as root:

```sh
echo "$PLY_NAS_SSH_LOGIN ALL=(ALL) NOPASSWD: /usr/local/bin/docker" \
  > /etc/sudoers.d/docker-llm
chmod 440 /etc/sudoers.d/docker-llm
```

Until then, `echo "$PLY_NAS_SSH_PW" | sudo -S docker ...` works.

## DSM Web API (fallback only)

DSM exposes a REST API (`/webapi/`, session-based auth) for DSM-domain
operations — creating shared folders, installing packages, user management.
It is NOT the primary LLM interface: everything deployment-shaped (files,
compose, logs) is easier over SSH. Reach for the API only if SSH can't do it.
Docs: https://kb.synology.com/en-us/DG/DSM_Login_Web_API_Guide
