# Documentation workflow

How to add and maintain documentation for the MiniStackableRack fleet.
Everything lives in the **globalnet** submodule and is served privately by the
dashboard container at **`/docs`** (`http://bnu-raspberrypi:5001/docs`) —
deploying docs is the same `push → merge → DockerHub → Pi cron` flow as code.

## Where things live

| Content | Location (in `globalnet/`) |
|---|---|
| Device inventory & diagrams | **generated** from `architecture.yaml` — never edit `docs/architecture/` by hand |
| Runbooks (how it works / how to fix it) | `docs/runbooks/*.md` |
| Device reference: purchase links, manuals, notes | `docs/devices/index.md` (or one page per device) |
| PDF manuals, datasheets, photos | `docs/_static/manuals/` |

## Adding a purchase link or manual

1. Drop the file in `globalnet/docs/_static/manuals/` (e.g. `slzb-06.pdf`).
2. Add a row/link in `globalnet/docs/devices/index.md`:

   ```markdown
   | SLZB-06 | Zigbee PoE coordinator | [store](https://smlight.tech/...) | [manual](../_static/manuals/slzb-06.pdf) |
   ```

3. Build and test locally, then commit + PR:

   ```bash
   cd globalnet
   make docs     # regenerates architecture pages + builds Sphinx into static/docs
   make check    # 30+ schema/API/docs gates
   ```

## Adding a device-specific page

1. Create `globalnet/docs/devices/<device>.md` (MyST Markdown — headings,
   tables, images, Mermaid diagrams all work).
2. Reference it from the device's node in `architecture.yaml` so the
   dashboard's **Docs** button deep-links to it:

   ```yaml
   doc: devices/<device>
   ```

3. Add it to the toctree in `docs/devices/index.md`, then `make docs` + PR.

## Adding a runbook

Same pattern: create `docs/runbooks/<topic>.md`, add it to the toctree in
`docs/runbooks/index.md`, point relevant `doc:` slugs in `architecture.yaml`
at it. Write for future-you at 2 a.m.: what it is, where config lives, what
to check when it breaks.

## Ingesting documentation (manuals, photos, stickers)

For raw material you don't want to file by hand — device manuals, photos,
photos of stickers with serials/credentials — use the **`ingest-docs`** skill
(`globalnet/.claude/skills/ingest-docs/SKILL.md`). It OCRs photos, files the
originals, updates the right device page, and opens a **digest pull request**
you review and merge. Two ways to feed it, use either:

1. **Drop in a Claude chat** (on demand): in the Claude desktop app on the
   globalnet repo, attach the photos/PDFs and say what they are
   ("this is the FLN router sticker, has the admin password"). It processes
   immediately and opens the PR.
2. **Drop in the Drive inbox** (hands-off): put files in the Google Drive
   folder **`MiniStackableRack Inbox`** with a short `.txt` note per batch
   saying which device/region. A daily scheduled task sweeps it and opens the
   PR. (Runs while the Claude app is open, or on next launch if a run was
   missed.)

**Sensitivity — two tiers, automatic:**

- Manuals and harmless photos → `docs/_static/…` (browsable at `/docs`).
- Credential/serial **stickers** → `globalnet/vault/<region>/<device>/` —
  git-tracked in the private repo but **excluded from the Docker image**
  (the image on DockerHub is public; the Dockerfile only copies
  `app.py`, `static/`, `devices.json`, `architecture.yaml`). Credential
  *values* are additionally extracted to `.env`; the docs page references the
  `.env` key, never the value.

Nothing is filed until you merge the digest PR, so you always get a review.

## Rules of thumb

- `architecture.yaml` is the **single source of truth** for devices — hardware
  specs, IPs, monitoring, links. Facts about a device go there first; prose
  goes in docs.
- `make check` gates everything: broken doc slugs, malformed yaml, and Sphinx
  warnings (`-W`) all fail CI.
- No secrets in docs — credential *names* (`.env` keys) are fine, values never.
- Credential stickers live in `globalnet/vault/`, never in `docs/` or
  `static/` (those ship in the public image).
