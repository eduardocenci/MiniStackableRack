"""Minimal Flightradar24 client — the same unofficial endpoints the HA
flightradar24 integration uses. No API key; identified as a browser."""
import requests

BASE = "https://api.flightradar24.com/common/v1"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
    "Origin": "https://www.flightradar24.com",
    "Referer": "https://www.flightradar24.com/",
}


def list_flights(reg, limit=20):
    """Recent flights of an aircraft registration, newest first."""
    r = requests.get(
        f"{BASE}/flight/list.json",
        params={"query": reg, "fetchBy": "reg", "limit": limit, "page": 1},
        headers=HEADERS,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["result"]["response"]["data"] or []


def latest_landed(reg, max_age_s=6 * 3600, now_ts=None):
    """Newest flight of `reg` with a real arrival within `max_age_s`.

    Returns the flight-list entry dict, or None. `now_ts` is injectable for
    tests; when None the newest arrival is only age-checked if now_ts given.
    """
    import time

    now = now_ts or time.time()
    for f in list_flights(reg):
        arr = ((f.get("time") or {}).get("real") or {}).get("arrival")
        if arr and (now - arr) <= max_age_s:
            return f
    return None


FEED_URL = "https://data-cloud.flightradar24.com/zones/fcgi/feed.js"
FEED_PARAMS = {
    "faa": "1", "satellite": "1", "mlat": "1", "flarm": "1", "adsb": "1",
    "gnd": "1", "air": "1", "vehicles": "0", "estimated": "1",
    "maxage": "14400", "gliders": "0", "stats": "0",
}
# feed row indexes (list): 4 alt_ft · 9 reg · 10 ts · 11 origin_iata ·
# 12 dest_iata · 14 on_ground(0/1) · 16 callsign


def live_reg(reg):
    """Live feed filtered by registration: {flight_id: row}. Empty when the
    aircraft is not transmitting. Tiny response — safe to poll every ~30 s
    (the HA integration polls the same feed every 10 s for its whole area)."""
    r = requests.get(
        FEED_URL, params={**FEED_PARAMS, "reg": reg}, headers=HEADERS, timeout=15
    )
    r.raise_for_status()
    return {k: v for k, v in r.json().items() if isinstance(v, list)}


def list_entry(flight_id, reg):
    """The flight-list entry for one id, or None."""
    for e in list_flights(reg):
        if (e.get("identification") or {}).get("id") == flight_id:
            return e
    return None


def live_details(flight_id):
    """Live flight details (the clickhandler endpoint) — works while the
    flight is in the air; carries the trail flown so far (newest first)."""
    r = requests.get(
        "https://data-live.flightradar24.com/clickhandler/",
        params={"flight": flight_id, "version": "1.5"},
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def latest_airborne(reg):
    """Newest flight of `reg` with a real departure and no arrival yet."""
    for f in list_flights(reg):
        t = (f.get("time") or {}).get("real") or {}
        if t.get("departure") and not t.get("arrival"):
            return f
    return None


def playback(flight_id):
    """Full post-flight playback: identification, airports, aircraft and the
    complete track (lat/lon/altitude/speed/vspeed per timestamp)."""
    r = requests.get(
        f"{BASE}/flight-playback.json",
        params={"flightId": flight_id},
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["result"]["response"]["data"]["flight"]
