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

import cards
import db
import enroute
import fr24
import report
import waha
from report import TZ_LOCAL

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
HOME_IATA = os.environ.get("HOME_IATA", "BNU")
# At home HA announces the take-off (it calls /report with announce=true); the
# live watch only steps in if no announcement claimed the flight by then.
HOME_BACKUP_S = int(os.environ.get("HOME_BACKUP_S", "150"))
# Live watch: the reg-filtered FR24 feed (same feed the HA integration polls
# every 10 s for its area) — IMMEDIATE take-off/landing detection anywhere.
FAST_POLL_S = int(os.environ.get("FAST_POLL_S", "30"))
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


_claim_lock = threading.Lock()


def _claim(key):
    """Atomically mark `key` reported; False when another path already has it
    (HA's announce, the live watch and the list watch race for take-offs)."""
    with _claim_lock:
        if _already_reported(key):
            return False
        _mark_reported(key)
        return True


def _resolve_flight_id(flight_id):
    """Use the id HA passed if FR24 confirms it; otherwise newest landed."""
    if flight_id:
        return flight_id
    entry = fr24.latest_landed(REG, now_ts=time.time())
    return (entry or {}).get("identification", {}).get("id")


def _fill_airports(flight, entry):
    """FR24 playback often lacks the destination (or origin) that the flight
    list already has — e.g. 41f01799 SOD→BNU came back with destination=null,
    so the landing message had no arrival airport. Merge the list's sides in."""
    ap = flight.setdefault("airport", {}) or {}
    flight["airport"] = ap
    eap = (entry or {}).get("airport") or {}
    for side in ("origin", "destination"):
        if not ap.get(side) and eap.get(side):
            ap[side] = eap[side]
    return flight


def _attach_expectation(flight, stats):
    """stats['expected'] = how long this route usually takes, from the flight
    log (wheels-up → wheels-down, this flight excluded) — same direction,
    else the reverse one, like the en-route ETAs. Absent without history."""
    try:
        ap = flight.get("airport") or {}
        o, d = (((ap.get(side) or {}).get("code") or {}).get("icao")
                for side in ("origin", "destination"))
        if not o or not d:
            return
        fid = stats.get("fr24_id")
        durs = db.route_durations(o, d, exclude_fid=fid)
        reverse = not durs
        if reverse:
            durs = db.route_durations(d, o, exclude_fid=fid)
        if durs:
            stats["expected"] = {
                "mean_s": sum(durs) / len(durs), "n": len(durs),
                "fastest_s": min(durs), "reverse": reverse,
            }
    except Exception:  # noqa: BLE001 — a comparison must never block the report
        log.warning("expectation lookup failed", exc_info=True)


def _write_chart(name, png):
    with open(os.path.join(CHARTS_DIR, name), "wb") as fh:
        fh.write(png)
    return f"{SELF_URL}/charts/{name}"


