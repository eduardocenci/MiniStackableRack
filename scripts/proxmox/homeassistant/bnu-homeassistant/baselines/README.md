# Frigate digest baseline images

The digest LLM compares each event snapshot against a **baseline** (how the scene looks
when nothing is happening) to describe what changed. Drop one clean picture per camera
into `/config/frigate_baselines/` on bnu-homeassistant (via the **File editor** or
**Samba** add-on — the folder is root-owned, so the SSH `hassio` user can't write it).

## Filenames

`load_baselines()` prefers a time-of-day variant and falls back to a generic file, so
**either** is enough:

- Simple (one per camera): `<camera>.jpg`
- Better (day + night): `<camera>_day.jpg` and `<camera>_night.jpg`
  (day = 06:00–20:00, night otherwise)

## Cameras (bnu)

```
frente_principal.jpg
frente_pedestres.jpg
frente_overview.jpg
frente_garagem.jpg
lateral_direita.jpg
lateral_esquerda.jpg
fundos_overview.jpg
frente_campainha.jpg
```

Pick a frame with no people/vehicles. A baseline is optional per camera — any camera
without one is simply sent to the LLM without a comparison image. Images are gitignored
(too large); only this README and `.gitkeep` are committed.

## Scene-check ground truth (`/config/frigate_scene_baselines/`)

The **Ronda da Casa** scene check (`frigate_scene_check.py`) uses its own reference set at
`/config/frigate_scene_baselines/` — same filename convention as above (`<camera>_day.jpg` /
`<camera>_night.jpg`, generic `<camera>.jpg` fallback). These are stricter than digest
baselines: they must show the **"all clear" state** — varal empty, gates and doors closed,
nothing left outside — because the VLM reports exactly what differs from them.

You normally never upload these by hand. Put the house in the all-clear state and press
the **"Capturar referência"** button in HA (`input_button.frigate_scene_check_capture`):
the script snapshots every configured camera into the day or night slot for the current
hour (the previous file is kept as `.bak`). Do it once during the day and once after
20:00 for the night set. With the Ronda debug toggle on, the captured images are echoed
to the SmokeTests WhatsApp group so you can eyeball them.

A camera with no scene reference falls back to the digest baseline above; if neither
exists, the scene check **skips** that camera (comparing against nothing is the main
false-positive source).
