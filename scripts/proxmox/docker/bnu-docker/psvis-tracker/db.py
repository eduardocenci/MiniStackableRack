"""SQLite flight log — every completed PS-VIS flight (both directions).

Purpose: a growing internal database of registrations, exact routes, times,
speeds and altitudes, so future flights can be compared against history
(slower flight? lower cruise? significant route deviation?).

Layout:
  flights       one row per flight (endpoints, times, cruise/max stats)
  track_points  the exact path, one row per playback point
  /data/playbacks/<id>.json.gz  raw playback flight dict — schema insurance
"""
import gzip
import json
import os
import sqlite3
import time

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "flights.db")
RAW_DIR = os.path.join(DATA_DIR, "playbacks")

SCHEMA = """
CREATE TABLE IF NOT EXISTS flights (
  fr24_id       TEXT PRIMARY KEY,
  reg           TEXT,
  callsign      TEXT,
  model         TEXT,
  o_iata TEXT, o_icao TEXT, o_name TEXT, o_city TEXT, o_lat REAL, o_lon REAL,
  d_iata TEXT, d_icao TEXT, d_name TEXT, d_city TEXT, d_lat REAL, d_lon REAL,
  sched_dep INTEGER, sched_arr INTEGER,
  dep_ts INTEGER, arr_ts INTEGER, duration_s INTEGER,
  cruise_alt_ft INTEGER, cruise_kt REAL, cruise_kmh REAL,
  max_alt_ft INTEGER, max_kt REAL,
  dist_km REAL, route_km REAL,
  n_points INTEGER,
  stored_at INTEGER
);
CREATE TABLE IF NOT EXISTS track_points (
  fr24_id  TEXT,
  seq      INTEGER,
  ts       INTEGER,
  lat REAL, lon REAL,
  alt_ft INTEGER, spd_kt INTEGER, vspd_fpm INTEGER, heading INTEGER,
  PRIMARY KEY (fr24_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_flights_route ON flights (o_icao, d_icao, dep_ts);
"""


def _conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.executescript(SCHEMA)
    return c


def has_flight(fr24_id):
    with _conn() as c:
        return c.execute("SELECT 1 FROM flights WHERE fr24_id=?", (fr24_id,)).fetchone() is not None


def count_flights():
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM flights").fetchone()[0]


def store_flight(flight, stats, list_entry=None):
    """Persist one completed flight (idempotent by fr24_id).

    `flight` is the playback flight dict, `stats` comes from
    report.compute_stats, `list_entry` (optional) is the flight-list entry —
    the only source of the *scheduled* times.
    """
    ident = flight.get("identification") or {}
    fid = ident.get("id") or stats.get("fr24_id")
    if not fid:
        raise ValueError("flight has no FR24 id")

    ap = flight.get("airport") or {}

    def side(name):
        a = ap.get(name) or {}
        code = a.get("code") or {}
        pos = a.get("position") or {}
        return (
            code.get("iata"), code.get("icao"), a.get("name"),
            ((pos.get("region") or {}).get("city")),
            pos.get("latitude"), pos.get("longitude"),
        )

    sched = ((list_entry or {}).get("time") or {}).get("scheduled") or {}

    row = (
        fid, stats["reg"], ident.get("callsign"), stats["model"],
        *side("origin"), *side("destination"),
        sched.get("departure"), sched.get("arrival"),
        stats["dep_ts"], stats["arr_ts"], stats["duration_s"],
        stats["cruise_alt_ft"], round(stats["cruise_kt"], 1), round(stats["cruise_kmh"], 1),
        stats["max_alt_ft"], stats["max_kt"],
        round(stats["dist_km"], 1), round(stats["route_km"], 1),
        len(stats["track"]), int(time.time()),
    )
    points = [
        (
            fid, i, p.get("timestamp"),
            p.get("latitude"), p.get("longitude"),
            (p.get("altitude") or {}).get("feet"),
            (p.get("speed") or {}).get("kts"),
            (p.get("verticalSpeed") or {}).get("fpm"),
            p.get("heading"),
        )
        for i, p in enumerate(stats["track"])
    ]
    with _conn() as c:
        c.execute("DELETE FROM track_points WHERE fr24_id=?", (fid,))
        c.execute(
            f"INSERT OR REPLACE INTO flights VALUES ({','.join('?' * len(row))})", row
        )
        c.executemany("INSERT INTO track_points VALUES (?,?,?,?,?,?,?,?,?)", points)

    os.makedirs(RAW_DIR, exist_ok=True)
    with gzip.open(os.path.join(RAW_DIR, f"{fid}.json.gz"), "wt", encoding="utf-8") as fh:
        json.dump(flight, fh)


def list_flights(limit=20):
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT fr24_id, reg, model, o_iata, o_icao, d_iata, d_icao,"
            " dep_ts, arr_ts, duration_s, cruise_alt_ft, cruise_kt,"
            " max_alt_ft, max_kt, dist_km, route_km, n_points"
            " FROM flights ORDER BY dep_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