def _send_report(fid, flight, stats, jid):
    """Landing: card 1/2 (route) carrying the full caption, then card 2/2
    (profile, speed, previous flights). If the cards cannot be rendered the
    legacy chart+map image goes out instead — the message is never lost."""
    _attach_expectation(flight, stats)
    caption = report.build_caption(stats)
    try:
        p1, p2 = cards.landing_cards(flight, stats)
    except Exception:  # noqa: BLE001
        log.warning("landing cards failed for %s — legacy chart", fid, exc_info=True)
        p1 = p2 = None
    if p1:
        waha.send_image(jid, _write_chart(f"{fid}-1.png", p1), caption)
        try:
            waha.send_image(jid, _write_chart(f"{fid}-2.png", p2), "")
        except Exception:  # noqa: BLE001 — 1/2 (with the caption) already went out
            log.warning("landing card 2/2 failed for %s", fid, exc_info=True)
    else:
        waha.send_image(jid, _write_chart(f"{fid}.png", report.build_report_image(stats)), caption)
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
            entry = None
            try:
                entry = fr24.list_entry(fid, REG)
            except Exception:  # noqa: BLE001 — the list only enriches
                pass
            _fill_airports(flight, entry)
            stats = report.compute_stats(flight)  # raises while track is short
            try:  # flight log first — a WAHA hiccup must not lose the record
                db.store_flight(flight, stats, list_entry=entry)
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
    waited = False
    attempt = 0
    while attempt < 3:
        attempt += 1
        try:
            try:
                origin, track, dest_hint = enroute.from_clickhandler(fr24.live_details(fid))
                if len(track) < 5:
                    raise ValueError("live trail empty")
            except Exception:  # not live any more (or endpoint hiccup)
                origin, track, dest_hint = enroute.from_playback(fr24.playback(fid))
            if sim:  # test path: pretend we are ENROUTE_DELAY_S into the flight
                track = enroute.truncate_after_takeoff(track, ENROUTE_DELAY_S)
                dest_hint = None  # no hindsight: FR24 rarely knows it at T+10
            elif not force:
                # The scheduled delay comes from the list's "real departure",
                # which FR24 sometimes stamps at transponder-on on the ground
                # (41f01799: 10:20 vs wheels-up 11:22 → update sent at "há
                # 0 min"). The trail's first airborne point is the truth.
                t0 = enroute.takeoff_ts(track)
                wait = (t0 + ENROUTE_DELAY_S - time.time()) if t0 else 0
                if wait > 30 and not waited:
                    waited = True
                    log.info("en-route %s: trail take-off is recent — waiting %ds", fid, wait)
                    time.sleep(wait)
                    attempt -= 1  # the wait is not a failed attempt
                    continue
            st = enroute.analyze(origin, track, dest_hint, exclude_fid=fid)
            caption = enroute.caption(st)
            try:
                png = cards.enroute_card(st, fid)
            except Exception:  # noqa: BLE001
                log.warning("en-route card failed for %s — legacy map", fid, exc_info=True)
                png = enroute.legacy_map(st)
            waha.send_image(jid, _write_chart(f"{fid}-enroute.png", png), caption)
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
        lines.append(f"🕐 Decolagem {datetime.fromtimestamp(dep, TZ_LOCAL).strftime('%H:%M')}")
    if fid:
        lines.append(f"🔗 https://www.flightradar24.com/data/aircraft/ps-vis#{fid}")
    return "\n".join(lines)


def _run_takeoff(fid, jid, fallback_text="", sim=False, entry=None):
    """Take-off card (image + caption) — plain text if anything fails.
    `sim` rebuilds the moment of wheels-up from a completed flight's playback
    (tests, re-sends): trail cut at take-off, no FR24 destination hindsight."""
    for attempt in range(1, 4):
        try:  # retries cover fetch + render only — the send happens once
            fid, png, caption = _build_takeoff(fid, sim, entry)
        except Exception:  # noqa: BLE001
            log.warning("take-off card attempt %d/3 failed for %s", attempt, fid, exc_info=True)
            if attempt < 3 and not sim:
                time.sleep(20)  # FR24 may not serve the trail yet right after wheels-up
            continue
        waha.send_image(jid, _write_chart(f"{fid}-takeoff.png", png), caption)
        log.info("take-off card for %s sent to %s", fid, jid)
        return
    text = fallback_text or (_takeoff_text(entry) if entry else "")
    if text:
        waha.send_text(jid, text)
        log.info("take-off text (fallback) for %s sent to %s", fid, jid)


