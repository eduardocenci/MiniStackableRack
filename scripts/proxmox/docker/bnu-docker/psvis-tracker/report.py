"""Turn an FR24 playback into flight statistics, a WhatsApp caption and an
altitude/speed profile chart (PNG).

Chart follows the dataviz conventions: two stacked single-series panels (never a
dual-axis), validated palette slots 1/2, recessive grid, text in ink tokens.
"""
import io
import logging
import math
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

# dataviz reference palette (light mode)
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e7e6e2"
BLUE = "#2a78d6"  # categorical slot 1 — altitude
ORANGE = "#eb6834"  # categorical slot 2 — speed

try:
    from zoneinfo import ZoneInfo

    TZ_LOCAL = ZoneInfo("America/Sao_Paulo")
except Exception:  # no tzdata — Brazil currently has no DST
    TZ_LOCAL = timezone(timedelta(hours=-3))


def _haversine_km(lat1, lon1, lat2, lon2):
    r = math.pi / 180
    dlat = (lat2 - lat1) * r
    dlon = (lon2 - lon1) * r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _fmt_hm(seconds):
    m = int(round(seconds / 60))
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def _fmt_int_br(n):
    return f"{int(round(n)):,}".replace(",", ".")


def compute_stats(flight):
    """Stats dict from a playback flight dict (needs a non-trivial track)."""
    track = flight.get("track") or []
    if len(track) < 20:
        raise ValueError(f"track too short ({len(track)} points)")

    ts = [p["timestamp"] for p in track]
    alt = [(p.get("altitude") or {}).get("feet") or 0 for p in track]
    kmh = [(p.get("speed") or {}).get("kmh") or 0 for p in track]
    kts = [(p.get("speed") or {}).get("kts") or 0 for p in track]

    airborne = [i for i, a in enumerate(alt) if a > 0]
    i0, i1 = (airborne[0], airborne[-1]) if airborne else (0, len(track) - 1)
    dep_ts, arr_ts = ts[max(i0 - 1, 0)], ts[min(i1 + 1, len(ts) - 1)]

    # trim ground taxi off both ends (keep ~1 min of context) so the chart's
    # time axis spans the flight, not the parking position
    j0 = max(i0 - 1, 0)
    while j0 > 0 and dep_ts - ts[j0 - 1] <= 60:
        j0 -= 1
    j1 = min(i1 + 1, len(ts) - 1)
    while j1 < len(ts) - 1 and ts[j1 + 1] - arr_ts <= 60:
        j1 += 1
    track = track[j0:j1 + 1]
    ts, alt = ts[j0:j1 + 1], alt[j0:j1 + 1]
    kmh, kts = kmh[j0:j1 + 1], kts[j0:j1 + 1]

    max_alt = max(alt)
    cruise_idx = [i for i, a in enumerate(alt) if a >= 0.95 * max_alt]
    # Cruise = the LONGEST CONTIGUOUS stretch at cruise altitude; cruise speed
    # is the MEAN over that stretch (the stabilized period), not the whole flight.
    runs, start = [], cruise_idx[0]
    for prev, cur in zip(cruise_idx, cruise_idx[1:] + [None]):
        if cur is None or cur != prev + 1:
            runs.append((start, prev))
            start = cur
    c0, c1 = max(runs, key=lambda r: r[1] - r[0])
    cruise_alt = sorted(alt[c0:c1 + 1])[(c1 - c0) // 2]
    cruise_kt = sum(kts[c0:c1 + 1]) / (c1 - c0 + 1)
    cruise_kmh = sum(kmh[c0:c1 + 1]) / (c1 - c0 + 1)

    dist_km = sum(
        _haversine_km(
            track[i]["latitude"], track[i]["longitude"],
            track[i + 1]["latitude"], track[i + 1]["longitude"],
        )
        for i in range(len(track) - 1)
    )

    ap = flight.get("airport") or {}

    def code(side):
        c = ((ap.get(side) or {}).get("code") or {})
        return c.get("iata") or c.get("icao") or "?"

    def friendly(side):
        # 'Sao Paulo Campo de Marte Airport' + city 'Sao Paulo' -> 'Campo de Marte'
        a = ap.get(side) or {}
        name = (a.get("name") or "").replace(" Airport", "").replace("Airport", "").strip()
        city = ((a.get("position") or {}).get("region") or {}).get("city") or ""
        if city and name.startswith(city):
            name = name[len(city):].strip(" -–")
        return name or city

    def codes_pair(side):
        c = ((ap.get(side) or {}).get("code") or {})
        ia, ic = c.get("iata"), c.get("icao")
        return f"{ia}/{ic}" if ia and ic and ia != ic else (ia or ic or "")

    def city(side):
        a = ap.get(side) or {}
        return (((a.get("position") or {}).get("region") or {}).get("city")) or ""

    def apos(side):
        p = (ap.get(side) or {}).get("position") or {}
        return p.get("latitude"), p.get("longitude")

    o_pos, d_pos = apos("origin"), apos("destination")
    route_km = 0
    if None not in o_pos and None not in d_pos:
        route_km = _haversine_km(o_pos[0], o_pos[1], d_pos[0], d_pos[1])

    reg = ((flight.get("aircraft") or {}).get("identification") or {}).get("registration") or "PS-VIS"

    return {
        "reg": reg,
        "origin": code("origin"),
        "destination": code("destination"),
        "origin_name": friendly("origin"),
        "destination_name": friendly("destination"),
        "o_codes": codes_pair("origin"),
        "o_city": city("origin"),
        "d_city": city("destination"),
        "route_km": route_km,
        "model": (((flight.get("aircraft") or {}).get("model") or {}).get("text")) or "",
        "fr24_id": ((flight.get("identification") or {}).get("id")) or "",
        "dep_ts": dep_ts,
        "arr_ts": arr_ts,
        "duration_s": arr_ts - dep_ts,
        "cruise_alt_ft": cruise_alt,
        "cruise_kmh": cruise_kmh,
        "cruise_kt": cruise_kt,
        "cruise_i0": c0,
        "cruise_i1": c1,
        "max_alt_ft": max_alt,
        "max_kmh": max(kmh),
        "max_kt": max(kts),
        "dist_km": dist_km,
        "track": track,
        "ts": ts,
        "alt": alt,
        "kmh": kmh,
        "kts": kts,
    }


def build_caption(s):
    """The SINGLE WhatsApp landing message (the chart travels as its image).
    Mirrors the HA fallback text and adds the cruise lines before the link."""
    dep = datetime.fromtimestamp(s["dep_ts"], TZ_LOCAL).strftime("%H:%M")
    arr = datetime.fromtimestamp(s["arr_ts"], TZ_LOCAL).strftime("%H:%M")
    lines = [f"🛬 *{s['reg']} pousou em {s['d_city'] or s['destination']}*"]
    origem = s["origin_name"] or s["o_city"] or s["origin"]
    line = f"✈️ Origem: {origem}"
    if s["o_codes"]:
        line += f" ({s['o_codes']})"
    if s["o_city"] and s["o_city"] != origem:
        line += f" — {s['o_city']}"
    lines.append(line)
    lines.append(f"🕐 Saída {dep} → Chegada {arr} ({_fmt_hm(s['duration_s'])})")
    if s["model"]:
        lines.append(f"🛩️ {s['model']}")
    if s["route_km"]:
        lines.append(f"📏 Rota: ~{_fmt_int_br(s['route_km'])} km")
    lines.append(f"⛰️ Altitude de Cruzeiro: {_fmt_int_br(s['cruise_alt_ft'])} ft")
    max_note = "" if s["max_kt"] <= s["cruise_kt"] else f" · máx {_fmt_int_br(s['max_kt'])} kt"
    lines.append(
        f"💨 Velocidade de Cruzeiro: {_fmt_int_br(s['cruise_kt'])} kt "
        f"({_fmt_int_br(s['cruise_kmh'])} km/h){max_note}"
    )
    if s["fr24_id"]:
        lines.append(
            f"🔗 https://www.flightradar24.com/data/aircraft/{s['reg'].lower()}#{s['fr24_id']}"
        )
    return "\n".join(lines)


def build_chart(s):
    """Altitude + speed profile as PNG bytes (two panels, shared time axis)."""
    times = [datetime.fromtimestamp(t, TZ_LOCAL) for t in s["ts"]]
    day = times[0].strftime("%d/%m/%Y")

    fig, (ax1, ax2) = plt.subplots(
        2, 1, sharex=True, figsize=(8, 5.6), dpi=150,
        gridspec_kw={"height_ratios": [1.4, 1.1], "hspace": 0.22},
    )
    fig.patch.set_facecolor(SURFACE)
    fig.suptitle(
        f"PS-VIS · {s['origin']} → {s['destination']} · {day}",
        color=INK, fontsize=13, fontweight="bold", x=0.06, ha="left",
    )

    for ax in (ax1, ax2):
        ax.set_facecolor(SURFACE)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(GRID)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.tick_params(colors=INK_2, labelsize=9, length=0)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _fmt_int_br(v)))

    ax1.fill_between(times, s["alt"], color=BLUE, alpha=0.12, linewidth=0)
    ax1.plot(times, s["alt"], color=BLUE, linewidth=1.8)
    ax1.set_title("Altitude (ft)", loc="left", color=INK_2, fontsize=10)
    ax1.set_ylim(bottom=0)
    # selective direct label: the cruise plateau
    i_max = s["alt"].index(s["max_alt_ft"])
    ax1.annotate(
        f"{_fmt_int_br(s['cruise_alt_ft'])} ft",
        (times[i_max], s["max_alt_ft"]), textcoords="offset points",
        xytext=(0, 6), ha="center", color=INK, fontsize=9,
    )
    ax1.set_ylim(top=s["max_alt_ft"] * 1.15)

    # Speed in kt only. Ground/taxi points are masked and the axis is anchored
    # just below the airborne envelope so the cruise band gets the vertical
    # room — a linear window instead of a non-linear scale, which would distort.
    flying = [i for i, k in enumerate(s["kts"]) if k >= 50]
    if flying:
        f0, f1 = flying[0], flying[-1]
        v_lo = min(s["kts"][f0:f1 + 1])
        ax2.plot(times[f0:f1 + 1], s["kts"][f0:f1 + 1], color=ORANGE, linewidth=1.8)
    else:
        v_lo = 0
        ax2.plot(times, s["kts"], color=ORANGE, linewidth=1.8)
    ax2.set_ylim(bottom=max(0, v_lo * 0.92), top=s["max_kt"] * 1.10)
    ax2.set_title("Velocidade (kt)", loc="left", color=INK_2, fontsize=10)
    i_vmax = s["kts"].index(s["max_kt"])
    ax2.annotate(
        f"máx {_fmt_int_br(s['max_kt'])} kt",
        (times[i_vmax], s["max_kt"]), textcoords="offset points",
        xytext=(0, 5), ha="center", color=INK, fontsize=9,
    )
    # cruise speed = mean over the stabilized stretch at cruise altitude,
    # highlighted as a dashed reference spanning exactly that stretch
    c0, c1 = s["cruise_i0"], s["cruise_i1"]
    ax2.plot(
        [times[c0], times[c1]], [s["cruise_kt"]] * 2,
        color=INK_2, linewidth=1.2, linestyle=(0, (4, 3)),
    )
    ax2.annotate(
        f"cruzeiro {_fmt_int_br(s['cruise_kt'])} kt",
        (times[(c0 + c1) // 2], s["cruise_kt"]), textcoords="offset points",
        xytext=(0, -13), ha="center", color=INK, fontsize=9,
    )

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=TZ_LOCAL))
    fig.subplots_adjust(left=0.07, right=0.96, top=0.89, bottom=0.07)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()


def build_report_image(s):
    """Profile chart stacked over the OSM route map, as one PNG. The map is
    best-effort — tile fetch problems degrade to the chart alone."""
    from PIL import Image

    chart = Image.open(io.BytesIO(build_chart(s)))
    try:
        import maptile

        route = maptile.render_path(s["track"], width=chart.width, height=520)
    except Exception:
        logging.getLogger("psvis.report").warning("route map failed", exc_info=True)
        route = None
    if route is None:
        out_img = chart
    else:
        out_img = Image.new("RGB", (chart.width, chart.height + route.height), SURFACE)
        out_img.paste(chart, (0, 0))
        out_img.paste(route, (0, chart.height))
    buf = io.BytesIO()
    out_img.save(buf, format="PNG")
    return buf.getvalue()


if __name__ == "__main__":
    # local smoke test: python report.py <playback.json> [out.png]
    import json
    import sys

    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
    flight = data["result"]["response"]["data"]["flight"]
    stats = compute_stats(flight)
    print(build_caption(stats))
    out = sys.argv[2] if len(sys.argv) > 2 else "chart.png"
    with open(out, "wb") as fh:
        fh.write(build_report_image(stats))
    print("report image ->", out)
