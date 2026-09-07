import os
import json, sys, os, yaml
from websockets.sync.client import connect
REPO=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO,"scripts")); import devtool  # noqa
tok=devtool.ENV["MIA_HA_TOKEN"]; base=devtool.ENV["MIA_HA_URL"].rstrip("/").replace("http://","ws://").replace("https://","wss://")
url_path=sys.argv[1]; card_yaml_path=sys.argv[2]
card=yaml.safe_load(open(card_yaml_path,encoding="utf-8"))
with connect(f"{base}/api/websocket",max_size=20_000_000) as ws:
    ws.recv(); ws.send(json.dumps({"type":"auth","access_token":tok})); assert json.loads(ws.recv())["type"]=="auth_ok"
    ws.send(json.dumps({"id":1,"type":"lovelace/config","url_path":url_path})); cfg=json.loads(ws.recv())["result"]
    view=cfg["views"][0]
    secs=view.get("sections")
    if secs is None:
        view.setdefault("cards",[]).insert(0,card)
    else:
        # add as its own new section at the top so it spans full width
        secs.insert(0,{"type":"grid","cards":[{"type":"heading","heading":"Floor plan"},card]})
    ws.send(json.dumps({"id":2,"type":"lovelace/config/save","url_path":url_path,"config":cfg}))
    print("save:", json.loads(ws.recv()).get("success"))

