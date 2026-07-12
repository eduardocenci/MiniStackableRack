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

- **Password auth (the working path):** use Python `paramiko` — `plink`/`sshpass`
  are NOT installed on the Claude host (see CLAUDE.md → Remote Access). A ready
  helper pattern: connect with `look_for_keys=False`, `exec_command`, and for
  root pipe the password to `sudo -S -p ''`.
- **Key auth (pending):** a keypair exists at `gitignore/ply-nas_id_ed25519`,
  but installing the pubkey into `authorized_keys` needs an explicitly-named
  user authorization (the permission classifier blocks it as a persistent
  access grant). Once installed, plain
  `ssh -i <key> $PLY_NAS_SSH_LOGIN@ply-nas-ds918plus '<cmd>'` is fully
  non-interactive.
- **Docker needs root:** pipe the password to `sudo -S`. The sudoers drop-in
  below (also pending explicit authorization) would make it passwordless.
- **Verified facts (2026-07-12):** user `globalnet` uid=1028 gid=100(users),
  member of `administrators`; sudo works; DSM 7.1; Docker 20.10.3 with
  docker-compose **v1** (1.28.5) — command is `docker-compose`, and it lives in
  `/usr/local/bin` (not in the non-login SSH PATH: use full paths).
- **Git Bash gotcha on the Claude host:** a remote command string starting with
  `/` (e.g. `/usr/syno/sbin/synoshare ...`) can get MSYS path-converted into a
  `C:/Program Files/...` path before reaching SSH. Export
  `MSYS_NO_PATHCONV=1` before such calls.

## One-time setup (already done / how to redo)

### 1. Enable SSH
DSM → Control Panel → Terminal & SNMP → **Enable SSH service** (port 22).
Status: **done** (port 22 confirmed open over Tailscale).

### 2. SSH user
Status: **done** — the `globalnet` account (uid 1028, `administrators` member)
is the LLM SSH user; credentials in `.env` (`PLY_NAS_SSH_LOGIN` /
`PLY_NAS_SSH_PW`).

Background: DSM only permits SSH for users in the **administrators** group. To
recreate or replace the user: DSM → Control Panel → User & Group → create it,
add to `administrators`, deny the shares it must not touch (admin membership
does NOT auto-grant share access), and update `.env`.

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
