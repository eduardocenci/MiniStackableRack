# mia Kasa HS300 power strips — local, cloud-free (done 2026-09-01)

Two 6-outlet TP-Link Kasa HS300 (hw 2.0) on the mia rack LAN, both on the
`Cenci-IoT` SSID (same `192.168.2.0/24` as the wired rack; reserved `.50` rack strip, `.51` second strip), both provisioned
**without a Kasa account** (discovery `owner` empty, factory-default
credentials `kasa@tp-link.net` / `kasaSetup` accepted):

| strip | IP | MAC | fw | local protocol | HA entry |
|---|---|---|---|---|---|
| `TP-LINK_Power Strip_F845` (rack) | `.233` | `E0-D3-62-D0-F8-45` | 1.1.2 (2024-12) | **KLAP v2** :80 (`lv 2`, `new_klap 1`) | `01KMXYA5Z7D3JHRXFN52RQ177P` — needs `klap2_patch` |
| `TP-LINK_Power Strip_FDB8` | `.213` | `E0-D3-62-D0-FD-B8` | 1.0.11 (2021-10) | legacy :9999 | `01M1FS8A0MBCHZPN7M8MPES6FN` |

Rack strip outlets (HA unique_id = `8006CCADC855C79E3F05D2CF0195D27024597005`
+ two-digit index; **names and entity ids survived the factory reset**):

| idx | alias | feeds |
|---|---|---|
| 0 | Plug1 | — |
| 1 | Anker_USBcHub | desktop hub |
| 2 | Desktop | mia desktop |
| 3 | MiniRack_Main | mia-proxmox |
| 4 | MiniRack_UPS_NAS | UPS + NAS |
| 5 | MiniRack_AUX_Rasp | mia-raspberrypi |

These feed the `power_entity` sensors in `globalnet/architecture.yaml`. FDB8's
outlets still carry factory names `Kasa_Smart Plug_FDB8_0..5` (outlet 5 ≈205 W,
outlet 2 ≈23 W — not identified yet).

## What was wrong, and what is now true

- A strip **bound to a Kasa account** derives its local KLAP key from that
  account's password. HA had no password stored → `setup_error`. The password
  on record did not match what the strip held either (route A `unbind` was
  refused), so both strips went through **route B: factory reset + local
  provisioning**. The 10 s reset did **not** drop the relays on either strip
  (outlet `on_since` timestamps survived).
- The strips still open a session to `n-devs.tplinkcloud.com` (no account
  behind it). Blocking their WAN on the UniFi gateway (UCG Max `192.168.2.1`,
  `MIA_UNIFI_*` creds; traffic rule on the two MACs + DHCP reservations for
  `.233`/`.213`) is the remaining step — controller access was not exercised
  from this repo yet.
- **HS300 fw 1.1.2 speaks KLAP v2 on an IOT device.** python-kasa 0.10.2 (also
  what HA 2026.8 pins) maps `IOT.KLAP` to the v1 transport and ignores
  `login_version`, so it fails "Device response did not match our challenge"
  no matter the credentials (python-kasa#1604, home-assistant/core#153390,
  waiting-for-upstream). The strip validates the plain v2 formula
  `sha256(local_seed + remote_seed + sha256(sha1(user) + sha1(pw)))` — proved
  with `hs300_local.py probe`. A second gap: for KLAP IOT devices the library
  builds an `IotPlug` from the discovery family instead of an `IotStrip` from
  sysinfo → no outlets. Both are worked around in
  `scripts/proxmox/homeassistant/mia-homeassistant/custom_components/klap2_patch/`
  (deployed on mia HA) and in `hs300_local.py` / `join_v2.py` here.

## Tooling

Desktop (any tailnet machine; the rack LAN is routed):

```bash
uv run scripts/kasa/mia-hs300/hs300_local.py probe    # discovery owner + handshake test (legacy fw: cnCloud binded)
uv run scripts/kasa/mia-hs300/hs300_local.py verify   # default creds, outlets + watts (forces KLAP v2 when lv=2)
uv run scripts/kasa/mia-hs300/hs300_local.py names    # restore the 6 outlet aliases if ever lost
HS300_HOST=192.168.2.51 uv run scripts/kasa/mia-hs300/hs300_local.py probe   # the other strip
```

