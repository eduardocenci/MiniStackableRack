"""Install a HACS repository via the HACS websocket API.

usage: python scripts/ha_hacs_install.py <site> <owner/repo> [plugin|integration]

- plugin (default): Lovelace card; the resource is auto-registered under /hacsfiles/...
- integration: custom component dropped into /config/custom_components/<domain>/ - HA must be
  RESTARTED before its config flow exists (POST /api/services/homeassistant/restart), then add it
  with POST /api/config/config_entries/flow {"handler": "<domain>"}.
"""
import json, sys, os, time
from websockets.sync.client import connect

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import devtool  # noqa: E402

site, full_name = sys.argv[1:3]
category = sys.argv[3] if len(sys.argv) > 3 else "plugin"
assert category in ("plugin", "integration"), category
token = devtool.ENV[f"{site.upper()}_HA_TOKEN"]
base = devtool.ENV[f"{site.upper()}_HA_URL"].rstrip("/").replace("http://", "ws://").replace("https://", "wss://")

_id = 0
def call(ws, msg, timeout=120):
    global _id
    _id += 1
    msg["id"] = _id
    ws.send(json.dumps(msg))
    t0 = time.time()
    while True:
        r = json.loads(ws.recv(timeout=timeout))
        if r.get("id") == _id and r.get("type") == "result":
            return r
        if time.time() - t0 > timeout:
            raise TimeoutError(msg)

with connect(f"{base}/api/websocket", max_size=50_000_000) as ws:
    assert json.loads(ws.recv())["type"] == "auth_required"
    ws.send(json.dumps({"type": "auth", "access_token": token}))
    assert json.loads(ws.recv())["type"] == "auth_ok", "auth failed"

    r = call(ws, {"type": "hacs/repositories/list", "categories": [category]})
    assert r["success"], r
    repos = r["result"]
    match = [x for x in repos if x.get("full_name", "").lower() == full_name.lower()]
    print(f"{len(repos)} {category} repos known to HACS; match: {[ (m['id'], m.get('installed'), m.get('available_version')) for m in match]}")
    if not match:
        r = call(ws, {"type": "hacs/repository/add", "repository": full_name, "category": category})
        print("add:", r)
        r = call(ws, {"type": "hacs/repositories/list", "categories": [category]})
        match = [x for x in r["result"] if x.get("full_name", "").lower() == full_name.lower()]
        assert match, "repository still not listed after add"
    repo = match[0]
    if repo.get("installed"):
        print("already installed, version", repo.get("installed_version"))
    else:
        r = call(ws, {"type": "hacs/repository/download", "repository": repo["id"]}, timeout=300)
        print("download:", r)
        assert r["success"], r
        r = call(ws, {"type": "hacs/repositories/list", "categories": [category]})
        repo = [x for x in r["result"] if x["id"] == repo["id"]][0]
        print("installed:", repo.get("installed"), repo.get("installed_version"))

    if category == "plugin":
        r = call(ws, {"type": "lovelace/resources"})
        print("lovelace resources:", [x["url"] for x in r["result"]])
    else:
        print("integration downloaded - restart HA, then create its config entry via the config flow")
