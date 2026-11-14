"""SQLite flight log — every completed PS-VIS flight (both directions).

Purpose: a growing internal database of registrations, exact routes, times,
speeds and altitudes, so future flights can be compared against history
(slower flight? lower cruise? significant route deviation?).

Layout:
  flights       one row per flight (endpoints + where each came from, times,
                cruise/max stats, FR24's own status for the flight)
  track_points  the exact path, one row per playback point
  claims        one row per message stage already taken for a flight
                (takeoff:/enroute:/report:<id>) — the dedupe, atomic across
                threads and restarts
  messages      every WhatsApp message sent (audit log: what, when, where)
  /data/playbacks/<id>.json.gz  raw playback flight dict — schema insurance
"""
import gzip
import json
import os
import sqlite3
import threading
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
CREATE TABLE IF NOT EXISTS claims (
  key        TEXT PRIMARY KEY,
  claimed_at INTEGER
);
CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  sent_at     INTEGER,
  fr24_id     TEXT,
  kind        TEXT,
  jid         TEXT,
  http_status INTEGER,
  image       TEXT,
  caption     TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_flight ON messages (fr24_id, kind);
"""

# Columns added after the first release — ALTERed into older databases.
# o_src/d_src: where each airport side came from — 'fr24' (playback), 'list'
# (flight list), 'touchdown'/'liftoff' (the aerodrome the track was seen on
# the ground at); NULL = stored before 2026-10-17, unknown.
# fr24_status: the list's status text ('Landed 12:35', 'Unknown' when FR24
# never closed the flight).
_ADDED = {"o_src": "TEXT", "d_src": "TEXT", "fr24_status": "TEXT"}
_migrated = False
_migrate_lock = threading.Lock()


def _conn():
    global _migrated
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    if not _migrated:
        with _migrate_lock:
            if not _migrated:
                c.executescript(SCHEMA)
                have = {r[1] for r in c.execute("PRAGMA table_info(flights)")}
                for col, typ in _ADDED.items():
                    if col not in have:
                        c.execute(f"ALTER TABLE flights ADD COLUMN {col} {typ}")
                c.commit()
                _migrated = True
    return c


def has_flight(fr24_id):
    with _conn() as c:
        return c.execute("SELECT 1 FROM flights WHERE fr24_id=?", (fr24_id,)).fetchone() is not None


def count_flights():
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM flights").fetchone()[0]


def store_flight(flight, stats, list_entry=None):
    """Persist one completed flight (idempotent by fr24_id — an upsert that
    keeps the scheduled times and FR24 status a later call cannot supply).

    `flight` is the playback flight dict (airport sides may carry `_src`, see
    app._fill_airports), `stats` comes from report.compute_stats,
    `list_entry` (optional) is the flight-list entry — the only source of the
    *scheduled* times and of FR24's status text.
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

    def src(name):
        a = ap.get(name)
        return (a.get("_src") or "fr24") if a else None

    sched = ((list_entry or {}).get("time") or {}).get("scheduled") or {}
    status = (((list_entry or {}).get("status") or {}).get("text")) or None

    cols = (
        "fr24_id", "reg", "callsign", "model",
        "o_iata", "o_icao", "o_name", "o_city", "o_lat", "o_lon",
        "d_iata", "d_icao", "d_name", "d_city", "d_lat", "d_lon",
        "sched_dep", "sched_arr", "dep_ts", "arr_ts", "duration_s",
        "cruise_alt_ft", "cruise_kt", "cruise_kmh", "max_alt_ft", "max_kt",
        "dist_km", "route_km", "n_points", "stored_at",
        "o_src", "d_src", "fr24_status",
    )
    row = (
        fid, stats["reg"], ident.get("callsign"), stats["model"],
        *side("origin"), *side("destination"),
        sched.get("departure"), sched.get("arrival"),
        stats["dep_ts"], stats["arr_ts"], stats["duration_s"],
        stats["cruise_alt_ft"], round(stats["cruise_kt"], 1), round(stats["cruise_kmh"], 1),
        stats["max_alt_ft"], stats["max_kt"],
        round(stats["dist_km"], 1), round(stats["route_km"], 1),
        len(stats["track"]), int(time.time()),
        src("origin"), src("destination"), status,
    )
    keep = ("sched_dep", "sched_arr", "fr24_status")  # only the list knows these
    sets = ", ".join(
        f"{c}=COALESCE(excluded.{c}, flights.{c})" if c in keep else f"{c}=excluded.{c}"
        for c in cols[1:]
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
            f"INSERT INTO flights ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
            f" ON CONFLICT(fr24_id) DO UPDATE SET {sets}", row
        )
        c.executemany("INSERT INTO track_points VALUES (?,?,?,?,?,?,?,?,?)", points)

    os.makedirs(RAW_DIR, exist_ok=True)
    with gzip.open(os.path.join(RAW_DIR, f"{fid}.json.gz"), "wt", encoding="utf-8") as fh:
        json.dump(flight, fh)


