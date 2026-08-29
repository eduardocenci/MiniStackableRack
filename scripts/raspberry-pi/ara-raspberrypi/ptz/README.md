# canteiro-ptz — remote aiming of the iM9's PT lens (ara-raspberrypi)

CLI for nudging the canteiro camera's motorized PT lens over ONVIF and
checking the result, from anywhere on the tailnet:

```bash
ssh eduardocenci@ara-raspberrypi "canteiro-ptz move -0.4 0 1"   # pan burst
ssh eduardocenci@ara-raspberrypi "canteiro-ptz snap"            # keyframe JPEG
ssh eduardocenci@ara-raspberrypi "canteiro-ptz stop"
```

| File | Live copy |
|---|---|
| [`canteiro-ptz.py`](canteiro-ptz.py) | baked into the `canteiro-timelapse` image as `/usr/local/bin/canteiro-ptz` (see [`../docker/canteiro-timelapse/`](../docker/canteiro-timelapse/)); a host copy stays at `/usr/local/bin/canteiro-ptz` for manual aiming (755). Creds: env in `~/canteiro-timelapse/env/canteiro-ptz.env` (container) and `/etc/canteiro-ptz.env` (host copy) |
| credentials | `/etc/canteiro-ptz.env` (640 root:eduardocenci — CAM_HOST/CAM_USER/CAM_PASS, mirror of `.env` `ARA_CANTEIRO_CAM_KEY`) |

## Why there is no `goto <preset>` (empirical, 2026-08-24, fw 2.800.00IB00N.0.R)

Probed live over ONVIF (auth works — WS-UsernameToken digest, user `admin`,
device password):

| Operation | Result |
|---|---|
| `ContinuousMove` + `Stop` | **works** — physically moves the lens |
| `GetStatus` | HTTP 200 but the position is a **fake constant** (0.8, 0.8 forever) |
| `AbsoluteMove`, `RelativeMove` | `ter:ActionNotSupported/NotImplemented` fault, or accepted and inert |
| `GetPresets`/`SetPreset`/home | NotImplemented; PTZ node reports `MaximumNumberOfPresets=0` |

No position feedback + no absolute positioning = a reliable local
"goto preset" cannot be built on this firmware (dead-reckoning drifts and
cannot self-correct). The **Mibo app's favorite positions live in Imou's
cloud API only** — they are not exposed locally, which matches the wider
Imou family behavior. Recall of exact favorites therefore stays in the app;
this CLI covers scripted/remote *aiming* (nudge + look).

Also learned, and worth not re-deriving:

- **RTSP channel 1 (`canteiro`, on the bnu screen/TV) = the PT lens** =
  ONVIF `Profile000`/`VideoSource000`. Channel 2 (`canteiro-alt`) = fixed
  lens. (DOC-2026-197's lens descriptions predate the lens having moved.)
- The camera's **auto-tracking keeps re-aiming the PT lens** whenever it
  follows someone — any manually set framing is transient while tracking is
  enabled in the Mibo app.
- SOAP faults arrive with **HTTP 200**; parse the body.
- Snapshots must be **keyframe-only** (`ffmpeg -skip_frame nokey`) — HEVC
  mid-GOP grabs from the relay produce gray garbage.
