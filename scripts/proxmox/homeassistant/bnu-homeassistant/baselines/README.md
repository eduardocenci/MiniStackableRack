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