# ── dedupe claims + message log ──────────────────────────────────────────────

def claim(key):
    """Take `key` (e.g. 'enroute:<id>'): True for exactly one caller, ever —
    the primary key makes the check-and-mark a single atomic statement, so
    threads waking in the same second cannot both pass (they did with the old
    read-then-append text file: every T+10 card went out twice 12–16/10)."""
    with _conn() as c:
        cur = c.execute("INSERT OR IGNORE INTO claims (key, claimed_at) VALUES (?, ?)",
                        (key, int(time.time())))
        return cur.rowcount == 1


def is_claimed(key):
    with _conn() as c:
        return c.execute("SELECT 1 FROM claims WHERE key=?", (key,)).fetchone() is not None


def release(key):
    """Give a claim back (the send failed before anything reached the group)."""
    with _conn() as c:
        c.execute("DELETE FROM claims WHERE key=?", (key,))


def import_claims(keys):
    """Seed claims from the pre-2026-10-17 /data/last_reported file."""
    now = int(time.time())
    with _conn() as c:
        c.executemany("INSERT OR IGNORE INTO claims (key, claimed_at) VALUES (?, ?)",
                      [(k, now) for k in keys])


def log_message(fr24_id, kind, jid, http_status, image, caption):
    with _conn() as c:
        c.execute(
            "INSERT INTO messages (sent_at, fr24_id, kind, jid, http_status, image, caption)"
            " VALUES (?,?,?,?,?,?,?)",
            (int(time.time()), fr24_id, kind, jid, http_status, image, caption),
        )


def messages(fr24_id=None, limit=50):
    with _conn() as c:
        c.row_factory = sqlite3.Row
        if fr24_id:
            rows = c.execute("SELECT * FROM messages WHERE fr24_id=? ORDER BY id DESC LIMIT ?",
                             (fr24_id, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def known_airports(exclude_icao=None):
    """Distinct airports seen in the log (either endpoint of any flight) with
    coordinates — the candidate destinations for the en-route estimate."""
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT d_iata AS iata, d_icao AS icao, d_name AS name,"
            "       d_city AS city, d_lat AS lat, d_lon AS lon FROM flights"
            " UNION "
            "SELECT o_iata, o_icao, o_name, o_city, o_lat, o_lon FROM flights"
        ).fetchall()
    out = {}
    for r in rows:
        if r["icao"] and r["lat"] is not None and r["icao"] != exclude_icao:
            out.setdefault(r["icao"], dict(r))
    return list(out.values())


def airport_by_iata(iata):
    """Gazetteer lookup from the flight log (city names for feed IATA codes)."""
    if not iata:
        return None
    for a in known_airports():
        if a.get("iata") == iata:
            return a
    return None


def route_durations(o_icao, d_icao, exclude_fid=None):
    """Durations (s) of logged flights on a route, newest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT duration_s FROM flights"
            " WHERE o_icao=? AND d_icao=? AND duration_s > 0 AND fr24_id != ?"
            " ORDER BY dep_ts DESC LIMIT 10",
            (o_icao, d_icao, exclude_fid or ""),
        ).fetchall()
    return [r[0] for r in rows]


def list_flights(limit=20):
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT fr24_id, reg, model, o_iata, o_icao, d_iata, d_icao, o_src, d_src,"
            " dep_ts, arr_ts, duration_s, cruise_alt_ft, cruise_kt,"
            " max_alt_ft, max_kt, dist_km, route_km, n_points, fr24_status"
            " FROM flights ORDER BY dep_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
