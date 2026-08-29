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


def _run_report(flight_id, jid, force):
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
            png = report.build_report_image(stats)  # profile chart + route map
            with open(os.path.join(CHARTS_DIR, f"{fid}.png"), "wb") as fh:
                fh.write(png)
            caption = report.build_caption(stats)
            waha.send_image(jid, f"{SELF_URL}/charts/{fid}.png", caption)
            _mark_reported(fid)
            log.info("report for %s sent to %s", fid, jid)
            return
        except Exception as exc:  # noqa: BLE001 — retry on anything, FR24 lags
            log.warning("attempt %d/%d failed: %s", attempt, MAX_TRIES, exc)
            if attempt < MAX_TRIES:
                time.sleep(RETRY_S)
    log.error("giving up on flight report (flight_id=%s)", flight_id)


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
    threading.Thread(target=_run_report, args=(flight_id, jid, force), daemon=True).start()
    return jsonify(status="accepted", flight_id=flight_id, test=test), 202


@app.get("/charts/<path:name>")
def charts(name):
    return send_from_directory(CHARTS_DIR, name)


@app.get("/health")
def health():
    return jsonify(status="ok", reg=REG, charts=len(glob.glob(os.path.join(CHARTS_DIR, "*.png"))))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
