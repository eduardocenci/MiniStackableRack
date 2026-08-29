"""T+10 en-route update after a take-off.

FR24 rarely knows the destination at departure, so this publishes what CAN be
known ten minutes in: the cardinal heading, the route flown so far (map), and
arrival estimates for previously-seen destinations that lie along the heading
— cross-referenced against the flight log (db.py).
"""
import logging
import math
from datetime import datetime

import db
import maptile
import metar
from report import TZ_LOCAL, _fmt_int_br

log = logging.getLogger("psvis.enroute")

CARDINALS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]
HEADING_TOLERANCE_DEG = 45


def _bearing(lat1, lon1, lat2, lon2):
    r = math.pi / 180
    dlon = (lon2 - lon1) * r
    y = math.sin(dlon) * math.cos(lat2 * r)
    x = math.cos(lat1 * r) * math.sin(lat2 * r) - math.sin(lat1 * r) * math.cos(lat2 * r) * math.cos(dlon)
    return math.degrees(math.atan2(y, x)) % 360


def _cardinal(brg):
    return CARDINALS[round(brg / 22.5) % 16]


def _angdiff(a, b):
    return abs((a - b + 180) % 360 - 180)


def _norm_trail(trail):
    """clickhandler trail ({lat,lng,alt,spd,ts,hd}, newest first) -> the
    playback-style track list the rest of the code understands."""
    pts = sorted(trail, key=lambda p: p.get("ts") or 0)
    return [
        {
            "latitude": p["lat"],
            "longitude": p["lng"],
            "timestamp": p.get("ts"),
            "heading": p.get("hd"),
            "altitude": {"feet": p.get("alt") or 0},
            "speed": {"kts": p.get("spd") or 0, "kmh": round((p.get("spd") or 0) * 1.852)},
        }
        for p in pts
    ]


def _airport_side(airport_dict):
    a = airport_dict or {}
    code = a.get("code") or {}
    pos = a.get("position") or {}
    return {
        "iata": code.get("iata"),
        "icao": code.get("icao"),
        "name": a.get("name"),
        "city": ((pos.get("region") or {}).get("city")),
        "lat": pos.get("latitude"),
        "lon": pos.get("longitude"),
    }


def from_clickhandler(raw):
    """(origin, track, dest_hint) from a live_details response."""
    ap = raw.get("airport") or {}
    dest = _airport_side(ap.get("destination")) if ap.get("destination") else None
    return _airport_side(ap.get("origin")), _norm_trail(raw.get("trail") or []), dest


def from_playback(flight):
    """(origin, track, dest_hint) from a playback flight dict (fallback and
    the simulation path used in tests)."""
    ap = flight.get("airport") or {}
    dest = _airport_side(ap.get("destination")) if ap.get("destination") else None
    return _airport_side(ap.get("origin")), flight.get("track") or [], dest


def truncate_after_takeoff(track, seconds):
    """First `seconds` of flight (plus a minute of ground context) — used to
    simulate the T+10 moment from a completed flight's full track."""
    alt = [(p.get("altitude") or {}).get("feet") or 0 for p in track]
    airborne = [i for i, a in enumerate(alt) if a > 0]
    if not airborne:
        return track
    t0 = track[airborne[0]]["timestamp"]
    return [p for p in track if p["timestamp"] <= t0 + seconds and p["timestamp"] >= t0 - 60]


def _codes(c):
    ia, ic = c.get("iata"), c.get("icao")
    return f"{ia}/{ic}" if ia and ic and ia != ic else (ia or ic or "")


def _label(c):
    nm = c.get("name") or ""
    city = c.get("city") or ""
    nm = nm.replace(" Airport", "").replace("Airport", "").strip()
    if city and nm.startswith(city):
        nm = nm[len(city):].strip(" -–")
    nm = nm or city or _codes(c)
    codes = _codes(c)
    return f"{nm} ({codes})" if codes and codes not in nm else nm


def _weather_lines(cur, cands):
    """METAR breakdown, one line per candidate destination: conditions at the
    destination (nearest reporting station when the aerodrome publishes no
    METAR) and, briefly, en route (station nearest the remaining-route
    midpoint). Fully best-effort — any failure just drops the block."""
    if not cands:
        return []
    try:
        reported = metar.by_ids([c["icao"] for c in cands if c.get("icao")])
        lines = ["🌦️ Meteo agora (METAR):"]
        for c in cands:
            obs, station = reported.get(c["icao"]), None
            if not obs:
                near = metar.nearest(c["lat"], c["lon"])
                if near:
                    obs, station = near[0], near[0].get("icaoId")
            if not obs:
                continue
            dest_txt = metar.summarize(obs)
            if station and station != c["icao"]:
                dest_txt += f" ({station})"
            line = f"• {c.get('city') or _codes(c)}: {dest_txt}"
            mid_lat = (cur["latitude"] + c["lat"]) / 2
            mid_lon = (cur["longitude"] + c["lon"]) / 2
            near_mid = metar.nearest(mid_lat, mid_lon)
            if near_mid and near_mid[0].get("icaoId") not in (obs.get("icaoId"),):
                line += f" · em rota: {metar.summarize(near_mid[0], brief=True)}"
            lines.append(line)
        return lines if len(lines) > 1 else []
    except Exception as exc:  # noqa: BLE001 — weather must never break the update
        log.warning("METAR block failed: %s", exc)
        return []