`unbind` (route A) stays for a strip that is bound to an account whose
password you know: `MIA_KASA_LOGIN/PW` in `.env`, then `... hs300_local.py unbind`.

On mia-raspberrypi (kept there, nothing persists between uses):

| file | role |
|---|---|
| `~/pi_ap.sh up <suffix>` / `down` | join/leave a strip's open setup AP on wlan0 (eth0 stays the uplink); adds a /32 route to `192.168.0.1` via wlan0. The strip is `192.168.0.1` on that AP; since the rack LAN moved to `192.168.2.0/24` (2026-09-04) this no longer collides with the Pi's gateway/DNS (`192.168.2.1`). |
| `~/kasa310/` | python-kasa **0.10.2** venv built for arm64 — the Pi is 64-bit kernel / 32-bit OS (Python 3.9), so it only runs inside `docker run --rm --network host --security-opt seccomp=unconfined --platform linux/arm64 -v /home/eduardocenci/kasa310:/venv python:3.12-slim-bookworm /venv/bin/...` (without `seccomp=unconfined` the 64-bit binary dies with SIGSYS, exit 159). |
| `~/kasa310/join_v2.py` | = `join_v2.py` here — KLAP v2 setup-mode connect + `wifi_join` (fw 1.1.x strips) |
| `~/kasa-venv/` | python-kasa 0.7.7 (native, Python 3.9) — enough for **legacy-fw** strips |
| `~/pi_provision.sh <suffix> [SSID PSK [keytype]]` | one-shot legacy-fw path (0.7.7): join AP, `wifi scan`, `wifi join` |
| `~/pi_relay.py` | stdlib TCP relay `:18080 → 192.168.0.1:80`, fallback to drive a setup-mode strip from the desktop with `kasa --host mia-raspberrypi --port 18080` |

## Route B — factory reset + local provisioning (proven on both strips)

Prefer the Pi. (Until the 2026-09-04 subnet move the rack gateway was also
`192.168.0.1`, the strip's setup-AP address, so joining from the desktop
clashed and could cut its internet; on `192.168.2.0/24` that clash is gone,
but the Pi's wlan0 remains the tested path.)

1. Hold the strip's power button ~10 s until the Wi-Fi LED blinks
   amber/green. Open AP `TP-LINK_Power Strip_<last 4 hex of MAC>` appears
   (no password; strip = `192.168.0.1`). Verify it is *your* strip: the old
   IP stops answering and the AP's BSSID matches the MAC.
2. fw 1.1.x (KLAP v2):
   ```bash
   python scripts/devtool.py run mia-raspberrypi "~/pi_ap.sh up F845"
   python scripts/devtool.py run mia-raspberrypi "docker run --rm --network host --security-opt seccomp=unconfined --platform linux/arm64 -v /home/eduardocenci/kasa310:/venv python:3.12-slim-bookworm /venv/bin/python /venv/join_v2.py Cenci-IoT '<psk>' 4"
   python scripts/devtool.py run mia-raspberrypi "~/pi_ap.sh down"
   ```
   legacy fw (port 9999): `~/pi_provision.sh FDB8 'Cenci-IoT' '<psk>' 4`.
   `4` is the `key_type` the strip reports for Cenci-IoT in `wifi scan`.
3. `join response: {}` → the AP disappears within ~20 s and the strip is back
   on `192.168.2.0/24` (`ip neigh | grep e0:d3:62` on the Pi; the UCG handed
   back the same leases). Then `hs300_local.py probe` → `owner=<empty>`.
4. HA: `MSYS_NO_PATHCONV=1 python scripts/devtool.py ha mia POST /api/config/config_entries/entry/<id>/reload '{}'`
   (a new strip: `POST /api/config/config_entries/flow '{"handler":"tplink"}'`
   then `POST .../flow/<flow_id> '{"host":"<ip>"}'`). fw 1.1.x strips need
   `klap2_patch` loaded or the reload fails exactly like before.
