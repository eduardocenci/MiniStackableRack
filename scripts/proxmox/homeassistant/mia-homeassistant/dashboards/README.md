# mia-homeassistant dashboards

Storage-mode Lovelace dashboards on mia HA, kept here as the source of truth
and pushed over the websocket API (HA does not read these files).

| file | dashboard | apply with |
|---|---|---|
| `dashboard-main_ac_plotly_card.yaml` | sidebar **A/C** (`dashboard-main`), first section's graph card — plotly card, see header comments | `scripts/ha_lovelace_add_entities.py` / manual `lovelace/config/save` |
| `dashboard-power.yaml` | sidebar **Power** (`power-strips`): the three mia power strips (Tapo P316M via Matter, two Kasa HS300 via tplink) — per-outlet on/off, live watts, 6 h history graph per strip | `power_panel.py` (below) |
| `dashboard-sonos.yaml` | sidebar **Sonos** (`sonos-players`), one page, no tabs: HACS `custom:sonos-card` (punxaphil/custom-sonos-card v10.8.2, installed 2026-09-04) instantiated once per section over the three players — Living Room Arc Ultra, Study Era 100, Kitchen Era 100 — as three columns (now playing · groups/grouping/volumes · favorites/queue; all instances follow the same selected player); below them one entities card per speaker (Spotify favorite button row, EQ, night sound / speech enhancement / surround on the Arc) | `apply_dashboard.py dashboard-sonos.yaml` (below) |
| `dashboard-lights.yaml` | sidebar **Lights** (`led-lights`), one page: `light.all_wled` group tile on top, one column per WLED Gledopto controller (`wled_60` 93 LEDs, `wled_61` 59 LEDs, `wled_64` 119 LEDs, `wled_65` 127 LEDs — 68 on the rack plus a 59-LED tail, driven by the P2S and the sun; `wled_66` 24 LEDs in the guest bedroom (added 2026-09-08), brightness driven by the sun dial — its section at the bottom of the page — see below: tile with brightness, preset / playlist / palette selects, speed, intensity, nightlight, sync, sensors, firmware, restart), and a FancyLEDs (`192.168.2.63`) placeholder | `apply_dashboard.py dashboard-lights.yaml` |
| `dashboard-vacuum.yaml` | sidebar **Vacuum** (`robot-vacuum`), one page: Roborock Saros 10R tile (start/pause · stop · dock · locate) + live map (`image.living_room_saros_10r_mia`, `picture-entity`), a 3×3 room-button grid (raw `app_segment_clean` through `vacuum.send_command`, segment ids 1–8 from `roborock.get_maps` — HA 2026.8 has no per-room action), the three Roborock-app routines (`button.saros_10r_deep_*`), settings/DND, status, dock, consumables, 24 h history and a markdown identity card (192.168.2.73, local API 58867) | `apply_dashboard.py dashboard-vacuum.yaml` |
| `dashboard-study.yaml` | sidebar **Study button** (`study-button`), one page: the SONOFF SNZB-01P (ZHA, joined 2026-09-06) and its rule from `packages/study_button.yaml` — inputs (link switch, scene cycle, Sonos favourite, start volumes, vacuum segment, the speakers/vacuum/light it drives), decision markdown (what the next short/double/long press does and why, from `sensor.den_button`), executor (last action, router automation, three Run buttons that fire the same scripts as the button), the rule restated with the active branch marked, identity card | `apply_dashboard.py dashboard-study.yaml` |
| `dashboard-printer.yaml` | sidebar **Printer** (`3d-printer`), one page: the Bambu Lab P2S (HACS `greghesp/ha-bambulab` 2.2.25, `bambu_lab`, LAN mode — see below) — bundled `graphic` status card (P2S rendering with live overlays) beside the HACS PrintWatch card (evaluation), chamber camera (still every 10 s, tap for live), status/stage/task tiles, progress gauge, current-model cover image (conditional), chamber light + door; then the **LED strip .65 ← P2S** section (inputs, decision, strip state, rule card); then Print job · Temperatures & fans (6 h graph) · Filament (external spool + AMS 2 Pro trays/humidity/drying) + Health (print error and HMS messages rendered from the binary sensors' attributes by markdown templates), 24 h history and the identity card (192.168.2.72, serial 22E8AJ581301903) | `apply_dashboard.py dashboard-printer.yaml` |

```bash
python scripts/proxmox/homeassistant/mia-homeassistant/dashboards/power_panel.py          # entity names + dashboard
python scripts/proxmox/homeassistant/mia-homeassistant/dashboards/power_panel.py --panel  # dashboard only (after editing the yaml)
python scripts/proxmox/homeassistant/mia-homeassistant/dashboards/power_panel.py --names  # re-apply the P316M outlet names
```

```bash
python scripts/proxmox/homeassistant/mia-homeassistant/dashboards/apply_dashboard.py dashboard-sonos.yaml
```

**Editing the robot's map from HA (2026-09-06).** Segments are edited with raw robot commands
through `vacuum.send_command` (no response is visible; HA caches `get_maps` at load, so
**reload the `roborock` entry** afterwards to see the result). Merge two adjacent rooms:
`merge_segment` `[6, 9]` — the surviving segment (6) **loses its room name** (its iot id is
blanked on the robot; HA then shows "Room 6"). Restore names with `name_segment`
`[{"miRoomId": "<cloud room iot id>", "robotRoomId": <segment>}, …]` for **all** segments
(Valetudo's format; sending the full list is what the app does). The cloud room ids come from
`shell_command.roborock_rooms` (`packages/roborock_tools.yaml` + `/config/scripts/roborock_rooms.py`,
runs python-roborock inside the HA core container; call with `?return_response`) — the account
tokens stay inside HA (copying them to the desktop was refused on purpose). Done once: Room 9
("Bedroom", cloud id 14514296) merged into 6 = Guest bedroom (cloud id 20816057); map "MIA" now
has 8 segments. The merged segment's room-type `tag` is left at 1 (generic "Room"); the
Roborock app can reset the icon if it matters.

**Sonos `media_player.join` can time out on one member and leave HA's group view stale (2026-09-06).**
The Study button's "start" grouped Kitchen fine but the Arc Ultra threw `Timeout while waiting for
Sonos player to join the group Study` (transient — the same join succeeded seconds later); the script
aborted at that step, and afterwards HA still believed the Living Room was a *non-coordinator* member
(its card showed idle, but Play errored with `"play" can only be called/used on the coordinator in a
group`) while the speaker itself (`ZoneGroupTopology#GetZoneGroupState` on `.82:1400`) was alone.
Fix: reload the sonos entry
(`devtool.py ha mia POST /api/config/config_entries/entry/01M1FPJ58Z3WHKKZT1XE0DBM65/reload`) —
group attributes are correct again in ~10 s. The button packages now join members one at a time with
`continue_on_error`, so a slow member no longer blocks the volumes and the play.

**Saros 10R goes `unavailable` while the entry stays `loaded` (seen 2026-09-06 01:03 UTC,
~10 min into a Study clean, local TCP session to `.73:58867` still ESTABLISHED, robot
pingable): the python-roborock coordinator stalls and never recovers on its own — the
known Saros 10/10R local-protocol issue (home-assistant/core#152159). Fix is a reload:
`devtool.py ha mia POST /api/config/config_entries/entry/01KMXE08Y11YZTVE9N50DYF8WS/reload`
brought everything back in <10 s. If it recurs, add an automation that reloads the entry
when `vacuum.saros_10r` is unavailable for 5 min.

**Saros 10R "leaves the dock, comes back, washes, leaves again" at every start (checked
2026-09-06, 10 days of history + `get_clean_record`).** Firmware behaviour, not a fault: the dock
hot-air-dries the pads for 3 h after each run (`app_get_dryer_setting` dry_time 10800), so a
mopping task always begins with a pre-wet wash — undock 14-26 s → `going_to_wash_the_mop`
26-67 s → `washing_the_mop` 145-183 s → clean. Every one-room record shows `wash_count: 2`
(pre-wet + final) and `extra_time` ≈ 210 s; whole-flat runs add a wash every 15 min of mopping
(`get_smart_wash_params` smart_wash 0 / wash_interval 900, wash mode 10 = smart, hot wash).
No app setting or RPC skips the pre-wet; only a vacuum-only run does (the 10R auto-detaches the
pads: `isAutoTearDownMopSupported`). Untested idea: send `app_start_wash` and wait for
`charging` before `app_segment_clean` — same wash, no excursion. Settings/records are read with
`shell_command.roborock_query` (`REMOTE_ACCESS.md` → MIA site specifics).

**Bambu Lab P2S (2026-09-05).** Added through HACS (`python scripts/ha_hacs_install.py mia
greghesp/ha-bambulab integration`, HA restart, REST config flow in LAN mode — flow quirks and the
printer's network facts in `REMOTE_ACCESS.md` → *MIA site specifics*). The entry, the printer device,
the External Spool and the AMS 2 Pro were renamed `P2S` / `P2S External Spool` / `P2S AMS 2 Pro` and
every entity id shortened from `p2s_22e8aj581301903_*` to `p2s_*` (`config/entity_registry/update`
with `new_entity_id`); `sensor.p2s_remaining_time` shows one decimal (`display_precision`). The AMS
device only appeared after a **reload of the entry** (the first setup ran before the full MQTT state
had arrived) — expect the same after a printer power cycle during an HA restart, and rename the new
ids. Known gaps: **no control entities** (pause/resume/stop, fans, speed profile) until *Developer
Mode* is enabled on the printer (fw 01.02.00.00 wants signed MQTT commands), and **`camera.p2s_camera` only
loads with the printer's *LAN Mode Liveview* ON** (switched on 2026-09-05; off = `rtsp_url` "disable"
→ `urlparse` TypeError at setup in ha-bambulab 2.2.25, entity missing until Liveview is on and the entry is
reloaded). **Camera paths, verified in Chrome 2026-09-05:** the WebRTC live view (`picture-entity`,
`camera_view: live`, served by HA's go2rtc) plays when go2rtc holds the printer's slot; `/api/camera_proxy` stills work (1080p, ~1 s); HA's MJPEG
proxy (`/api/camera_proxy_stream`) returns the integration's black "!" placeholder (`camera_image()` is
hard-coded to it) — exactly what the bundled `print_status-card` `minimal` style shows in its camera pane, so
that style is not used. The printer accepts **one RTSPS client**: HA's legacy `stream` worker (HLS — spawned by
the frontend's WebRTC→HLS fallback or by `camera/stream` calls) and go2rtc then take turns timing out
(`go2rtc.server` "i/o timeout" + `stream.camera.p2s_camera` "Error demuxing stream" in the log); every reload with a `live`
card re-triggered that fallback and blanked the card, so the dashboard's camera card runs in **`camera_view: auto`**
(a go2rtc still every 10 s, tap it for the live view) and the two big cards are pinned to `rows: 12` so the
36-column grid does not stretch around them. Top row: the bundled `custom:ha-bambulab-print_status-card` in `graphic` style (P2S rendering with live
temperature/fan/progress overlays), **PrintWatch** (`custom:printwatch-card`, HACS `drkpxl/printwatch-card`
v1.2.0 — the only Bambu card in HACS, last release 2025-02-03, written for a P1S: stills camera works at
`camera_refresh_rate: 5000`, AMS/temps/progress fine, but "Left" shows the hours value as minutes, Pause/Stop
call `button.*` entities that need Developer Mode, and the print preview needs `image.p2s_cover_image`, which is
unavailable for cloud prints) — kept for comparison; remove its block from the yaml (and the plugin with
`hacs/repository/remove`) if not wanted — then the native camera picture-entity and tiles/gauge. The bundled `minimal` status card also renders the remaining time
negative ("-1h 54m", upstream). `custom:ha-bambulab-ams-card` (vector) and `custom:ha-bambulab-spool-card` sit in
the Filament column; all bundled cards take HA *device ids* (listed in the yaml). The bundled `print_control-card`
is omitted until Developer Mode provides the buttons it drives. Error / HMS cards are
markdown templates over `binary_sensor.p2s_print_error` (`code`, `error`) and
`binary_sensor.p2s_hms_errors` (`Count`, `<n>-Code/-Error/-Wiki/-Severity`); the tray list reads the
`sensor.p2s_ams_1_tray_<n>` attributes (`type`, `color`, `remain`, `active`, `empty`).

**LED strip .65 ← P2S (2026-09-06).** The fourth Gledopto (WLED 16.0.1, 127 RGBW LEDs: *Front of
Rack* 0–38 / *Side of Rack* 38–68 on the rack, then a tail 68–127 whose cap/plate light is 77–117 — presets
8 *Selective Cap/Plate* and 9 *Default + Cap/Plate*, 2026-09-06, mechanics in `REMOTE_ACCESS.md`; MAC `88:57:21:bc:01:9c`, UCG reservation `wled-bc019c`) was confirmed from
its zeroconf discovery, repointed to `192.168.2.65` with the WLED user flow (the discovery record carried a
stale host → `setup_retry`), renamed `WLED .65` / `*.wled_65_*` like the others and added to `light.all_wled`.
It follows the printer through `packages/p2s_led.yaml` — one readable rule: **door open → preset 5 "Door Open"**,
else **print issue → preset 3 "Print Issue"**, else **preset 4 "Default (Day-Night blend)"** dialled by the sun: preset 4 holds
the day look (Flow Stripe, = preset 9) and the night look (preset 7's Colorwaves) as two full-length layers blended additively,
plus the cap/plate segment, and `sensor.p2s_led_layers` turns the shared sun dial `sensor.sun_night_mix` (`packages/sun_night_mix.yaml`:
0 % above `input_number.sun_day_elevation` = 0°, 100 % below `input_number.sun_night_elevation` = −6°, 1 % steps, re-evaluated every
20 s from the sun's elevation interpolated between HA's own sunset/dusk times by `custom_templates/sun_model.jinja`) into the three
opacities — day 255·(1−m)^2.8, night 255·m^2.8, cap/plate between its day level and `input_number.p2s_led_cap_night_brightness` —
sent through `rest_command.wled_65_mix` with a 20 s crossfade per step, so the looks merge continuously while the light fades
(2026-09-07, made continuous and additive 2026-09-08 after a stepped, alpha-blended first version; WLED blend mechanics in
`REMOTE_ACCESS.md`). The same dial drives the guest bedroom strip's brightness (`packages/guest_led.yaml`: 100 % by day,
`input_number.guest_led_night_brightness` = 10 % at night, straight line in perceptual space, `rest_command.wled_66_brightness`,
never while the strip is off); a print issue is
`binary_sensor.p2s_print_error` on, print status `failed`, or an HMS message of severity serious/fatal ("common"
reminders do not count). The decision is a single template sensor, `sensor.p2s_led_preset` (state = preset id;
attributes `preset_name`, `because`, `rule`, `door`, `print_issue`), fed by `binary_sensor.p2s_print_issue`
(attribute `reason`); `automation.p2s_led_apply_preset` only sends the decided preset **id** to the strip through
`rest_command.wled_65_preset` (WLED JSON API `{"ps": id}`) — by id, because the first version selected by *name*
through `select.wled_65_preset` and broke within the hour when preset 4 was renamed "Default (Chase)" →
"Default (Flow Stripe)" ("Option … is not valid" in the automation trace); the names shown on the dashboard come
from the strip itself (`sensor.wled_65_presets`, a `rest` sensor polling `/presets.json` every 5 min, ids 3/4/5/7 as
attributes). `rest_command` and `rest` are start-up-only integrations: the package needed one HA restart, later
edits to the templates/automation only need `reload_all`. `input_boolean.p2s_led_link` switches the link off for
manual use. The Printer dashboard's
"LED strip .65" section shows every input, the decision with its reason, what the strip reports and the
automation's last run, plus a markdown card that re-states the rule with the currently active branch marked.

`apply_dashboard.py` is the generic applier: the yaml carries a `dashboard:` block
(the `lovelace/dashboards/create` payload, created once if the `url_path` is
missing) and a `config:` block (saved with `lovelace/config/save`). New
dashboards should use it; `dashboard-power.yaml` predates it and stays on
`power_panel.py`. The Sonos card itself comes from HACS
(`python scripts/ha_hacs_install.py mia punxaphil/custom-sonos-card`, resource
auto-registered under `/hacsfiles/custom-sonos-card/`). Sonos favorites in the
card's media browser come from `media_player.browse_media`, so the
integration-disabled `sensor.sonos_favorites` is not needed.

**Spotify on the speakers** goes through Sonos favorites (option chosen
2026-09-04): in the Sonos app open a Spotify playlist/album → ⋯ → *Add to My
Sonos*. Each favorite is a `mdi:spotify` button row at the top of the speaker's card (`media_player.play_media`, `media_content_type: favorite_item_id`,
`media_content_id: FV:2/<n>`) and also appears in the card's media browser.
**HA only re-reads favorites on integration reload** — a favorite added in the app
stayed invisible to `browse_media` (while a direct `ContentDirectory Browse FV:2`
on the speaker showed it) until
`devtool.py ha mia POST /api/config/config_entries/entry/<sonos entry_id>/reload`
(the websocket has no `config_entries/reload`). After adding favorites:

```bash
python scripts/proxmox/homeassistant/mia-homeassistant/dashboards/sonos_favorites.py   # entry id + reload cmd, then title / FV:2/<n> ids
```

then add a button per favorite/speaker in `dashboard-sonos.yaml` and re-apply.
Sonos Radio favorites without a stream URI are filtered out by HA and cannot be
played this way. Verified 2026-09-04: `FV:2/3` (Spotify "Summer Hits 2026")
played on the Study at volume 0.42. The Study player (Sonos zone renamed Den → Study
on the speaker itself via UPnP `DeviceProperties#SetZoneAttributes` on 2026-09-06; HA area
`study`, entity ids renamed `unnamed_room_unnamed_room_*` → `study_*`, e.g. `media_player.study`) —
only the device was renamed.

**WLED (2026-09-05).** The three Gledopto controllers were sitting as zeroconf
discoveries; they were confirmed through the REST config-flow API
(`GET/POST /api/config/config_entries/flow/<flow_id>`, empty body on the
`zeroconf_confirm` step). One entry still carried the pre-renumbering host
`192.168.2.138` (`setup_retry`); starting a user flow with `host: 192.168.2.64`
aborted `already_configured` **and repointed the entry** (the WLED flow updates
the host on abort), after which it loaded. All three call themselves
`WLED-Gledopto`, so HA produced `wled_gledopto`, `_2`, `_3` ids: devices got
`name_by_user` / entry titles `WLED .60/.61/.64` and every entity was renamed
`<domain>.wled_<octet>_<suffix>` via `config/entity_registry/update`
(`new_entity_id`). Rename again to rooms once known (also in the yaml).
`light.all_wled` is a **group helper** made through the `group` config flow
(`handler: group` → `next_step_id: light` → name + entities); note the group
did **not** follow the entity-id renames — its members were reset through the
options flow (`POST /api/config/config_entries/options/flow {handler: <entry_id>}`).

**FancyLEDs `192.168.2.63`** (MAC `d8:fc:92:f6:64:1f`, OUI Tuya Smart Inc.): no
TCP port open (6668/80/443 refused) and no Tuya UDP 6666/6667 broadcasts in 25 s
(the `.62` Atomi does broadcast), so **no local control** — neither `tuya_local`
nor LocalTuya can work. Path: pair it into the Smart Life app, then add the core
**Tuya** integration (QR login, Eduardo does the login), then put its `light.*`
in the FancyLEDs section of the yaml.

`power_panel.py` holds the P316M outlet → name mapping (`P316M_OUTLETS`) and
names the switch, its `power_on_behavior` select and the five meter sensors of
each outlet (Matter endpoint N ↔ meter N+6). Auth comes from `MIA_HA_URL` /
`MIA_HA_TOKEN` through `scripts/devtool.py` (`devtool.ENV`); needs the local
`websockets` and `pyyaml` packages.

The Tapo has no strip-level meter, so the heading badge uses
`sensor.power_strip_rack_total_power` — a **Template helper** (UI-created,
2026-09-04, via the `template` config flow: sum of `sensor.den_power_strip_rack_power_7..12`,
unit W, device_class power, state_class measurement). Its display precision
and the P316M meters' (`options.sensor.display_precision = 1`) were set via
`config/entity_registry/update` so all three strips show one decimal.

Gotchas: HA rejects dashboard `url_path`s without a hyphen (`power` → invalid,
`power-strips` ok). Renaming an entity from the panel (row → ⚙ → Name) writes
the entity registry directly; re-running `--names` overwrites the P316M names
with the repo mapping, so update `P316M_OUTLETS` first.