def build(origin, track, dest_hint=None, exclude_fid=None):
    """Returns (caption, map_png_bytes) for the en-route update."""
    if len(track) < 5:
        raise ValueError(f"trail too short ({len(track)} points)")

    alt = [(p.get("altitude") or {}).get("feet") or 0 for p in track]
    airborne = [i for i, a in enumerate(alt) if a > 0]
    if not airborne:
        raise ValueError("not airborne yet")
    dep_ts = track[airborne[0]]["timestamp"]
    cur = track[-1]
    now_ts = cur["timestamp"]
    cur_alt = (cur.get("altitude") or {}).get("feet") or 0
    cur_kt = (cur.get("speed") or {}).get("kts") or 0

    # circular mean of the last few reported headings = current course
    heads = [p.get("heading") for p in track[-5:] if p.get("heading") is not None]
    if heads:
        r = math.pi / 180
        heading = math.degrees(math.atan2(
            sum(math.sin(h * r) for h in heads), sum(math.cos(h * r) for h in heads)
        )) % 360
    else:  # derive from the last two positions
        p1, p2 = track[-2], track[-1]
        heading = _bearing(p1["latitude"], p1["longitude"], p2["latitude"], p2["longitude"])

    # ── candidates: previously-seen airports along the heading, estimated
    #    ONLY from route history — the range spans the fastest to the slowest
    #    previous flight on that route (same direction, else the reverse one).
    #    Ranked by alignment AND route frequency, equally weighted; no cap.
    cands = []
    for cand in db.known_airports(exclude_icao=origin.get("icao")):
        brg = _bearing(cur["latitude"], cur["longitude"], cand["lat"], cand["lon"])
        diff = _angdiff(heading, brg)
        if diff > HEADING_TOLERANCE_DEG:
            continue
        same = db.route_durations(origin.get("icao"), cand["icao"], exclude_fid=exclude_fid)
        rev = db.route_durations(cand["icao"], origin.get("icao"), exclude_fid=exclude_fid)
        durs = same or rev
        if not durs:
            continue
        lo = datetime.fromtimestamp(dep_ts + min(durs), TZ_LOCAL).strftime("%H:%M")
        hi = datetime.fromtimestamp(dep_ts + max(durs), TZ_LOCAL).strftime("%H:%M")
        cands.append({
            **cand,
            "diff": diff,
            "freq": len(same) + len(rev),
            "eta_txt": f"*~{lo}*" if lo == hi else f"*~{lo}–{hi}*",
        })
    if cands:
        max_freq = max(c["freq"] for c in cands)
        for c in cands:
            c["score"] = 0.5 * (1 - c["diff"] / HEADING_TOLERANCE_DEG) + 0.5 * (c["freq"] / max_freq)
        cands.sort(key=lambda c: -c["score"])
    cand_lines = [f"• {_label(c)}: chegada {c['eta_txt']}" for c in cands]

    dep_hm = datetime.fromtimestamp(dep_ts, TZ_LOCAL).strftime("%H:%M")
    mins = int(round((now_ts - dep_ts) / 60))
    o_city = origin.get("city") or _codes(origin)

    caption = [f"🧭 *PS-VIS em voo* — rumo {_cardinal(heading)} ({round(heading)}°)"]
    caption.append(f"🛫 Decolagem de {o_city} às {dep_hm} · há {mins} min")
    if dest_hint and (dest_hint.get("city") or _codes(dest_hint)):
        caption.append(f"✈️ Destino (FR24): {_label(dest_hint)}")
    caption.append(f"📍 {_fmt_int_br(cur_alt)} ft · {_fmt_int_br(cur_kt)} kt")
    if cand_lines:
        caption.append("🎯 Estimativas (histórico de destinos no rumo):")
        caption.extend(cand_lines)
    else:
        caption.append("🎯 Nenhum destino com histórico no rumo — rota nova")

    caption.extend(_weather_lines(cur, cands))

    img = maptile.render_path(track, width=1200, height=560, plane_heading=heading)
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "\n".join(caption), buf.getvalue()
