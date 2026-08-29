"""psvis-tracker — post-flight report for PS-VIS.

Home Assistant POSTs /report when the Flightradar24 integration fires a landing
event for PS-VIS at Blumenau. This service then pulls the flight's full track
from FR24 playback (retrying while FR24 finalizes it), computes cruise
altitude/speed/duration/distance, renders the altitude+speed profile chart and
sends it to the WhatsApp group via WAHA.

The chart PNG is served from /charts/<id>.png because WAHA Core sends images by
URL — WAHA fetches it over the shared `waha_default` docker network.
"""
import glob
import json
import logging
import os
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory

import db
import enroute
import fr24
import report
import waha

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("psvis")

REG = os.environ.get("REG", "PS-VIS")
GROUP_JID = os.environ.get("GROUP_JID", "")
TEST_GROUP_JID = os.environ.get("TEST_GROUP_JID", "")
SELF_URL = os.environ.get("SELF_URL", "http://psvis-tracker:8000")
INITIAL_DELAY_S = int(os.environ.get("INITIAL_DELAY_S", "120"))
RETRY_S = int(os.environ.get("RETRY_S", "60"))
MAX_TRIES = int(os.environ.get("MAX_TRIES", "10"))
# 15 min: the sweep is the UNIVERSAL capture path — a landing at any airport
# with no HA event still gets stored and reported within one interval.
SYNC_INTERVAL_S = int(os.environ.get("SYNC_INTERVAL_S", "900"))
# T+10 en-route update after take-off (FR24 rarely knows the destination then)
ENROUTE_DELAY_S = int(os.environ.get("ENROUTE_DELAY_S", "600"))
# Airborne watch: HA take-off events are only guaranteed near Blumenau, so the
# tracker polls the FR24 list itself — a take-off ANYWHERE is noticed within
# one interval, gets its text (non-BNU origins; HA already covers BNU) and its
# T+10 en-route update scheduled from the real departure time.
AIRBORNE_POLL_S = int(os.environ.get("AIRBORNE_POLL_S", "300"))
HOME_ICAO = os.environ.get("HOME_ICAO", "SSBL")  # HA announces this one itself
DATA_DIR = os.environ.get("DATA_DIR", "/data")
CHARTS_DIR = os.path.join(DATA_DIR, "charts")
LAST_FILE = os.path.join(DATA_DIR, "last_reported")

os.makedirs(CHARTS_DIR, exist_ok=True)
app = Flask(__name__)


def _already_reported(fid):
    try:
        with open(LAST_FILE, encoding="utf-8") as fh:
            return fid in fh.read().splitlines()[-20:]
    except FileNotFoundError:
        return False


def _mark_reported(fid):
    with open(LAST_FILE, "a", encoding="utf-8") as fh:
        fh.write(fid + "\n")


def _resolve_flight_id(flight_id):
    """Use the id HA passed if FR24 confirms it; otherwise newest landed."""
    if flight_id:
        return flight_id
    entry = fr24.latest_landed(REG, now_ts=time.time())
    return (entry or {}).get("identification", {}).get("id")


def _send_report(fid, flight, stats, jid):
    """Render chart+map and send the single flight message via WAHA."""
    png = report.build_report_image(stats)
    with open(os.path.join(CHARTS_DIR, f"{fid}.png"), "wb") as fh:
        fh.write(png)
    waha.send_image(jid, f"{SELF_URL}/charts/{fid}.png", report.build_caption(stats))
    _mark_reported(fid)
    log.info("report for %s sent to %s", fid, jid)