def _build_takeoff(fid, sim, entry):
    """Fetch + render the take-off card: (fid, png, caption); raises on failure."""
    if not fid:
        fid = ((fr24.latest_airborne(REG) or {}).get("identification") or {}).get("id")
    if not fid:
        raise LookupError("no airborne PS-VIS flight on FR24")
    if entry is None:
        try:
            entry = fr24.list_entry(fid, REG)
        except Exception:  # noqa: BLE001 — the list only enriches
            entry = None
    if sim:
        flight = _fill_airports(fr24.playback(fid), entry)
        origin, track, _ = enroute.from_playback(flight)
        dep = enroute.takeoff_ts(track)
        track = [p for p in track if p["timestamp"] <= dep + 60]
    else:
        origin, track, _ = enroute.from_clickhandler(fr24.live_details(fid))
        dep = (enroute.takeoff_ts(track)
               or (((entry or {}).get("time") or {}).get("real") or {}).get("departure")
               or int(time.time()))
    if origin.get("lat") is None and entry:
        origin = enroute._airport_side(((entry.get("airport") or {}).get("origin")))
    png, caption = cards.takeoff_card(origin, dep, track, fid,
                                      next_ts=dep + ENROUTE_DELAY_S)
    return fid, png, caption


def _home_takeoff_backup(fid, entry, dep):
    """Home take-off: HA announces it through /report (announce=true). If no
    announcement claimed the flight by HOME_BACKUP_S (HA down / event lost),
    the tracker sends the card itself."""
    time.sleep(HOME_BACKUP_S)
    if not GROUP_JID or time.time() - dep > 20 * 60 or not _claim(f"takeoff:{fid}"):
        return
    log.warning("home take-off %s not announced by HA — tracker sends it", fid)
    _run_takeoff(fid, GROUP_JID, _takeoff_text(entry) if entry else "", entry=entry)


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
    if GROUP_JID and o_code.get("icao") != HOME_ICAO and _claim(f"takeoff:{fid}"):
        threading.Thread(target=_run_takeoff, args=(fid, GROUP_JID, _takeoff_text(entry)),
                         kwargs={"entry": entry}, daemon=True).start()
        log.info("remote take-off of %s — card scheduled", fid)
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


# ── Live watch: immediate take-off/landing detection at ANY airport ──────────
# Polls the reg-filtered live feed and reacts to on_ground transitions, the
# same signal the HA integration uses for its Blumenau area. The 5-min
# airborne watch and the 15-min sweep remain as layered backups.

_active_reports = set()
_active_lock = threading.Lock()


def _spawn_once(key, fn):
    """Run fn in a thread unless an identical job is already in flight —
    closes the HA-event vs live-watch race on the same landing."""
    with _active_lock:
        if key in _active_reports:
            return False
        _active_reports.add(key)

    def wrap():
        try:
            fn()
        finally:
            with _active_lock:
                _active_reports.discard(key)

    threading.Thread(target=wrap, daemon=True).start()
    return True


def _on_airborne_first_seen(fid, row, just_took_off=False):
    o_iata = row[11] or ""
    entry = None
    try:
        entry = fr24.list_entry(fid, REG)
    except Exception:  # noqa: BLE001
        pass
    dep = (((entry or {}).get("time") or {}).get("real") or {}).get("departure") \
        or row[10] or int(time.time())
    now = time.time()
    if just_took_off:  # we watched it leave the ground in the feed
        dep = row[10] or int(now)
    elif now - dep > 15 * 60:
        # The list's "real departure" can be transponder-on time on the
        # ground (41f01799: 10:20 vs wheels-up 11:22) — check the live trail.
        try:
            t0 = enroute.takeoff_ts(enroute.from_clickhandler(fr24.live_details(fid))[1])
            if t0:
                dep = t0
        except Exception:  # noqa: BLE001
            pass
    key = f"takeoff:{fid}"
    o_icao = ((((entry or {}).get("airport") or {}).get("origin") or {})
              .get("code") or {}).get("icao") or ""
    if o_icao == HOME_ICAO or o_iata == HOME_IATA:
        # HA announces it (and claims the key via /report announce=true);
        # the backup only fires if nobody did.
        if not _already_reported(key) and now - dep <= 15 * 60:
            log.info("take-off %s at home — HA announces it (backup in %ds)", fid, HOME_BACKUP_S)
            threading.Thread(target=_home_takeoff_backup, args=(fid, entry, dep),
                             daemon=True).start()
    elif _claim(key):
        if now - dep > 15 * 60:
            log.info("first sight of %s is mid-flight — skipping take-off card", fid)
        elif GROUP_JID:
            if entry:
                text = _takeoff_text(entry)
            else:
                city = (db.airport_by_iata(o_iata) or {}).get("city") or o_iata
                text = "\n".join(filter(None, [
                    f"🛫 *PS-VIS decolou de {city}*" if city else "🛫 *PS-VIS decolou*",
                    f"🕐 Decolagem {datetime.fromtimestamp(dep, TZ_LOCAL).strftime('%H:%M')}",
                    f"🔗 https://www.flightradar24.com/data/aircraft/ps-vis#{fid}",
                ]))
            threading.Thread(target=_run_takeoff, args=(fid, GROUP_JID, text),
                             kwargs={"entry": entry}, daemon=True).start()
            log.info("live watch: take-off of %s — card scheduled", fid)
    if GROUP_JID and not _already_reported(f"enroute:{fid}"):
        delay = max(0, dep + ENROUTE_DELAY_S - now)
        threading.Thread(
            target=_run_enroute, args=(fid, GROUP_JID, False, False, delay), daemon=True
        ).start()


