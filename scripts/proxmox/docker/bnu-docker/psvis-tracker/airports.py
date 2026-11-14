"""Aerodrome gazetteer — names the airport PS-VIS was actually seen on the
ground at when FR24 does not say (pedido Eduardo 2026-10-16).

42226758 (15/10/2026) NVT→?: FR24 never closed the flight (status
"Unknown", no destination), yet its track rolls out ~1 km from SBPA's runway
and taxis to the apron — the group got "pousou em ?". Only GROUND points
count: the landing roll and taxi after the last airborne point (or the
take-off roll before the first one). A track that ends in the air stays
"destino desconhecido" — never guessed from the last position (decisão
Eduardo 2026-10-02 still holds for that case).

Source: OurAirports airports.csv (public domain; every ANAC aerodrome,
private strips included), fetched into /data/airports.csv (aerodromes only,
just the columns used here) and refreshed every 30 days; a failed refresh
keeps the old copy, and with no copy at all the flight log's own airports are
the gazetteer. A match the flight log already knows takes the log's
FR24-style name/city, so one airport reads the same in every message.
"""
import csv
import io
import logging
import os
import threading
import time

import requests

import db
from report import _haversine_km

log = logging.getLogger("psvis.airports")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
PATH = os.path.join(DATA_DIR, "airports.csv")
URL = os.environ.get("AIRPORTS_URL",
                     "https://davidmegginson.github.io/ourairports-data/airports.csv")
MAX_AGE_S = 30 * 86400
# Ground points sit on the runway/apron; an aerodrome reference point can be
# ~2 km from a long runway's end (SBPA's touchdown was 1.2 km from it).
MATCH_KM = float(os.environ.get("AIRPORT_MATCH_KM", "4"))
TYPES = ("large_airport", "medium_airport", "small_airport")
FIELDS = ("ident", "type", "name", "latitude_deg", "longitude_deg",
          "municipality", "iso_country", "icao_code", "gps_code", "iata_code")

_lock = threading.Lock()


def _fetch():
    r = requests.get(URL, timeout=90)
    r.raise_for_status()
    n, tmp = 0, PATH + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for row in csv.DictReader(io.StringIO(r.content.decode("utf-8"))):
            if row.get("type") in TYPES:
                w.writerow([row.get(f, "") for f in FIELDS])
                n += 1
    if n < 1000:
        raise ValueError(f"only {n} aerodromes in the download")
    os.replace(tmp, PATH)
    log.info("aerodrome gazetteer refreshed: %d aerodromes", n)


def _refresh():
    """Make sure the cached file exists and is under MAX_AGE_S old. Never
    raises — a stale copy (or, with none, the flight log) still works."""
    with _lock:
        try:
            age = time.time() - os.path.getmtime(PATH)
        except OSError:
            age = None
        if age is None or age > MAX_AGE_S:
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                _fetch()
            except Exception as exc:  # noqa: BLE001
                log.warning("aerodrome gazetteer refresh failed: %s", exc)


def _gazetteer(box=None):
    """(lat, lon, row) per aerodrome, streamed from the cached file (~4 MB,
    read only when FR24 lacks an airport — nothing kept in memory); `box` =
    (lat_lo, lat_hi, lon_lo, lon_hi) filters while reading."""
    _refresh()
    if not os.path.exists(PATH):
        return
    with open(PATH, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                lat, lon = float(row["latitude_deg"]), float(row["longitude_deg"])
            except (TypeError, ValueError):
                continue
            if box is None or (box[0] <= lat <= box[1] and box[2] <= lon <= box[3]):
                yield lat, lon, row


def warm():
    """Fetch the gazetteer ahead of the first landing (startup thread)."""
    log.info("aerodrome gazetteer ready: %d aerodromes", sum(1 for _ in _gazetteer()))


def _side(name, icao, iata, city, lat, lon):
    """An airport in FR24's playback shape — what every consumer reads."""
    return {"name": name or None, "code": {"iata": iata or None, "icao": icao or None},
            "position": {"latitude": lat, "longitude": lon, "region": {"city": city or None}}}


def _box(pts, pad=0.1):
    """Bounding box of the points plus ~11 km — the cheap pre-filter."""
    lats, lons = [a for a, _ in pts], [b for _, b in pts]
    return min(lats) - pad, max(lats) + pad, min(lons) - pad, max(lons) + pad


def _nearest(cands, pts, max_km):
    """(km, cand) nearest to any point, within max_km — cands are (lat, lon, x)."""
    lo_a, hi_a, lo_o, hi_o = _box(pts)
    best = None
    for lat, lon, x in cands:
        if lo_a <= lat <= hi_a and lo_o <= lon <= hi_o:
            d = min(_haversine_km(a, b, lat, lon) for a, b in pts)
            if d <= max_km and (best is None or d < best[0]):
                best = (d, x)
    return best


def at_ground(points, max_km=MATCH_KM):
    """The aerodrome under a run of ground track points (landing roll/taxi or
    take-off roll), as (airport side dict, km, source) — None when no
    aerodrome lies within max_km of them."""
    pts = [(p["latitude"], p["longitude"]) for p in points[:60]
           if p.get("latitude") is not None and p.get("longitude") is not None]
    if not pts:
        return None
    known = {a["icao"]: a for a in db.known_airports()}
    hit = _nearest(_gazetteer(_box(pts)), pts, max_km)
    if hit:
        km, row = hit
        icao = row.get("icao_code") or row.get("gps_code") or row.get("ident")
        a = known.get(icao)
        if a:  # already in the log: keep FR24's naming
            return _side(a["name"], a["icao"], a["iata"], a["city"], a["lat"], a["lon"]), km, "log"
        return (_side(row.get("name"), icao, row.get("iata_code"), row.get("municipality"),
                      float(row["latitude_deg"]), float(row["longitude_deg"])), km, "ourairports")
    hit = _nearest([(a["lat"], a["lon"], a) for a in known.values()], pts, max_km)
    if hit:
        km, a = hit
        return _side(a["name"], a["icao"], a["iata"], a["city"], a["lat"], a["lon"]), km, "log"
    return None
