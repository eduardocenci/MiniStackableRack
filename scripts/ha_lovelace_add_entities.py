"""Add entities to the history-graph card of an HA storage-mode dashboard via the websocket API.

usage: python scripts/ha_lovelace_add_entities.py <site> <url_path> <entity1> [entity2 ...]
"""
import json, sys, os
from websockets.sync.client import connect

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import devtool  # noqa: E402

site, url_path, *entities = sys.argv[1:]
token = devtool.ENV[f"{site.upper()}_HA_TOKEN"]
base = devtool.ENV[f"{site.upper()}_HA_URL"].rstrip("/").replace("http://", "ws://").replace("https://", "wss://")

with connect(f"{base}/api/websocket", max_size=10_000_000) as ws:
    assert json.loads(ws.recv())["type"] == "auth_required"
    ws.send(json.dumps({"type": "auth", "access_token": token}))
    assert json.loads(ws.recv())["type"] == "auth_ok", "auth failed"

    ws.send(json.dumps({"id": 1, "type": "lovelace/config", "url_path": url_path}))
    cfg = json.loads(ws.recv())
    assert cfg["success"], cfg
    config = cfg["result"]

    graphs = []
    for view in config["views"]:
        for section in view.get("sections", []):
            for card in section.get("cards", []):
                if card.get("type") == "history-graph":
                    graphs.append(card)
        for card in view.get("cards", []):
            if card.get("type") == "history-graph":
                graphs.append(card)
    assert len(graphs) == 1, f"expected exactly one history-graph card, found {len(graphs)}"
    card = graphs[0]
    have = {e["entity"] if isinstance(e, dict) else e for e in card["entities"]}
    added = []
    for ent in entities:
        if ent not in have:
            card["entities"].append({"entity": ent})
            added.append(ent)
    print("history-graph entities now:", [e["entity"] if isinstance(e, dict) else e for e in card["entities"]])
    if not added:
        print("nothing to add")
        sys.exit(0)

    ws.send(json.dumps({"id": 2, "type": "lovelace/config/save", "url_path": url_path, "config": config}))
    res = json.loads(ws.recv())
    assert res["success"], res
    print("saved; added", added)