def _on_landing_detected(fid, wait_arrival=False):
    if _already_reported(fid) or not GROUP_JID:
        return

    def run():
        if wait_arrival:  # disappeared from the feed — confirm it actually landed
            for _ in range(30):
                try:
                    e = fr24.list_entry(fid, REG)
                    if e and ((e.get("time") or {}).get("real") or {}).get("arrival"):
                        break
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(60)
            else:
                log.info("no FR24 arrival for %s — leaving it to the sweep", fid)
                return
        _run_report(fid, GROUP_JID, False, "")

    if _spawn_once(f"report:{fid}", run):
        log.info("live watch: landing of %s detected (wait_arrival=%s)", fid, wait_arrival)


_live_state = {}


def _live_watch_once():
    rows = fr24.live_reg(REG)
    for fid, row in rows.items():
        airborne = row[14] == 0
        prev = _live_state.get(fid)
        if airborne and (prev is None or not prev["airborne"]):
            _on_airborne_first_seen(fid, row, just_took_off=prev is not None)
        elif not airborne and prev and prev["airborne"]:
            _on_landing_detected(fid)
        _live_state[fid] = {"airborne": airborne}
    for fid in [f for f in _live_state if f not in rows]:
        if _live_state.pop(fid)["airborne"]:
            _on_landing_detected(fid, wait_arrival=True)


def _live_watch_loop():
    while True:
        try:
            _live_watch_once()
        except Exception as exc:  # noqa: BLE001
            log.warning("live watch failed: %s", exc)
        time.sleep(FAST_POLL_S)


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
        if force:  # tests always run, on their own jid
            threading.Thread(
                target=_run_report, args=(flight_id, jid, force, fallback_text), daemon=True
            ).start()
        else:  # dedupe vs the live watch reacting to the same landing
            _spawn_once(
                f"report:{flight_id or 'latest'}",
                lambda: _run_report(flight_id, jid, force, fallback_text),
            )
        return jsonify(status="accepted", flight_id=flight_id, test=test), 202
    if direction == "took_off":
        sim, announce = bool(body.get("sim")), bool(body.get("announce"))
        fallback_text = body.get("fallback_text") or ""

        def run():
            # one thread, in order: the take-off card now, then the T+10 update
            if announce:
                # tests/re-sends (force) always go; live: whoever claims first
                if force or not flight_id or _claim(f"takeoff:{flight_id}"):
                    _run_takeoff(flight_id, jid, fallback_text, sim=sim)
                else:
                    log.info("take-off %s already announced — skipping", flight_id)
            _run_enroute(flight_id, jid, force, sim)

        threading.Thread(target=run, daemon=True).start()
        return jsonify(status="takeoff-accepted" if announce else "enroute-scheduled",
                       flight_id=flight_id, test=test), 202
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
            flight = _fill_airports(fr24.playback(fid), entry)
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
    threading.Thread(target=_live_watch_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
