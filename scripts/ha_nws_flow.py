import os
import json, sys, os, urllib.request
REPO=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO,"scripts")); import devtool  # noqa
base=devtool.ENV["MIA_HA_URL"].rstrip("/"); tok=devtool.ENV["MIA_HA_TOKEN"]
H={"Authorization":"Bearer "+tok,"Content-Type":"application/json"}
API_KEY=sys.argv[1] if len(sys.argv)>1 else "casa-mia-ha"
def post(path,body):
    req=urllib.request.Request(base+path,data=json.dumps(body).encode(),headers=H,method="POST")
    try:
        return json.loads(urllib.request.urlopen(req,timeout=30).read())
    except urllib.error.HTTPError as e:
        return {"_http_error":e.code,"body":e.read().decode()[:400]}
r=post("/api/config/config_entries/flow",{"handler":"nws","show_advanced_options":False})
print("init:", json.dumps(r)[:400])
fid=r.get("flow_id")
if not fid: print("no flow_id (already configured?)"); sys.exit(0)
# adapt: menu -> pick 'location'; then form -> submit fields
if r.get("type")=="menu":
    opts=r.get("menu_options",[]); nxt="location" if "location" in opts else (opts[0] if opts else "location")
    r=post(f"/api/config/config_entries/flow/{fid}",{"next_step_id":nxt}); print("menu->",nxt,":",json.dumps(r)[:300])
fields={"api_key":API_KEY,"latitude":25.7617,"longitude":-80.1918,"station":"KMIA"}
r=post(f"/api/config/config_entries/flow/{fid}",fields); print("submit:", json.dumps(r)[:500])

