# Copyparty on the Synology NAS (Tailscale-only)

[Copyparty](https://github.com/9001/copyparty) is a portable file server (web UI
+ WebDAV + FTP). This deployment runs it in Docker on the Synology NAS,
reachable **only from devices on the Tailscale network** — never from the LAN or
the public internet.

> **Status: DEPLOYED & VERIFIED** (2026-07-12) at `/volume1/docker/copyparty/`
> on `ply-nas-ds918plus`, serving the dedicated `copyparty` DSM share
> (`/volume1/copyparty`, created via `synoshare --add`). Verified end-to-end
> over the tailnet: auth-gated listing, PUT/GET/DELETE round-trip, WebDAV on,
> anonymous denied.

> **Which NAS:** the `ply-nas-ds918plus` (Synology DS918+, DSM 7.1) in the PLY
> rack — the only Synology NAS in the fleet (see `systemarchitecture.excalidraw`).
> DSM WebUI on `:5000`; Copyparty adds `:3923`, no conflict. The Docker package
> ships compose **v1** — the command is `docker-compose`, not `docker compose`.

## Files

| File | Committed? | Purpose |
|---|---|---|
| `docker-compose.yml` | yes | Container definition; binds the port to the NAS Tailscale IP |
| `copyparty.conf` | yes | Global options + shares; **placeholder** password |
| `copyparty.local.conf` | **no** (gitignored) | Real account password(s), created on the NAS |
| `.env` | **no** (gitignored) | `${...}` values for the compose (Tailscale IP, PUID/PGID) |

Copyparty auto-loads every `*.conf` in the mounted `/cfg` folder alphabetically,
so `copyparty.local.conf` overrides `[accounts]` from `copyparty.conf`.

## Prerequisites

1. **Tailscale on the NAS** — ✅ done: the NAS is on the tailnet as
   `ply-nas-ds918plus` (100.110.80.51). If it ever needs reinstalling:
   Package Center → *Tailscale*, sign in, then `tailscale ip -4`.
2. **Container Manager** installed (Package Center).
3. A **shared folder** for the files, e.g. create `copyparty` (or reuse an
   existing share). Note its path, typically `/volume1/copyparty`.
4. **SSH enabled** — ✅ done (port 22 open over Tailscale). LLM access details:
   `scripts/synology/README.md`.

## Deploy

### Option A — command line (recommended for LLM/scripted setup)

```sh
# on the NAS, over SSH
sudo mkdir -p /volume1/docker/copyparty
# copy docker-compose.yml + copyparty.conf here (scp, git clone, or File Station)

cd /volume1/docker/copyparty

# 1) create the local secrets (gitignored):
cat > copyparty.local.conf <<'EOF'
[accounts]
  ed: <your-real-strong-password>
EOF

# 2) create the .env the compose reads (gitignored):
#    - BIND_IP: 127.0.0.1 for userspace tailscale (Synology default — see
#      "Why it's tailnet-only" below), else the NAS 100.x Tailscale IP
#    - PUID/PGID: from `id <the-dsm-user-that-owns-/volume1/copyparty>`
cat > .env <<'EOF'
PLY_COPYPARTY_BIND_IP=127.0.0.1
PLY_COPYPARTY_PUID=1028
PLY_COPYPARTY_PGID=100
EOF

# 3) start it (DSM Docker package = compose v1, hyphenated command)
sudo docker-compose up -d
sudo docker-compose logs -f copyparty     # watch it come up
```

### Option B — DSM GUI

1. Put this folder on the NAS (File Station) under `/volume1/docker/copyparty/`.
2. Create `copyparty.local.conf` and `.env` beside the compose (see Option A).
3. DSM 7.1's **Docker** package UI has no compose-project support (that arrived
   with Container Manager in DSM 7.2) — run Option A's `docker-compose up -d`
   over SSH; the running container then shows up in the Docker UI.

## Access

From any device on the tailnet:

```
http://ply-nas-ds918plus.<your-tailnet>.ts.net:3923
```

or `http://100.110.80.51:3923`. Log in as `ed` with the password from
`copyparty.local.conf` (mirrored in the repo-root `.env` as
`PLY_COPYPARTY_PASSWORD`).

- **WebDAV:** same URL — mount it in Finder / Windows Explorer / mobile apps.
- **Mobile:** any WebDAV client, or just the web UI (it's mobile-friendly).

### Why it's tailnet-only

Synology's Tailscale package runs in **userspace-networking mode** by default:
there is no `tailscale0` interface, the 100.x IP is not assigned to any NIC
(binding to it fails with `cannot assign requested address`), and `tailscaled`
instead **proxies inbound tailnet connections to `127.0.0.1`** on the same port.
So the container binds `${PLY_COPYPARTY_BIND_IP}` = `127.0.0.1:3923`:

- tailnet client → `100.110.80.51:3923` → tailscaled → `127.0.0.1:3923` ✓
- LAN/WAN client → `192.168.0.10:3923` → nothing listening ✗ (by design)

If Tailscale is ever switched to TUN mode (a `tailscale0` interface appears),
set `PLY_COPYPARTY_BIND_IP` to the NAS's 100.x IP instead. If the var is unset,
`docker-compose up` fails loudly rather than defaulting to `0.0.0.0` — fail safe.

> **Public access?** Not enabled by design. If you ever want it reachable
> outside the tailnet, prefer `tailscale serve` (HTTPS inside the tailnet) or,
> only if you truly need public exposure, `tailscale funnel` — do **not** bind
> to `0.0.0.0` and port-forward on the router.

## Updates

Nothing auto-pulls. To update Copyparty:

```sh
cd /volume1/docker/copyparty
sudo docker-compose pull && sudo docker-compose up -d
```

Optionally automate it with **DSM → Control Panel → Task Scheduler** (a
scheduled *user-defined script*, e.g. weekly) running the two commands above —
the NAS analogue of the Raspberry Pi's 5-min pull cron.

## Notes / troubleshooting

- **Permissions / "cannot write":** the container runs as `PUID:PGID` from
  `.env`. It must match the owner of `/volume1/copyparty`. Check with
  `id <user>` and `ls -ln /volume1/copyparty`.
- **Thumbnails / media tags:** provided by the `ac` image (bundled ffmpeg).
  First index of a large share takes a while; progress is in the logs.
- **Search:** enabled via `e2dsa`/`e2ts`; the index db lives in `/w/.hist`.
- **More shares:** add another `- /volume1/<share>:/w2:z` volume in the compose
  and a matching `[/url]` block in `copyparty.conf`.
- **`:z` SELinux flag:** harmless on Synology (no SELinux); left in to match the
  upstream compose. Remove if it ever complains.
