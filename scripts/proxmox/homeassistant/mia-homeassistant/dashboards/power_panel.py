"""Apply the mia 'Power' dashboard (dashboard-power.yaml) and the outlet entity names
to mia HA over the websocket API.

    python scripts/proxmox/homeassistant/mia-homeassistant/dashboards/power_panel.py          # names + dashboard
    python scripts/proxmox/homeassistant/mia-homeassistant/dashboards/power_panel.py --names  # entity names only
    python scripts/proxmox/homeassistant/mia-homeassistant/dashboards/power_panel.py --panel  # dashboard only

Storage-mode dashboard: lovelace/dashboards/create (once) + lovelace/config/save.
Entity names: config/entity_registry/update (friendly names only; entity ids untouched).
Same auth pattern as scripts/ha_lovelace_add_entities.py (MIA_HA_URL / MIA_HA_TOKEN via devtool.ENV).
"""
from __future__ import annotations

import json
import os
import sys

import yaml
from websockets.sync.client import connect

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import devtool  # noqa: E402

SITE = "mia"
URL_PATH = "power-strips"  # HA requires a hyphen in dashboard url paths
DASHBOARD = dict(url_path=URL_PATH, title="Power", icon="mdi:power-plug-outline",
                 show_in_sidebar=True, require_admin=False, mode="storage")

# Tapo P316M "Power Strip Rack": Matter endpoint N (1-6) = outlet N — its switch AND its meters.
# (Commissioned 2026-09-04 with the meters on endpoints 7-12; on 2026-09-05 the strip re-presented
# them on 1-6, HA created new *_1..6 sensors and the *_7..12 ones went unavailable for good —
# fixed here 2026-11-24.) Outlet names from Eduardo, 2026-09-04.
P316M_OUTLETS = {
    1: "Verizon 5G Home",
    2: "Outlet 2 (empty)",
    3: "Outlet 3 (empty)",
    4: "Rack (excl. NAS & Pi)",
    5: "NAS",
    6: "Raspberry Pi + Monitor",
}
METERS = {"power": "power", "energy": "energy", "energy_exported": "energy exported",
          "effective_voltage": "voltage", "effective_current": "current"}


DEVICE = "Power Strip Rack"  # HA does not prefix these Matter entities with the device name,
                             # so carry it in the name — same look as the HS300 "Power Strip Desk <outlet>"


def entity_names() -> dict[str, str]:
    names = {}
    for n, label in P316M_OUTLETS.items():
        names[f"switch.den_power_strip_rack_switch_{n}"] = f"{DEVICE} {label}"
        names[f"select.den_power_strip_rack_power_on_behavior_{n}"] = f"{DEVICE} {label} power-on behavior"
        for suffix, word in METERS.items():
            names[f"sensor.den_power_strip_rack_{suffix}_{n}"] = f"{DEVICE} {label} {word}"
    return names


class HA:
    def __init__(self, site: str):
        token = devtool.ENV[f"{site.upper()}_HA_TOKEN"]
        base = devtool.ENV[f"{site.upper()}_HA_URL"].rstrip("/")
        base = base.replace("http://", "ws://").replace("https://", "wss://")
        self.ws = connect(f"{base}/api/websocket", max_size=10_000_000)
        assert json.loads(self.ws.recv())["type"] == "auth_required"
        self.ws.send(json.dumps({"type": "auth", "access_token": token}))
        assert json.loads(self.ws.recv())["type"] == "auth_ok", "auth failed"
        self.n = 0

    def call(self, **msg):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, **msg}))
        while True:
            r = json.loads(self.ws.recv())
            if r.get("id") == self.n:
                if not r.get("success", True):
                    raise RuntimeError(f"{msg.get('type')}: {r.get('error')}")
                return r.get("result")


def apply_names(ha: HA) -> None:
    ok = skipped = 0
    for entity_id, name in entity_names().items():
        try:
            ha.call(type="config/entity_registry/update", entity_id=entity_id, name=name)
            ok += 1
        except RuntimeError as err:
            skipped += 1
            print(f"  skip {entity_id}: {err}")
    print(f"entity names: {ok} set, {skipped} skipped")


def apply_panel(ha: HA) -> None:
    with open(os.path.join(HERE, "dashboard-power.yaml"), encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    existing = {d["url_path"]: d for d in ha.call(type="lovelace/dashboards/list")}
    if URL_PATH not in existing:
        ha.call(type="lovelace/dashboards/create", **DASHBOARD)
        print(f"dashboard '{URL_PATH}' created")
    ha.call(type="lovelace/config/save", url_path=URL_PATH, config=config)
    views = config["views"][0]
    cards = sum(len(s.get("cards", [])) for s in views.get("sections", []))
    print(f"dashboard '{URL_PATH}' saved: {len(views['sections'])} sections, {cards} cards")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "--all"
    ha = HA(SITE)
    if what in ("--all", "--names"):
        apply_names(ha)
    if what in ("--all", "--panel"):
        apply_panel(ha)
