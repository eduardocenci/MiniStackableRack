import os
import json, sys, os
from websockets.sync.client import connect
REPO=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO,"scripts")); import devtool  # noqa
tok=devtool.ENV["MIA_HA_TOKEN"]; base=devtool.ENV["MIA_HA_URL"].rstrip("/").replace("http://","ws://").replace("https://","wss://")
name=sys.argv[1]; tracker=sys.argv[2]
with connect(f"{base}/api/websocket",max_size=10_000_000) as ws:
    ws.recv(); ws.send(json.dumps({"type":"auth","access_token":tok})); assert json.loads(ws.recv())["type"]=="auth_ok"
    ws.send(json.dumps({"id":1,"type":"person/list"})); r=json.loads(ws.recv())
    existing=[p for p in (r.get("result",{}).get("storage",[]) if isinstance(r.get("result"),dict) else r.get("result",[])) if isinstance(p,dict) and p.get("name")==name]
    if existing: print("person already exists:", name); sys.exit(0)
    ws.send(json.dumps({"id":2,"type":"person/create","name":name,"device_trackers":[tracker]}))
    res=json.loads(ws.recv()); print("person/create:", res.get("success"), json.dumps(res.get("result",res))[:300])

