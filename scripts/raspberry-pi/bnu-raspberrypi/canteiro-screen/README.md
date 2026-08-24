# canteiro-screen — ARA obra live stream on the bnu-raspberrypi screen

Keeps the screen attached to bnu-raspberrypi showing the live stream of the
House Hangar construction site (ARA), relayed by
[`ara-raspberrypi/mediamtx`](../../ara-raspberrypi/mediamtx/):

```
rtsp://ara-raspberrypi:8554/canteiro ──tailnet──▶ mpv (fullscreen, labwc session) ──▶ HDMI screen
```

Not a Docker container — a plain systemd **system** service that joins the
Pi's existing labwc/Wayland autologin session (`eduardocenci`, seat0). The
session also hosts wayvnc on a headless output; the service deliberately does
NOT replace the compositor (no cage/DRM kiosk) so VNC access keeps working.

| File | Live copy |
|---|---|
| [`canteiro-screen.service`](canteiro-screen.service) | `/etc/systemd/system/canteiro-screen.service` |
| [`99-canteiro-screen-hotplug.rules`](99-canteiro-screen-hotplug.rules) | `/etc/udev/rules.d/99-canteiro-screen-hotplug.rules` |

## Behavior

- mpv runs fullscreen with OSD/keybindings off and audio off, TCP transport,
  low-latency profile, `hwdec=auto-safe` (Pi 4 hardware HEVC/H.264 decode).
- On stream loss/EOF mpv **exits** and systemd restarts it every 10 s,
  forever (`StartLimitIntervalSec=0`) — the screen self-heals when Starlink,
  the relay, or the camera come back.
- The udev rule restarts the player on HDMI hotplug so the window lands on
  the physical screen (with no screen attached, mpv renders on the headless
  output — harmless).

## Operate

```bash
python scripts/devtool.py run bnu-raspberrypi "systemctl status canteiro-screen --no-pager -l"
python scripts/devtool.py run bnu-raspberrypi "sudo systemctl stop canteiro-screen"      # blank the screen
python scripts/devtool.py run bnu-raspberrypi "sudo systemctl start canteiro-screen"
```

To show the other camera lens, point the URL at `/canteiro-alt` (or swap the
channels in the relay config on ara-raspberrypi — see its README).
