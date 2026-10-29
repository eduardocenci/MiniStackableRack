"""Render every card stage for the logged flights that still have a playback,
WITHOUT sending anything — the visual check for a cards.py change.

Runs INSIDE the container, from the data volume (the Dockerfile does not copy
it). A patched cards.py dropped next to it in /data/_test shadows /app/cards.py,
so a layout change can be checked before the rebuild:

  pct push 101 cards.py     /opt/psvis-tracker/data/_test/cards.py      # optional
  pct push 101 render_test.py /opt/psvis-tracker/data/_test/render_test.py
  pct exec 101 -- docker exec -w /data/_test psvis-tracker python render_test.py [fid ...]

Output: /data/_test/out/<fid>-{takeoff,enroute,1,2}.png — take-off and en-route
are simulated from the playback like `sim:true` (trail cut at wheels-up /
T+10, no destination hindsight); METAR and terrain come from the network, so
a full run of ~10 flights takes a few minutes: launch it with nohup and a log.
"""
import gzip
import json
import os
import sys
import traceback

sys.path.insert(0, "/data/_test")
sys.path.insert(1, "/app")

import cards  # noqa: E402  (the /data/_test copy when there is one)
import db  # noqa: E402
import enroute  # noqa: E402
import report  # noqa: E402

OUT = "/data/_test/out"
os.makedirs(OUT, exist_ok=True)
print("cards.py from", cards.__file__, flush=True)


def attach_expectation(flight, stats):
    """Same rule as app._attach_expectation (app.py is not imported — Flask)."""
    ap = flight.get("airport") or {}
    o, d = (((ap.get(side) or {}).get("code") or {}).get("icao") for side in ("origin", "destination"))
    if not o or not d:
        return
    fid = stats.get("fr24_id")
    durs = db.route_durations(o, d, exclude_fid=fid)
    reverse = not durs
    if reverse:
        durs = db.route_durations(d, o, exclude_fid=fid)
    if durs:
        stats["expected"] = {"mean_s": sum(durs) / len(durs), "n": len(durs),
                             "fastest_s": min(durs), "reverse": reverse}


def fill_airports_from_db(flight, fid):
    """The stored playback may lack a side the list knew — take it from the row."""
    with db._conn() as c:
        row = c.execute("SELECT o_iata,o_icao,o_name,o_city,o_lat,o_lon,"
                        "d_iata,d_icao,d_name,d_city,d_lat,d_lon FROM flights WHERE fr24_id=?",
                        (fid,)).fetchone()
    if not row:
        return
    ap = flight.setdefault("airport", {}) or {}
    flight["airport"] = ap
    for side, off in (("origin", 0), ("destination", 6)):
        iata, icao, name, city, lat, lon = row[off:off + 6]
        if not ap.get(side) and icao:
            ap[side] = {"code": {"iata": iata, "icao": icao}, "name": name,
                        "position": {"latitude": lat, "longitude": lon, "region": {"city": city}}}


def render(fid):
    with gzip.open(f"/data/playbacks/{fid}.json.gz", "rt", encoding="utf-8") as fh:
        flight = json.load(fh)
    fill_airports_from_db(flight, fid)
    origin, track, _ = enroute.from_playback(flight)
    dep = enroute.takeoff_ts(track)

    try:  # take-off (sim: trail cut at wheels-up)
        png, _cap = cards.takeoff_card(origin, dep, [p for p in track if p["timestamp"] <= dep + 60],
                                       fid, next_ts=dep + 600)
        open(f"{OUT}/{fid}-takeoff.png", "wb").write(png)
    except Exception:
        traceback.print_exc()

    try:  # en-route at T+10 (sim: no destination hindsight)
        st = enroute.analyze(origin, enroute.truncate_after_takeoff(track, 600), None, exclude_fid=fid)
        open(f"{OUT}/{fid}-enroute.png", "wb").write(cards.enroute_card(st, fid))
    except Exception:
        traceback.print_exc()

    try:  # landing 1/2 + 2/2
        stats = report.compute_stats(flight)
        attach_expectation(flight, stats)
        p1, p2 = cards.landing_cards(flight, stats)
        open(f"{OUT}/{fid}-1.png", "wb").write(p1)
        open(f"{OUT}/{fid}-2.png", "wb").write(p2)
    except Exception:
        traceback.print_exc()
    print("rendered", fid, flush=True)


if __name__ == "__main__":
    fids = sys.argv[1:] or sorted(f[:-8] for f in os.listdir("/data/playbacks") if f.endswith(".json.gz"))
    for fid in fids:
        render(fid)
