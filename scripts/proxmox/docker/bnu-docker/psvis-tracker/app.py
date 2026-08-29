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

from flask import Flask, jsonify, request, send_from_directory

import db
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


@app.post("/report")
def report_endpoint():
    body = request.get_json(silent=True) or {}
    direction = body.get("direction", "landed")
    if direction != "landed":
        return jsonify(status="skipped", reason="report only on landing"), 200
    test = bool(body.get("test"))
    jid = body.get("chat_jid") or (TEST_GROUP_JID if test else GROUP_JID)
    if not jid:
        return jsonify(status="error", reason="no chat JID configured"), 500
    flight_id = (body.get("flight_id") or "").strip() or None
    force = bool(body.get("force") or test)
    fallback_text = body.get("fallback_text") or ""
    threading.Thread(
        target=_run_report, args=(flight_id, jid, force, fallback_text), daemon=True
    ).start()
    return jsonify(status="accepted", flight_id=flight_id, test=test), 202


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
    app.run(host="0.0.0.0", port=8000)
