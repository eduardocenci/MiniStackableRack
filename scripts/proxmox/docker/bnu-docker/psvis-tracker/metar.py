"""Live METAR via aviationweather.gov — no API key.

Small Brazilian aerodromes (e.g. SSBL Blumenau) publish no METAR; `nearest()`
falls back to the closest reporting station via a bbox query (SBNF Navegantes,
~40 km, in Blumenau's case).
"""
import math

import requests

BASE = "https://aviationweather.gov/api/data/metar"
HEADERS = {"User-Agent": "psvis-tracker/1.0 (+MiniStackableRack home automation)"}

WX_PT = [
    ("TS", "trovoada"), ("GR", "granizo"), ("FG", "nevoeiro"),
    ("SH", "pancadas de chuva"), ("RA", "chuva"), ("DZ", "garoa"),
    ("BR", "névoa"), ("HZ", "bruma"), ("FU", "fumaça"),
]


def _get(params):
    r = requests.get(BASE, params={**params, "format": "json"}, headers=HEADERS, timeout=20)
    r.raise_for_status()
    # a query matching no reporting station answers 204 with an empty body
    return r.json() if r.status_code != 204 and r.text.strip() else []


def by_ids(icaos):
    """{icao: obs} for the stations that actually report."""
    if not icaos:
        return {}
    return {m.get("icaoId"): m for m in _get({"ids": ",".join(icaos)})}


def nearest(lat, lon, box_deg=1.5):
    """Closest reporting station to a point, or None. Returns (obs, dist_km)."""
    obs = _get({"bbox": f"{lat - box_deg},{lon - box_deg},{lat + box_deg},{lon + box_deg}"})
    best, best_d = None, None
    for m in obs:
        mlat, mlon = m.get("lat"), m.get("lon")
        if mlat is None or mlon is None:
            continue
        d = math.dist((mlat, mlon), (lat, lon)) * 111  # good enough at this scale
        if best_d is None or d < best_d:
            best, best_d = m, d
    return (best, best_d) if best else None


def _fmt_int_br(n):
    return f"{int(round(n)):,}".replace(",", ".")


def summarize(m, brief=False):
    """Compact pt-BR one-liner: '🌫️ LIFR — chuva fraca, nevoeiro, teto 100 ft,
    vis 2 km, 14°C'. `brief` keeps only category + phenomena (the en-route
    variant)."""
    wx_raw = m.get("wxString") or ""
    wx = []
    for code, pt in WX_PT:
        if code in wx_raw and pt not in wx:
            qual = "fraca " if f"-{code}" in wx_raw or wx_raw.startswith("-") else (
                "forte " if f"+{code}" in wx_raw or wx_raw.startswith("+") else "")
            wx.append((qual + pt).strip() if pt == "chuva" else pt)

    clouds = m.get("clouds") or []
    ceiling = min(
        (c.get("base") for c in clouds if c.get("cover") in ("BKN", "OVC") and c.get("base")),
        default=None,
    )
    covers = {c.get("cover") for c in clouds}

    if "TS" in wx_raw:
        emoji = "⛈️"
    elif "FG" in wx_raw:
        emoji = "🌫️"
    elif any(x in wx_raw for x in ("RA", "DZ", "SH")):
        emoji = "🌧️"
    elif "BR" in wx_raw or "HZ" in wx_raw:
        emoji = "🌫️"
    elif covers & {"BKN", "OVC"}:
        emoji = "☁️"
    elif covers & {"SCT", "FEW"}:
        emoji = "⛅"
    else:
        emoji = "☀️"

    parts = list(wx)
    if not brief:
        if ceiling is not None:
            parts.append(f"teto {_fmt_int_br(ceiling)} ft")
        vis = m.get("visib")
        if vis is not None and not str(vis).endswith("+"):
            try:
                parts.append(f"vis {round(float(vis) * 1.609, 1):g} km")
            except ValueError:
                pass
        wspd = m.get("wspd") or 0
        if wspd >= 12:
            gust = m.get("wgst")
            parts.append(f"vento {wspd} kt" + (f" raj {gust}" if gust else ""))
        if m.get("temp") is not None:
            parts.append(f"{round(m['temp'])}°C")

    cat = m.get("fltCat") or ""
    head = f"{emoji} {cat}".strip()
    return f"{head} — {', '.join(parts)}" if parts else head