def _run_report(flight_id, jid, force, fallback_text=""):
    if not force and INITIAL_DELAY_S:
        time.sleep(INITIAL_DELAY_S)
    for attempt in range(1, MAX_TRIES + 1):
        try:
            fid = _resolve_flight_id(flight_id)
            if not fid:
                raise LookupError("no landed flight found on FR24 yet")
            if not force and _already_reported(fid):
                log.info("flight %s already reported — skipping", fid)
                return
            flight = fr24.playback(fid)
            stats = report.compute_stats(flight)  # raises while track is short
            try:  # flight log first — a WAHA hiccup must not lose the record
                db.store_flight(flight, stats)
            except Exception:
                log.warning("flight log store failed for %s", fid, exc_info=True)
            _send_report(fid, flight, stats, jid)
            return
        except Exception as exc:  # noqa: BLE001 — retry on anything, FR24 lags
            log.warning("attempt %d/%d failed: %s", attempt, MAX_TRIES, exc)
            if attempt < MAX_TRIES:
                time.sleep(RETRY_S)
    log.error("giving up on flight report (flight_id=%s)", flight_id)
    # The landing alert must never be lost: HA delegated the whole message to
    # us, so on total FR24 failure send its pre-rendered text (no chart/cruise).
    if fallback_text:
        waha.send_text(jid, fallback_text)
        log.info("fallback text sent to %s", jid)


def _run_enroute(flight_id, jid, force, sim, delay_s=None):
    """T+10 after take-off: cardinal heading, route-so-far map and arrival
    estimates for previously-seen destinations along the heading. May be
    triggered by the HA event AND by the airborne watch — the claim marker
    below makes whichever wakes first the only sender."""
    delay = ENROUTE_DELAY_S if delay_s is None else delay_s
    if not force and delay > 0:
        time.sleep(delay)
    fid = flight_id
    if not fid:
        try:
            entry = fr24.latest_airborne(REG)
            fid = (entry or {}).get("identification", {}).get("id")
        except Exception:  # noqa: BLE001
            pass
    if not fid:
        log.warning("en-route: no airborne flight found on FR24")
        return
    key = f"enroute:{fid}"
    if not force:
        if _already_reported(key):
            return
        _mark_reported(key)  # claim it before the slow build
    for attempt in range(1, 4):
        try:
            try:
                origin, track, dest_hint = enroute.from_clickhandler(fr24.live_details(fid))
                if len(track) < 5:
                    raise ValueError("live trail empty")
            except Exception:  # not live any more (or endpoint hiccup)
                origin, track, dest_hint = enroute.from_playback(fr24.playback(fid))
            if sim:  # test path: pretend we are ENROUTE_DELAY_S into the flight
                track = enroute.truncate_after_takeoff(track, ENROUTE_DELAY_S)
            caption, png = enroute.build(origin, track, dest_hint, exclude_fid=fid)
            name = f"{fid}-enroute.png"
            with open(os.path.join(CHARTS_DIR, name), "wb") as fh:
                fh.write(png)
            waha.send_image(jid, f"{SELF_URL}/charts/{name}", caption)
            log.info("en-route update for %s sent to %s", fid, jid)
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("en-route attempt %d/3 failed: %s", attempt, exc)
            if attempt < 3:
                time.sleep(RETRY_S)
    log.error("giving up on en-route update (flight_id=%s)", fid)


def _takeoff_text(entry):
    """Plain take-off announcement built from a flight-list entry — used for
    take-offs away from home, where no HA event announces anything."""
    ap = entry.get("airport") or {}

    def side_city(side):
        a = ap.get(side) or {}
        code = a.get("code") or {}
        pos = a.get("position") or {}
        return (((pos.get("region") or {}).get("city"))
                or a.get("name") or code.get("iata") or code.get("icao") or "")

    o_city, d_city = side_city("origin"), side_city("destination")
    dep = ((entry.get("time") or {}).get("real") or {}).get("departure")
    fid = (entry.get("identification") or {}).get("id")
    lines = [f"🛫 *PS-VIS decolou de {o_city}*" if o_city else "🛫 *PS-VIS decolou*"]
    if d_city:
        lines.append(f"✈️ Destino: {d_city}")
    if dep:
        from report import TZ_LOCAL
        lines.append(f"🕐 Decolagem {datetime.fromtimestamp(dep, TZ_LOCAL).strftime('%H:%M')}")
    if fid:
        lines.append(f"🔗 https://www.flightradar24.com/data/aircraft/ps-vis#{fid}")
    return "\n".join(lines)


