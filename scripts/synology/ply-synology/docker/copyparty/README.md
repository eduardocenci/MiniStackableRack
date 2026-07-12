# Copyparty on the Synology NAS (Tailscale-only)

[Copyparty](https://github.com/9001/copyparty) is a portable file server (web UI
+ WebDAV + FTP). This deployment runs it in Docker on the Synology NAS via DSM's
**Container Manager**, reachable **only from devices on the Tailscale network** —
never from the LAN or the public internet.

> **Which NAS:** the `ply-nas-ds918plus` (Synology DS918+) in the PLY rack — the
> only Synology NAS in the fleet (see `systemarchitecture.excalidraw`). Its DSM
> WebUI is on `:5000`; Copyparty adds `:3923`, no conflict.

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
#    - PLY_NAS_TAILSCALE_IP: from `tailscale ip -4`
#    - PUID/PGID: from `id <the-dsm-user-that-owns-/volume1/copyparty>`
cat > .env <<'EOF'
PLY_NAS_TAILSCALE_IP=100.101.102.103
PLY_COPYPARTY_PUID=1026
PLY_COPYPARTY_PGID=100
EOF

# 3) start it
sudo docker compose up -d
sudo docker compose logs -f copyparty     # watch it come up
```

### Option B — DSM GUI (Container Manager)

1. Put this folder on the NAS (File Station) under `/volume1/docker/copyparty/`.
2. Create `copyparty.local.conf` and `.env` beside the compose (see Option A).
3. Container Manager → **Project** → **Create** → point at this folder → it
   detects `docker-compose.yml` → **Build/Up**.

## Access

From any device on the tailnet:

```
http://ply-nas-ds918plus.<your-tailnet>.ts.net:3923
```

or `http://100.101.102.103:3923`. Log in as `ed` with the password from
`copyparty.local.conf`.

- **WebDAV:** same URL — mount it in Finder / Windows Explorer / mobile apps.
- **Mobile:** any WebDAV client, or just the web UI (it's mobile-friendly).

### Why it's tailnet-only

`docker-compose.yml` publishes the port as `${PLY_NAS_TAILSCALE_IP}:3923:3923`,
i.e. it binds **only** to the NAS's Tailscale interface. The LAN and WAN never
see port 3923. If `PLY_NAS_TAILSCALE_IP` is unset, `docker compose up` fails
loudly rather than defaulting to `0.0.0.0` — fail safe.

> **Public access?** Not enabled by design. If you ever want it reachable
> outside the tailnet, prefer `tailscale serve` (HTTPS inside the tailnet) or,
> only if you truly need public exposure, `tailscale funnel` — do **not** bind
> to `0.0.0.0` and port-forward on the router.

## Updates

Container Manager does not auto-pull. To update Copyparty:

```sh
cd /volume1/docker/copyparty
sudo docker compose pull && sudo docker compose up -d
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
