# InfluxDB 2 — MIA thermal-model store

Multi-year, never-purged telemetry store for the den/kitchen thermal model
(see the Den Thermal Control Plan). Home Assistant streams an allow-list of
entities here via its built-in `influxdb:` integration; the recorder on HA
stays a short-lived cache (`packages/recorder_prune.yaml`).

| Fact | Value |
|---|---|
| Host | `mia-nas-ds918plus`, DSM Docker, `/volume1/docker/influxdb/` |
| Image | `influxdb:2.7` |
| Bind | `${MIA_INFLUXDB_BIND_IP}:8086` — the NAS **rack-LAN** IP (192.168.2.15), LAN-only |
| Org / bucket | `casa-mia` / `house`, retention **infinite** |
| Auth | admin token `MIA_INFLUXDB_TOKEN` (repo-root `.env`, MIA section) |
| Off-site copy | nightly Parquet snapshot rsynced to bnu-nas (thermo service, later phase) |

## Why LAN and not tailnet

HA's `influxdb:` integration runs inside the HA Core container. Verified
2026-09-07 from that container: `192.168.2.15:5000` → 200, but the NAS tailnet
IP `100.110.80.51` and copyparty's `127.0.0.1` bind are both unreachable. So
this binds the NAS LAN IP. Reachable on `192.168.2.0/24`, never WAN,
token-protected. If the NAS LAN IP changes, update `MIA_INFLUXDB_BIND_IP` in
the `.env` beside the compose **and** `influxdb_host` in HA `secrets.yaml`.

## Deploy / redeploy

```bash
# from the Claude host, via the nas.py helper (sudo -S with MIA_NAS_SSH_PW):
#   mkdir + chown the dirs, push compose + .env, then compose up
sudo mkdir -p /volume1/docker/influxdb/{data,config}
sudo chown -R 1028:100 /volume1/docker/influxdb
cd /volume1/docker/influxdb && /usr/local/bin/docker-compose up -d
```

`.env` beside the compose (gitignored) must define: `MIA_INFLUXDB_BIND_IP`,
`MIA_INFLUXDB_INIT_PASSWORD`, `MIA_INFLUXDB_TOKEN`, and optionally
`MIA_INFLUXDB_ORG` / `MIA_INFLUXDB_BUCKET`. The `DOCKER_INFLUXDB_INIT_*` vars
run only on first start (empty data volume); they are ignored afterwards.

## Check

```bash
docker exec influxdb influx ping
docker exec influxdb influx bucket list --token "$MIA_INFLUXDB_TOKEN"
curl -s http://192.168.2.15:8086/health        # {"status":"pass"}
```

## Web UI

`http://192.168.2.15:8086` on the rack LAN, log in as `admin` /
`MIA_INFLUXDB_INIT_PASSWORD`. Data Explorer builds Flux queries against the
`house` bucket.