def _airborne_check():
    entry = fr24.latest_airborne(REG)
    if not entry:
        return
    fid = (entry.get("identification") or {}).get("id")
    dep = ((entry.get("time") or {}).get("real") or {}).get("departure")
    if not fid or not dep:
        return
    now = time.time()
    if now - dep > 45 * 60:
        return  # stale — the landing sweep owns it from here
    o_code = ((entry.get("airport") or {}).get("origin") or {}).get("code") or {}
    if (GROUP_JID and o_code.get("icao") != HOME_ICAO
            and not _already_reported(f"takeoff:{fid}")):
        _mark_reported(f"takeoff:{fid}")
        waha.send_text(GROUP_JID, _takeoff_text(entry))
        log.info("remote take-off text for %s sent", fid)
    if GROUP_JID and not _already_reported(f"enroute:{fid}"):
        delay = max(0, dep + ENROUTE_DELAY_S - now)
        threading.Thread(
            target=_run_enroute, args=(fid, GROUP_JID, False, False, delay), daemon=True
        ).start()


def _airborne_loop():
    while True:
        try:
            _airborne_check()
        except Exception as exc:  # noqa: BLE001
            log.warning("airborne watch failed: %s", exc)
        time.sleep(AIRBORNE_POLL_S)


@app.post("/report")
def report_endpoint():
    body = request.get_json(silent=True) or {}
    direction = body.get("direction", "landed")
    test = bool(body.get("test"))
    jid = body.get("chat_jid") or (TEST_GROUP_JID if test else GROUP_JID)
    if not jid:
        return jsonify(status="error", reason="no chat JID configured"), 500
    flight_id = (body.get("flight_id") or "").strip() or None
    force = bool(body.get("force") or test)
    if direction == "landed":
        fallback_text = body.get("fallback_text") or ""
        threading.Thread(
            target=_run_report, args=(flight_id, jid, force, fallback_text), daemon=True
        ).start()
        return jsonify(status="accepted", flight_id=flight_id, test=test), 202
    if direction == "took_off":
        threading.Thread(
            target=_run_enroute,
            args=(flight_id, jid, force, bool(body.get("sim"))),
            daemon=True,
        ).start()
        return jsonify(status="enroute-scheduled", flight_id=flight_id, test=test), 202
    return jsonify(status="skipped", reason="unknown direction"), 200


def _sync_history(limit=15):
    """Pull the FR24 flight list for REG; store AND report any completed
    flight not yet seen. This is the universal capture path: it covers
    landings at ANY airport (HA events only exist near Blumenau or while the
    aircraft is in the in-memory tracked list) and self-heals missed flights.
    Only flights new to the DB are reported — restarts/backfills never spam."""
    stored = 0
    for entry in fr24.list_flights(REG, limit=limit):
        fid = (entry.get("identification") or {}).get("id")
        arr = ((entry.get("time") or {}).get("real") or {}).get("arrival")
        if not fid or not arr or db.has_flight(fid):
            continue
        try:
            flight = fr24.playback(fid)
            stats = report.compute_stats(flight)
            db.store_flight(flight, stats, list_entry=entry)
            stored += 1
            log.info("history sync: stored flight %s", fid)
            if GROUP_JID and not _already_reported(fid):
                _send_report(fid, flight, stats, GROUP_JID)
        except Exception as exc:  # noqa: BLE001 — one bad flight must not stop the sweep
            log.warning("history sync: %s failed: %s", fid, exc)
    return stored


def _sync_loop():
    while True:
        try:
            _sync_history()
        except Exception as exc:  # noqa: BLE001
            log.warning("history sync sweep failed: %s", exc)
        time.sleep(SYNC_INTERVAL_S)


@app.post("/backfill")
def backfill_endpoint():
    limit = int((request.get_json(silent=True) or {}).get("limit", 15))
    stored = _sync_history(limit)
    return jsonify(stored=stored, total=db.count_flights())


@app.get("/flights")
def flights_endpoint():
    return jsonify(db.list_flights(int(request.args.get("limit", 20))))


@app.get("/charts/<path:name>")
def charts(name):
    return send_from_directory(CHARTS_DIR, name)


@app.get("/health")
def health():
    return jsonify(
        status="ok",
        reg=REG,
        charts=len(glob.glob(os.path.join(CHARTS_DIR, "*.png"))),
        flights=db.count_flights(),
    )


if __name__ == "__main__":
    threading.Thread(target=_sync_loop, daemon=True).start()
    threading.Thread(target=_airborne_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
