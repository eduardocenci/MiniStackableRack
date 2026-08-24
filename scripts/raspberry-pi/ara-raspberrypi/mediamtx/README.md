# mediamtx — canteiro camera relay (ara-raspberrypi)

Relays the Intelbras iM9+ Full Color site camera (`192.168.1.56`, Wi-Fi,
LAN-only) onto the tailnet so any fleet node can watch the obra without
touching the camera or the house LAN:

```
iM9 camera ──RTSP 554 (LAN)──▶ mediamtx on ara-raspberrypi ──RTSP 8554 (tailnet)──▶ viewers
                                                                └─ bnu-raspberrypi screen
```

| Path | What it is |
|---|---|
| `rtsp://ara-raspberrypi:8554/canteiro` | lens on `channel=1`, main stream — pulled 24/7 (`sourceOnDemand: no`) |
| `rtsp://ara-raspberrypi:8554/canteiro-alt` | lens on `channel=2`, main stream — pulled on demand |

The relay has no reader auth: it is reachable only from the tailnet and the
house LAN, and the camera credential stays on this Pi.

## Install layout (live)

| File | Purpose |
|---|---|
| `/usr/local/bin/mediamtx` | static binary (GitHub release, linux_arm64) |
| `/etc/mediamtx/mediamtx.yml` | config — copy of [`mediamtx.yml`](mediamtx.yml) with the real `chave de acesso` (640 `root:mediamtx`) |
| `/etc/systemd/system/mediamtx.service` | copy of [`mediamtx.service`](mediamtx.service) (runs as system user `mediamtx`) |

## Camera key — install / update

While `ARA_CANTEIRO_CAM_KEY` is unknown, the **live config keeps the two
paths without a `source:`** (publisher mode) instead of the repo config's
camera URLs — a wrong key retried every 5 s keeps the camera's Dahua
anti-bruteforce lockout warm (401 → 403), so the relay must not pull until
the key is real. To install the key (from repo root, key in `.env`):

```bash
sed "s/__ARA_CANTEIRO_CAM_KEY__/<CHAVE>/g" scripts/raspberry-pi/ara-raspberrypi/mediamtx/mediamtx.yml \
  | ssh eduardocenci@ara-raspberrypi "sudo tee /etc/mediamtx/mediamtx.yml > /dev/null && sudo chown root:mediamtx /etc/mediamtx/mediamtx.yml && sudo chmod 640 /etc/mediamtx/mediamtx.yml && sudo systemctl restart mediamtx"
ssh eduardocenci@ara-raspberrypi "journalctl -u mediamtx -n 20 --no-pager"   # expect "[path canteiro] source ready"
```

URL-encode the key first if it contains symbols. Keep the key in the
repo-root `.env` (`ARA_CANTEIRO_CAM_KEY`) — the live config here is a
credential mirror (listed in `REMOTE_ACCESS.md` §5).

## Troubleshooting

- `401` in the journal → wrong/missing chave de acesso (factory sticker key,
  not the app-changed one; reset the camera if the sticker key was rotated).
- `403 Forbidden` after 401s → the camera's anti-bruteforce lockout; stop
  retrying with bad credentials (revert the paths to publisher mode) and let
  it cool for some minutes before trying a corrected key.
- Source connects but no video → check the camera's "criptografia de
  imagem/vídeo" toggle in the Mibo Cam app (Imou twins block plain RTSP
  payload when image encryption is on).
- Camera IP drifted → it is a DHCP lease on the Starlink router; update
  `192.168.1.56` here or pin a reservation in the Starlink app.
