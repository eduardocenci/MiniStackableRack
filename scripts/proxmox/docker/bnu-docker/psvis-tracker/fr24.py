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
