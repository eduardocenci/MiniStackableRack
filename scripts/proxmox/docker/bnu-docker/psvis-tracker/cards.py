"""Light flight cards (1080x1350 PNG) for the "Aeronave PS-VIS" WhatsApp group.

One card per stage (decisão Eduardo 2026-10-03 — options A-E, light cards):

  take-off   takeoff_card()   chips + departure map + detailed METAR panel
                              with a weather icon — NO destination guessing:
                              where it goes is the T+10 card's job
  en-route   enroute_card()   chips + compass, progress map with dashed legs to
                              the candidate destinations and ETA chips (E),
                              flown profile + terrain ahead (B)
  landing    landing_cards()  1/2: chips incl. "vs esperado" + hero map, route
                              coloured by altitude, previous flight as a ghost
                              (C, D); 2/2: altitude over real terrain by
                              distance with phases (A, B), then altitude AND
                              speed vs minutes since wheels-up next to the
                              previous flights (D)

Every public function raises on failure — the caller (app.py) then falls back
to the legacy chart/map or plain text, so a card bug never costs a message.
Rendering uses the object-oriented Figure API under a lock (the tracker is
multi-threaded; pyplot's global state is not).
"""
import io
import json
import logging
import math
import os
import sqlite3
import threading
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe  # noqa: E402
import numpy as np  # noqa: E402
import requests  # noqa: E402
from matplotlib import font_manager as fm  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, Normalize  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import Circle, FancyBboxPatch, Polygon  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402
from PIL import Image, ImageEnhance, ImageFont  # noqa: E402

import db  # noqa: E402
import enroute  # noqa: E402
import maptile  # noqa: E402
import metar  # noqa: E402
from report import TZ_LOCAL, _fmt_int_br, _haversine_km  # noqa: E402

log = logging.getLogger("psvis.cards")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
TERRAIN_DIR = os.path.join(DATA_DIR, "terrain")
W, H, DPI = 1080, 1350, 100
M = 48
_LOCK = threading.Lock()

# ── palette (dataviz reference, light) ───────────────────────────────────────
SURFACE = "#fcfcfb"
CHIP = "#f1f0ec"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BORDER = "#dedcd4"
BLUE = "#2a78d6"        # altitude (categorical slot 1)
BLUE_DD = "#104281"
ORANGE = "#eb6834"      # speed (categorical slot 2)
GOOD_TXT = "#006300"
GOOD_BG = "#e5f2e1"
WARN_TXT = "#7a4f00"
WARN_BG = "#fbf0d6"
PILL_BG = "#e3eefc"
PILL_INK = "#184f95"
TERRAIN = "#dad6ca"
GHOST = "#b3b1a8"       # previous flights
ALT_CMAP = LinearSegmentedColormap.from_list(
    "psvis_alt", ["#86b6ef", "#3987e5", "#1c5cab", "#0d366b"])

# ── fonts: Inter (Debian fonts-inter), DejaVu Sans if it is missing ──────────
_INTER = "/usr/share/fonts/opentype/inter/Inter-{}.otf"
if os.path.exists(_INTER.format("Regular")):
    for _w in ("Regular", "Medium", "SemiBold", "Bold"):
        if os.path.exists(_INTER.format(_w)):
            fm.fontManager.addfont(_INTER.format(_w))
    _FAMILY = fm.FontProperties(fname=_INTER.format("Regular")).get_name()
else:
    log.warning("Inter not installed — cards fall back to DejaVu Sans")
    _FAMILY = "DejaVu Sans"
_PIL_FONTS = {}


def _font_path(weight="Regular"):
    for w in (weight, "Regular"):
        p = _INTER.format(w)
        if os.path.exists(p):
            return p
    return fm.findfont(fm.FontProperties(family="DejaVu Sans"))


def _F(px, weight="Regular"):
    return fm.FontProperties(fname=_font_path(weight), size=px * 72 / DPI)


def _lw(px):
    return px * 72 / DPI


def _text_w(s, px, weight="Regular"):
    key = (int(round(px)), weight)
    if key not in _PIL_FONTS:
        _PIL_FONTS[key] = ImageFont.truetype(_font_path(weight), key[0])
    return _PIL_FONTS[key].getlength(s)


def _fit(s, max_w, px, weight, min_px):
    while px > min_px and _text_w(s, px, weight) > max_w:
        px -= 1
    return px


_HALO = [pe.withStroke(linewidth=_lw(9), foreground=SURFACE)]


def _hm(ts):
    return datetime.fromtimestamp(ts, TZ_LOCAL).strftime("%H:%M")


def _dmy(ts):
    return datetime.fromtimestamp(ts, TZ_LOCAL).strftime("%d/%m/%Y")


def _dur(seconds):
    m = int(round(seconds / 60))
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def _alt(p):
    return (p.get("altitude") or {}).get("feet") or 0


def _circmean(hs):
    hs = [h for h in hs if h is not None]
    if not hs:
        return None
    r = math.pi / 180
    return math.degrees(math.atan2(sum(math.sin(h * r) for h in hs),
                                   sum(math.cos(h * r) for h in hs))) % 360


def _code(a):
    return (a or {}).get("iata") or (a or {}).get("icao") or "?"


# ── canvas (1 data unit = 1 px, y grows downward) ────────────────────────────

def _new_card():
    fig = Figure(figsize=(W / DPI, H / DPI), dpi=DPI)
    FigureCanvasAgg(fig)
    fig.patch.set_facecolor(SURFACE)
    cv = fig.add_axes([0, 0, 1, 1])
    cv.set_xlim(0, W)
    cv.set_ylim(H, 0)
    cv.axis("off")
    cv.set_zorder(0)
    return fig, cv


def _save(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, facecolor=SURFACE)
    return buf.getvalue()


def _axes_px(fig, x, y, w, h, z=1):
    ax = fig.add_axes([x / W, 1 - (y + h) / H, w / W, h / H])
    ax.set_zorder(z)
    ax.patch.set_alpha(0)
    return ax


def _rbox(ax, x, y, w, h, r=18, fc=CHIP, ec="none", width=0, z=1, alpha=1.0):
    p = FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}",
                       fc=fc, ec=ec, lw=_lw(width), zorder=z, alpha=alpha,
                       transform=ax.transData)
    ax.add_patch(p)
    return p


def _txt(ax, x, y, s, px, weight="Regular", color=INK, ha="left", va="baseline", **kw):
    return ax.text(x, y, s, fontproperties=_F(px, weight), color=color, ha=ha, va=va, **kw)


def _header(cv, stage, title, subtitle):
    _txt(cv, M, 66, "PS-VIS · Cirrus G2 Vision Jet", 24, "Medium", MUTED)
    _txt(cv, M, 132, title, _fit(title, W - 2 * M, 54, "SemiBold", 38), "SemiBold")
    _txt(cv, M, 178, subtitle, _fit(subtitle, W - 2 * M, 27, "Regular", 20), "Regular", INK2)
    pw = _text_w(stage, 24, "Medium") + 44
    _rbox(cv, W - M - pw, 36, pw, 46, r=23, fc=PILL_BG)
    _txt(cv, W - M - pw / 2, 36 + 23 + 9, stage, 24, "Medium", PILL_INK, ha="center")


def _chips(cv, y, items, h=132):
    gap = 16
    w = (W - 2 * M - gap * (len(items) - 1)) / len(items)
    for i, it in enumerate(items):
        x = M + i * (w + gap)
        _rbox(cv, x, y, w, h, r=22, fc=it.get("bg", CHIP))
        label, room = it["label"], w - 44 - it.get("reserve", 0)
        _txt(cv, x + 22, y + 38, label, _fit(label, room, 22, "Medium", 16), "Medium",
             it.get("label_color", MUTED))
        _txt(cv, x + 22, y + 90, it["value"], _fit(it["value"], room, 44, "SemiBold", 26),
             "SemiBold", it.get("color", INK))
        sub = it.get("sub")
        if sub:
            spx = _fit(sub, w - 44, 21, "Regular", 18)
            while _text_w(sub, spx) > w - 44 and len(sub) > 4:  # last resort: ellipsis
                sub = sub[:-2].rstrip(" ·") + "…"
            _txt(cv, x + 22, y + 120, sub, spx, "Regular", it.get("sub_color", INK2))
    return w


def _style_axes(ax):
    ax.set_facecolor("none")
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.spines["bottom"].set_linewidth(_lw(1.5))
    ax.grid(axis="y", color=GRID, linewidth=_lw(1.2))
    ax.set_axisbelow(True)
    ax.tick_params(colors=MUTED, labelsize=20 * 72 / DPI, length=0, pad=6)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: _fmt_int_br(v)))


def _nice_ticks(top, steps=(5000, 10000, 15000, 20000)):
    for st in steps:
        if top / st <= 3.5:
            return [t for t in range(0, int(top) + 1, st)]
    return [0, int(top)]


def _km_axis(ax, total):
    step = 50 if total <= 450 else 100 if total <= 1200 else 250
    ticks = list(range(0, int(total) + 1, step))
    ax.set_xticks(ticks, labels=[f"{t} km" if i == len(ticks) - 1 else str(t)
                                 for i, t in enumerate(ticks)])
    ax.set_xlim(0, total)


def _min_axis(ax, xmax, labels=True):
    step = 10 if xmax <= 90 else 20 if xmax <= 180 else 30
    ticks = list(range(0, int(math.ceil(xmax / step) * step) + 1, step))
    if labels:
        ax.set_xticks(ticks, labels=[str(t) for t in ticks[:-1]] + [f"{ticks[-1]} min"])
    else:
        ax.set_xticks(ticks, labels=[""] * len(ticks))
    ax.set_xlim(0, ticks[-1])


# ── geo + terrain ────────────────────────────────────────────────────────────

def _cumdist(latlon):
    d = [0.0]
    for (a, b), (c, e) in zip(latlon, latlon[1:]):
        d.append(d[-1] + _haversine_km(a, b, c, e))
    return d


def _gc_points(lat1, lon1, lat2, lon2, n=64):
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    v1 = np.array([math.cos(p1) * math.cos(l1), math.cos(p1) * math.sin(l1), math.sin(p1)])
    v2 = np.array([math.cos(p2) * math.cos(l2), math.cos(p2) * math.sin(l2), math.sin(p2)])
    om = math.acos(max(-1.0, min(1.0, float(v1 @ v2))))
    out = []
    for i in range(n + 1):
        t = i / n
        v = v1 if om < 1e-9 else (math.sin((1 - t) * om) * v1 + math.sin(t * om) * v2) / math.sin(om)
        out.append((math.degrees(math.asin(v[2])), math.degrees(math.atan2(v[1], v[0]))))
    return out


def _resample(latlon, step_km):
    d = _cumdist(latlon)
    if len(latlon) < 2 or d[-1] <= 0:
        return [(0.0, latlon[0][0], latlon[0][1])]
    out, j, k = [], 0, 0.0
    while k <= d[-1] + 1e-6:
        while j < len(d) - 2 and d[j + 1] < k:
            j += 1
        seg = (d[j + 1] - d[j]) or 1e-9
        t = min(1.0, max(0.0, (k - d[j]) / seg))
        a, b = latlon[j], latlon[j + 1]
        out.append((k, a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
        k += step_km
    return out


def _terrain(latlon, key, step_km=4.0):
    """(sample distances km, ground elevation ft) under a path — Copernicus DEM
    90 m via Open-Meteo (free, no key, 100 points per call), cached per key.
    Best-effort: None when the service is unreachable."""
    samples = _resample(latlon, step_km)
    path = os.path.join(TERRAIN_DIR, f"{key}.json")
    try:
        if os.path.exists(path):
            with open(path) as fh:
                elev = json.load(fh)
        else:
            elev = []
            for i in range(0, len(samples), 100):
                chunk = samples[i:i + 100]
                r = requests.get(
                    "https://api.open-meteo.com/v1/elevation",
                    params={"latitude": ",".join(f"{p[1]:.4f}" for p in chunk),
                            "longitude": ",".join(f"{p[2]:.4f}" for p in chunk)},
                    timeout=20)
                r.raise_for_status()
                elev += [max(0.0, e) * 3.28084 for e in r.json()["elevation"]]
            os.makedirs(TERRAIN_DIR, exist_ok=True)
            with open(path, "w") as fh:
                json.dump(elev, fh)
        if len(elev) != len(samples):
            raise ValueError("terrain sample count mismatch")
        return [s[0] for s in samples], elev
    except Exception as exc:  # noqa: BLE001
        log.warning("terrain unavailable (%s): %s", key, exc)
        return None


# ── basemap: the OSM tiles maptile already caches, made quiet ────────────────
# CARTO Positron answers "API KEY REQUIRED" tiles keyless (2026-10-03), so the
# light look is derived from OSM: desaturated and lifted toward the surface.
_TILE = 256


def _world(lat, lon, z):
    n = _TILE * 2 ** z
    return ((lon + 180.0) / 360.0 * n,
            (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def _quiet(img):
    img = ImageEnhance.Color(img).enhance(0.28)
    img = ImageEnhance.Contrast(img).enhance(0.9)
    return Image.blend(img, Image.new("RGB", img.size, (252, 252, 251)), 0.42)


class _Basemap:
    """`inset` = px kept clear on each side (top, right, bottom, left) so pin
    labels and ETA chips never fall off the map. Fractional zoom by resampling."""

    def __init__(self, latlon, w, h, inset=(110, 170, 170, 170), min_span_km=140):
        latlon = [(a, b) for a, b in latlon if a is not None and b is not None]
        it, ir, ib, il = inset
        xs, ys = zip(*(_world(a, b, 0) for a, b in latlon))
        span_min = min_span_km / 40075 * _TILE
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        bw = max(max(xs) - min(xs), span_min)
        bh = max(max(ys) - min(ys), span_min)
        scale0 = min((w - il - ir) / bw, (h - it - ib) / bh)
        z = max(3, min(12, round(math.log2(scale0))))
        s = scale0 / 2 ** z
        self.z, self.s, self.w, self.h = z, s, w, h
        self.left = cx * 2 ** z - (il + (w - il - ir) / 2) / s
        self.top = cy * 2 ** z - (it + (h - it - ib) / 2) / s
        ww, hh = w / s, h / s
        tx0, ty0 = int(self.left // _TILE), int(self.top // _TILE)
        tx1, ty1 = int((self.left + ww) // _TILE), int((self.top + hh) // _TILE)
        mosaic = Image.new("RGB", ((tx1 - tx0 + 1) * _TILE, (ty1 - ty0 + 1) * _TILE),
                           (240, 239, 235))
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                if ty < 0 or ty >= 2 ** z:
                    continue
                try:
                    mosaic.paste(maptile._get_tile(z, tx, ty), ((tx - tx0) * _TILE, (ty - ty0) * _TILE))
                except Exception as exc:  # noqa: BLE001 — a hole beats no card
                    log.warning("tile %s/%s/%s failed: %s", z, tx, ty, exc)
        ox, oy = self.left - tx0 * _TILE, self.top - ty0 * _TILE
        crop = mosaic.crop((int(ox), int(oy), int(ox + ww), int(oy + hh)))
        self.img = _quiet(crop.resize((w, h), Image.LANCZOS))

    def xy(self, lat, lon):
        X, Y = _world(lat, lon, self.z)
        return (X - self.left) * self.s, (Y - self.top) * self.s


def _map_axes(fig, rect, bm):
    x, y, w, h = rect
    ax = _axes_px(fig, x, y, w, h, z=2)
    ax.axis("off")
    clip = FancyBboxPatch((0, 0), w, h, boxstyle="round,pad=0,rounding_size=26",
                          fc="none", ec="none", transform=ax.transData)
    im = ax.imshow(bm.img, extent=(0, w, h, 0), interpolation="none", zorder=0)
    im.set_clip_path(clip)
    ax.add_patch(FancyBboxPatch((0, 0), w, h, boxstyle="round,pad=0,rounding_size=26",
                                fc="none", ec=BORDER, lw=_lw(2), zorder=30, clip_on=False))
    ax.set_xlim(0, w)
    ax.set_ylim(h, 0)
    ax.set_autoscale_on(False)
    _txt(ax, w - 18, h - 16, "© OpenStreetMap", 16, "Regular", INK2, ha="right",
         path_effects=_HALO, zorder=31)
    return ax


def _draw_route(ax, bm, latlon, alts, max_alt):
    pts = [bm.xy(a, b) for a, b in latlon]
    if len(pts) < 2:
        return
    xs, ys = zip(*pts)
    ax.plot(xs, ys, color="white", lw=_lw(13), alpha=0.95, solid_capstyle="round",
            solid_joinstyle="round", zorder=4)
    segs = [[pts[i], pts[i + 1]] for i in range(len(pts) - 1)]
    vals = np.array([(alts[i] + alts[i + 1]) / 2 for i in range(len(pts) - 1)])
    lc = LineCollection(segs, cmap=ALT_CMAP, norm=Normalize(0, max(max_alt, 1)),
                        linewidths=_lw(7), capstyle="round", joinstyle="round", zorder=5)
    lc.set_array(vals)
    ax.add_collection(lc)


def _draw_ghost(ax, bm, latlon):
    if len(latlon) < 2:
        return
    xs, ys = zip(*(bm.xy(a, b) for a, b in latlon))
    ax.plot(xs, ys, color=GHOST, lw=_lw(4), alpha=0.9, solid_capstyle="round", zorder=3)


def _draw_dashed(ax, bm, a, b, color=MUTED, width=2.5, z=3):
    xs, ys = zip(*(bm.xy(p, q) for p, q in _gc_points(a[0], a[1], b[0], b[1])))
    ax.plot(xs, ys, color=color, lw=_lw(width), ls=(0, (5, 5)), zorder=z)


def _pin(ax, bm, lat, lon, kind, label, sub=None):
    x, y = bm.xy(lat, lon)
    if kind == "origin":
        ax.add_patch(Circle((x, y), 12, fc="white", ec=INK, lw=_lw(3.5), zorder=8))
    elif kind == "dest":
        ax.add_patch(Circle((x, y), 14, fc=BLUE_DD, ec="white", lw=_lw(4.5), zorder=8))
    else:  # an airport known from history (or FR24's destination hint)
        ax.add_patch(Circle((x, y), 10, fc="white", ec=GHOST, lw=_lw(4), zorder=8))
    right = x <= bm.w * 0.62
    dx, ha = (24, "left") if right else (-24, "right")
    _txt(ax, x + dx, y + 4, label, 32, "SemiBold", INK, ha=ha, path_effects=_HALO, zorder=9)
    if sub:
        _txt(ax, x + dx, y + 36, sub, 23, "Regular", INK2, ha=ha, path_effects=_HALO, zorder=9)
    return right


def _eta_near(ax, bm, lat, lon, s, strong=True):
    x, y = bm.xy(lat, lon)
    w = _text_w(s, 24, "SemiBold") + 32
    right = x <= bm.w * 0.62
    bx = x + 24 if right else x - 24 - w
    by = y + 52 if y < bm.h - 120 else y - 104
    _rbox(ax, bx, by, w, 46, r=23, fc="white", ec=INK2 if strong else GHOST, width=2, z=12)
    _txt(ax, bx + w / 2, by + 23 + 9, s, 24, "SemiBold", INK if strong else INK2,
         ha="center", zorder=13)


def _plane(ax, x, y, heading, scale):
    ax.add_patch(Polygon(maptile._plane_points(x, y, heading, scale * 1.25), closed=True,
                         fc="white", ec="none", zorder=14))
    ax.add_patch(Polygon(maptile._plane_points(x, y, heading, scale), closed=True,
                         fc=BLUE_DD, ec="white", lw=_lw(1), zorder=15))


def _compass(cv, cx, cy, r, heading):
    cv.add_patch(Circle((cx, cy), r, fc="white", ec=AXIS, lw=_lw(2), zorder=4))
    hr = math.radians(heading)
    cv.annotate("", xy=(cx + 18 * math.sin(hr), cy - 18 * math.cos(hr)),
                xytext=(cx - 14 * math.sin(hr), cy + 14 * math.cos(hr)),
                arrowprops=dict(arrowstyle="-|>,head_length=0.6,head_width=0.35",
                                color=BLUE_DD, lw=_lw(3.5)), zorder=5)
    _txt(cv, cx, cy - r - 7, "N", 16, "SemiBold", MUTED, ha="center")


def _legend_row(cv, y, items):
    """Legend under a map, in the card itself — never collides with pins.
    items: ("ramp", label, lo, hi) | ("line", label, color, dashed)."""
    x = M + 4
    for it in items:
        if it[0] == "ramp":
            _, label, lo, hi = it
            _txt(cv, x, y + 8, label, 20, "Medium", INK2)
            x += _text_w(label, 20, "Medium") + 14
            _txt(cv, x, y + 8, lo, 18, "Regular", MUTED)
            x += _text_w(lo, 18) + 8
            grad = np.linspace(0, 1, 256).reshape(1, -1)
            cv.imshow(grad, extent=(x, x + 150, y + 4, y - 8), cmap=ALT_CMAP, aspect="auto",
                      zorder=3, interpolation="bilinear")
            x += 158
            _txt(cv, x, y + 8, hi, 18, "Regular", MUTED)
            x += _text_w(hi, 18) + 34
        else:
            _, label, color, dashed = it
            cv.plot([x, x + 40], [y] * 2, color=color, lw=_lw(3 if dashed else 4.5),
                    ls=(0, (5, 5)) if dashed else "-", solid_capstyle="round", zorder=3)
            _txt(cv, x + 52, y + 8, label, 20, "Regular", INK2)
            x += 52 + _text_w(label, 20) + 34
    cv.set_xlim(0, W)
    cv.set_ylim(H, 0)


# ── flight log + weather ─────────────────────────────────────────────────────

def _rows(sql, args=()):
    with db._conn() as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def _points(fid):
    """(ts, lat, lon, alt_ft, spd_kt) of a logged flight, in order."""
    with db._conn() as c:
        return [tuple(r) for r in c.execute(
            "SELECT ts, lat, lon, alt_ft, spd_kt FROM track_points WHERE fr24_id=? ORDER BY seq",
            (fid,)).fetchall()]


def _history(icao_a, icao_b, exclude_fid, before_ts=None):
    """Previous flights between two airports (both directions), newest first,
    each with its track points."""
    if not icao_a or not icao_b:
        return []
    rows = _rows("SELECT * FROM flights WHERE fr24_id != ? AND dep_ts < ? AND"
                 " ((o_icao=? AND d_icao=?) OR (o_icao=? AND d_icao=?)) ORDER BY dep_ts DESC",
                 (exclude_fid or "", before_ts or 4e9, icao_a, icao_b, icao_b, icao_a))
    for r in rows:
        r["pts"] = _points(r["fr24_id"])
    return [r for r in rows if len(r["pts"]) > 5]


def _metar_at(icao, lat, lon, at_ts):
    """METAR valid at `at_ts` (historical when in the past), nearest reporting
    station when the aerodrome publishes none. Best-effort: None on failure."""
    try:
        obs = metar.by_ids([icao], at_ts=at_ts).get(icao) if icao else None
        if not obs and lat is not None:
            near = metar.nearest(lat, lon, box_deg=1.2, at_ts=at_ts)
            obs = near[0] if near else None
        return obs
    except Exception as exc:  # noqa: BLE001
        log.warning("METAR for card failed: %s", exc)
        return None


def _wx_short(m):
    """Plain-text METAR digest for the card (the image font has no emoji)."""
    if not m:
        return None, None
    parts = []
    if m.get("temp") is not None:
        parts.append(f"{round(m['temp'])}°C")
    if (m.get("wspd") or 0) >= 12:  # same threshold as metar.summarize
        parts.append(f"vento {m['wspd']} kt")
    ceil = min((c.get("base") for c in (m.get("clouds") or [])
                if c.get("cover") in ("BKN", "OVC") and c.get("base")), default=None)
    if ceil is not None:
        parts.append(f"teto {_fmt_int_br(ceil)} ft")
    return (m.get("fltCat") or "—"), " · ".join(parts)


def _vspeed(track):
    cur = track[-1]
    vs = (cur.get("verticalSpeed") or {}).get("fpm")
    if vs is None:
        older = [p for p in track if cur["timestamp"] - p["timestamp"] >= 60]
        if older:
            p = older[-1]
            vs = (_alt(cur) - _alt(p)) / max(1, (cur["timestamp"] - p["timestamp"]) / 60)
    return vs or 0


# ── weather icons (flat vectors in canvas px, y grows downward) ─────────────
# Same condition keys as metar.condition() — the caption emoji and the card
# icon always agree (☀️ ⛅ ☁️ 🌧️ ⛈️ 🌫️).
SUN = "#f2a900"
CLOUD = "#b4bcc6"
CLOUD_DARK = "#7f8895"
RAIN = "#3987e5"
BOLT = "#f5b400"
_COVER_PT = {"FEW": "poucas nuvens", "SCT": "nuvens esparsas", "BKN": "nublado",
             "OVC": "encoberto"}


def _cloud(ax, cx, cy, s, color, z):
    for dx, dy, r in ((-0.17, 0.05, 0.15), (0.03, -0.06, 0.22), (0.22, 0.06, 0.14)):
        ax.add_patch(Circle((cx + dx * s, cy + dy * s), r * s, fc=color, ec="none", zorder=z))
    ax.add_patch(FancyBboxPatch((cx - 0.32 * s, cy + 0.02 * s), 0.68 * s, 0.18 * s,
                                boxstyle=f"round,pad=0,rounding_size={0.09 * s}",
                                fc=color, ec="none", zorder=z, transform=ax.transData))


def _sun(ax, cx, cy, s, z):
    ax.add_patch(Circle((cx, cy), 0.2 * s, fc=SUN, ec="none", zorder=z))
    for k in range(8):
        a = k * math.pi / 4
        ax.plot([cx + 0.28 * s * math.cos(a), cx + 0.40 * s * math.cos(a)],
                [cy + 0.28 * s * math.sin(a), cy + 0.40 * s * math.sin(a)],
                color=SUN, lw=_lw(max(2.0, s * 0.06)), solid_capstyle="round", zorder=z)


def _wx_icon(ax, cx, cy, s, key, z=6):
    """Weather icon ~s px wide centred at (cx, cy)."""
    stroke = _lw(max(2.0, s * 0.055))
    if key == "clear":
        _sun(ax, cx, cy, s, z)
    elif key == "partly":
        _sun(ax, cx - 0.14 * s, cy - 0.14 * s, s * 0.8, z)
        _cloud(ax, cx + 0.06 * s, cy + 0.1 * s, s * 0.85, CLOUD, z + 1)
    elif key == "cloud":
        _cloud(ax, cx, cy, s, CLOUD, z)
    elif key in ("rain", "storm"):
        _cloud(ax, cx, cy - 0.14 * s, s, CLOUD_DARK if key == "storm" else CLOUD, z + 1)
        for dx in ((-0.18, 0.0, 0.18) if key == "rain" else (-0.22, 0.22)):
            ax.plot([cx + dx * s, cx + (dx - 0.06) * s], [cy + 0.14 * s, cy + 0.34 * s],
                    color=RAIN, lw=stroke, solid_capstyle="round", zorder=z)
        if key == "storm":
            bolt = [(0.02, 0.04), (-0.09, 0.24), (0.0, 0.24), (-0.06, 0.42),
                    (0.12, 0.17), (0.03, 0.17), (0.1, 0.04)]
            ax.add_patch(Polygon([(cx + x * s, cy + y * s) for x, y in bolt], closed=True,
                                 fc=BOLT, ec="none", zorder=z + 2))
    else:  # fog / mist: a pale cloud over three wisps
        _cloud(ax, cx, cy - 0.16 * s, s * 0.8, CLOUD, z)
        for i, (x0, x1) in enumerate(((-0.34, 0.26), (-0.24, 0.36), (-0.3, 0.2))):
            ax.plot([cx + x0 * s, cx + x1 * s], [cy + (0.12 + i * 0.12) * s] * 2,
                    color=CLOUD_DARK if key == "fog" else CLOUD, lw=stroke,
                    solid_capstyle="round", zorder=z + 1)


def _wx_detail(m):
    """Everything the METAR panel says, as plain pt-BR strings."""
    if not m:
        return None
    clouds = m.get("clouds") or []
    layer = next((c for c in clouds if c.get("cover") in ("BKN", "OVC")), None) or (
        clouds[0] if clouds else None)
    if layer and layer.get("base"):
        sky = f"{_COVER_PT.get(layer.get('cover'), layer.get('cover'))} a {_fmt_int_br(layer['base'])} ft"
    else:
        sky = "céu claro"
    ceil = layer.get("base") if layer and layer.get("cover") in ("BKN", "OVC") else None
    short = (f"teto {_fmt_int_br(ceil)} ft" if ceil else
             _COVER_PT.get(layer.get("cover"), "nuvens") if layer else "céu claro")
    wspd, wdir = m.get("wspd") or 0, m.get("wdir")
    if not wspd:
        wind = "vento calmo"
    else:
        wind = (f"vento {wdir:03d}° {wspd} kt" if isinstance(wdir, int) else f"vento variável {wspd} kt")
        if m.get("wgst"):
            wind += f" (rajadas {m['wgst']})"
    vis = m.get("visib")
    try:
        vis_txt = ("visibilidade ≥10 km" if str(vis).endswith("+")
                   else f"visibilidade {round(float(vis) * 1.609, 1):g} km" if vis is not None else None)
    except ValueError:
        vis_txt = None
    obs = m.get("obsTime")
    return {"key": metar.condition(m), "cat": m.get("fltCat") or "—", "icao": m.get("icaoId"),
            "name": (m.get("name") or "").split(",")[0].strip(),
            "time": _hm(obs) if obs else None, "temp": m.get("temp"), "sky": sky, "short": short,
            "wind": wind, "vis": vis_txt, "raw": m.get("rawOb") or ""}


def _clip_text(s, max_w, px, weight="Regular"):
    while _text_w(s, px, weight) > max_w and len(s) > 4:
        s = s[:-2].rstrip() + "…"
    return s


# ═══ take-off ══════════════════════════════════════════════════════════════

def takeoff_card(origin, dep_ts, track, fid, next_ts=None):
    """(png, caption) at wheels-up. No destination guessing (decisão Eduardo
    2026-10-03): where it is going is the T+10 card's job — this one shows
    the departure, the weather there and when that update comes."""
    with _LOCK, matplotlib.rc_context({"font.family": _FAMILY}):
        return _takeoff(origin, dep_ts, track or [], fid, next_ts or dep_ts + 600)


def _takeoff(o, dep, track, fid, next_ts):
    if o.get("lat") is None:
        raise ValueError("origin has no coordinates")
    air = [p for p in track if _alt(p) > 0]
    head0 = _circmean([p.get("heading") for p in air][:6])
    wx = _metar_at(o.get("icao"), o["lat"], o["lon"], dep)
    det = _wx_detail(wx)
    city = o.get("city") or _code(o)

    fig, cv = _new_card()
    _header(cv, "Decolagem", f"Decolou de {city}", f"{_dmy(dep)} · {enroute._label(o)}")
    cw = _chips(cv, 210, [
        {"label": "Decolagem", "value": _hm(dep), "sub": _dmy(dep)},
        {"label": "Origem", "value": _code(o), "sub": city},
        {"label": f"Meteo ({det['icao']})" if det else "Meteo", "reserve": 58 if det else 0,
         "value": det["cat"] if det else "—",
         "sub": ((f"{round(det['temp'])}°C · " if det["temp"] is not None else "") + det["short"]
                 if det else "sem METAR por perto")},
        {"label": "Próxima atualização", "value": f"~{_hm(next_ts)}", "sub": "rumo e chegada"},
    ])
    if det:
        _wx_icon(cv, M + 2 * (cw + 16) + cw - 46, 284, 60, det["key"])

    # departure map: the airport's region, the climb-out so far, the aircraft
    rect = (M, 366, W - 2 * M, 720)
    ia = [i for i, p in enumerate(track) if _alt(p) > 0]
    trail = track[max(0, ia[0] - 1):] if ia else []
    pts = [(o["lat"], o["lon"])] + [(p["latitude"], p["longitude"]) for p in trail]
    bm = _Basemap(pts, rect[2], rect[3], inset=(140, 200, 140, 200), min_span_km=160)
    ax = _map_axes(fig, rect, bm)
    if len(trail) > 1:
        _draw_route(ax, bm, [(p["latitude"], p["longitude"]) for p in trail],
                    [_alt(p) for p in trail], max(_alt(p) for p in trail))
    _pin(ax, bm, o["lat"], o["lon"], "origin", _code(o), f"decolou {_hm(dep)}")
    if head0 is not None:
        ox, oy = bm.xy(o["lat"], o["lon"])
        px, py = bm.xy(trail[-1]["latitude"], trail[-1]["longitude"]) if trail else (ox, oy)
        if math.hypot(px - ox, py - oy) < 56:  # keep the aircraft clear of the pin
            r = math.radians(head0)
            px, py = ox + 56 * math.sin(r), oy - 56 * math.cos(r)
        _plane(ax, px, py, head0, 1.6)

    # METAR panel — the departure weather in full, icon first
    y0 = 1110
    _rbox(cv, M, y0, W - 2 * M, 190, r=22, fc=CHIP)
    if det:
        _wx_icon(cv, M + 96, y0 + 104, 124, det["key"])
        x0, room = M + 196, W - 2 * M - 196 - 24
        _txt(cv, x0, y0 + 56, f"Meteo na saída · {det['cat']}", 30, "SemiBold")
        where = f"{det['name']} ({det['icao']})" if det["name"] else det["icao"]
        line2 = where + (f" · observação das {det['time']}" if det["time"] else "")
        _txt(cv, x0, y0 + 96, _clip_text(line2, room, 22), 22, "Regular", INK2)
        bits = [f"{round(det['temp'])}°C" if det["temp"] is not None else None,
                det["sky"], det["wind"], det["vis"]]
        line3 = " · ".join(b for b in bits if b)
        _txt(cv, x0, y0 + 132, line3, _fit(line3, room, 22, "Regular", 17), "Regular", INK2)
        _txt(cv, x0, y0 + 166, _clip_text(det["raw"], room, 18), 18, "Regular", MUTED)
    else:
        _txt(cv, M + 32, y0 + 104, "Sem METAR por perto no horário da decolagem", 24,
             "Regular", INK2)
    png = _save(fig)

    lines = [f"🛫 *PS-VIS decolou de {city}*", f"🕐 Decolagem {_hm(dep)}"]
    if wx:
        lines.append(f"🌦️ Meteo na saída: {metar.summarize(wx)} ({wx.get('icaoId')})")
    lines.append(f"🔗 https://www.flightradar24.com/data/aircraft/ps-vis#{fid}")
    return png, "\n".join(lines)


# ═══ en-route (T+10) ═══════════════════════════════════════════════════════

def enroute_card(st, fid):
    """PNG for the en-route update from an enroute.analyze() state."""
    with _LOCK, matplotlib.rc_context({"font.family": _FAMILY}):
        return _enroute(st, fid)


def _enroute(st, fid):
    o, track, cur = st["origin"], st["track"], st["cur"]
    t0, heading, cands = st["dep_ts"], st["heading"], st["cands"]
    cur_alt, cur_kt, mins = st["cur_alt"], st["cur_kt"], st["mins"]
    vs = _vspeed(track)
    trend = (f"subindo {_fmt_int_br(abs(vs))} ft/min" if vs > 300 else
             f"descendo {_fmt_int_br(abs(vs))} ft/min" if vs < -300 else "nivelado")
    top = cands[0] if cands else None
    city = o.get("city") or _code(o)
    m_top = _metar_at(top.get("icao"), top["lat"], top["lon"], st["now_ts"]) if top else None

    fig, cv = _new_card()
    _header(cv, f"Em voo · T+{mins}", f"Em voo, rumo {st['cardinal']}",
            f"Decolou de {city} às {_hm(t0)} · há {mins} min")
    if top:
        lo, hi = _hm(t0 + top["lo_s"]), _hm(t0 + top["hi_s"])
        eta_chip = {"label": "Chegada", "value": f"~{lo}" if lo == hi else f"~{lo}–{hi}",
                    "sub": f"{_code(top)} · pelo histórico", "reserve": 58 if m_top else 0}
    else:
        eta_chip = {"label": "Chegada", "value": "rota nova", "sub": "sem histórico no rumo"}
    cw = _chips(cv, 210, [
        {"label": "Altitude", "value": f"{_fmt_int_br(cur_alt)} ft", "sub": trend},
        {"label": "Velocidade", "value": f"{_fmt_int_br(cur_kt)} kt",
         "sub": f"{_fmt_int_br(cur_kt * 1.852)} km/h"},
        {"label": "Rumo", "value": st["cardinal"], "sub": f"{round(heading)}° verdadeiro"},
        eta_chip,
    ])
    _compass(cv, M + 2 * (cw + 16) + cw - 48, 280, 26, heading)
    if m_top:  # weather at the most likely destination, at a glance
        _wx_icon(cv, M + 3 * (cw + 16) + cw - 46, 284, 60, metar.condition(m_top))

    rect = (M, 366, W - 2 * M, 540)
    flown = [(p["latitude"], p["longitude"]) for p in track]
    ghosts = _history(o.get("icao"), top["icao"], fid, before_ts=t0) if top else []
    pts = flown + [(c["lat"], c["lon"]) for c in cands[:3]]
    if o.get("lat") is not None:
        pts.append((o["lat"], o["lon"]))
    bm = _Basemap(pts, rect[2], rect[3])
    ax = _map_axes(fig, rect, bm)
    for g in ghosts:
        _draw_ghost(ax, bm, [(p[1], p[2]) for p in g["pts"]])
    for c in reversed(cands[:3]):
        _draw_dashed(ax, bm, (cur["latitude"], cur["longitude"]), (c["lat"], c["lon"]),
                     color=INK2 if c is top else GHOST, width=3 if c is top else 2.5, z=6)
    _draw_route(ax, bm, flown, [_alt(p) for p in track], max(cur_alt, 1))
    if o.get("lat") is not None:
        _pin(ax, bm, o["lat"], o["lon"], "origin", _code(o), f"decolou {_hm(t0)}")
    for c in cands[:3]:
        _pin(ax, bm, c["lat"], c["lon"], "hist", _code(c), c.get("city"))
        lo, hi = _hm(t0 + c["lo_s"]), _hm(t0 + c["hi_s"])
        _eta_near(ax, bm, c["lat"], c["lon"], f"~{lo}" if lo == hi else f"~{lo}–{hi}",
                  strong=c is top)
    x, y = bm.xy(cur["latitude"], cur["longitude"])
    _plane(ax, x, y, heading, 1.45)
    leg = [("ramp", "Altitude", "solo", f"{_fmt_int_br(cur_alt)} ft")]
    if ghosts:
        leg.append(("line", "voo anterior na rota", GHOST, False))
    if cands:
        leg.append(("line", "a seguir", INK2, True))
    _legend_row(cv, 940, leg)

    # B strip — flown profile, terrain under the whole route to the top candidate
    y0 = 1028
    path = flown + (_gc_points(cur["latitude"], cur["longitude"], top["lat"], top["lon"], 48)[1:]
                    if top else [])
    total = _cumdist(path)[-1]
    d_flown = _cumdist(flown)
    now_x = d_flown[-1]
    terr = _terrain(path, f"{fid}_enroute_{int(st['now_ts'])}")
    alts = [_alt(p) for p in track]
    ia = [i for i, a in enumerate(alts) if a > 0]
    fx = [d_flown[ia[0] - 1] if ia[0] else 0.0] + [d_flown[i] for i in ia]
    ground0 = float(np.interp(fx[0], terr[0], terr[1])) if terr else 0.0
    fy = [ground0] + [alts[i] for i in ia]
    cruise_ref = (sum(g["cruise_alt_ft"] for g in ghosts) / len(ghosts)) if ghosts else None
    _txt(cv, M, y0 - 18, "Perfil até agora e relevo à frente", 26, "Medium", INK2)
    ax2 = _axes_px(fig, M + 70, y0, W - 2 * M - 70, 220)
    _style_axes(ax2)
    ax2.axvspan(now_x, max(total, now_x + 1), color=CHIP, lw=0, zorder=0)
    if terr:
        ax2.fill_between(terr[0], terr[1], color=TERRAIN, lw=0, zorder=1)
    ax2.plot(fx, fy, color=BLUE, lw=_lw(4), zorder=3, solid_capstyle="round")
    ymax = max(cruise_ref or 0, cur_alt, 5000) * 1.25
    if cruise_ref:
        ax2.plot([now_x, total], [cruise_ref] * 2, color=GHOST, lw=_lw(2.5), ls=(0, (2, 4)), zorder=2)
        ax2.text(total - 4, cruise_ref + ymax * 0.05, f"cruzeiro típico {_fmt_int_br(cruise_ref)} ft",
                 fontproperties=_F(19), color=INK2, ha="right", va="bottom")
    ax2.plot([now_x], [cur_alt], marker="o", ms=_lw(13), mfc=BLUE_DD, mec="white", mew=_lw(3), zorder=5)
    ax2.text(now_x + 8, cur_alt + ymax * 0.05, "agora", fontproperties=_F(20, "SemiBold"),
             color=INK, ha="left", va="bottom", zorder=6)
    if top:
        ax2.text((now_x + total) / 2, ymax * 0.12 + (max(terr[1]) if terr else 0),
                 f"faltam ~{_fmt_int_br(total - now_x)} km até {_code(top)}",
                 fontproperties=_F(20), color=INK2, ha="center", va="bottom")
    ax2.set_ylim(0, ymax)
    ax2.set_yticks(_nice_ticks(ymax * 0.95))
    _km_axis(ax2, max(total, now_x + 1))

    if m_top:
        cat, sub = _wx_short(m_top)
        _wx_icon(cv, M + 24, H - 36, 46, metar.condition(m_top))
        line = f"Meteo em {top.get('city') or _code(top)} ({m_top.get('icaoId')}): {cat}"
        _txt(cv, M + 60, H - 26, line + (f" · {sub}" if sub else ""), 22, "Regular", INK2)
    return _save(fig)


# ═══ landing ═══════════════════════════════════════════════════════════════

def landing_cards(flight, stats):
    """(png 1/2 route, png 2/2 profile). `stats` from report.compute_stats,
    with stats['expected'] attached when the route has history."""
    with _LOCK, matplotlib.rc_context({"font.family": _FAMILY}):
        ap = flight.get("airport") or {}
        o = enroute._airport_side(ap.get("origin"))
        d = enroute._airport_side(ap.get("destination"))
        ghosts = _history(o.get("icao"), d.get("icao"), stats["fr24_id"], before_ts=stats["dep_ts"])
        return (_landing_route(o, d, stats, ghosts),
                _landing_profile(o, d, stats, ghosts))


def _delta_chip(stats):
    exp = stats.get("expected")
    if not exp:
        return {"label": "vs esperado", "value": "1º voo", "sub": "sem histórico na rota"}
    delta = stats["duration_s"] - exp["mean_s"]
    basis = f"média {_dur(exp['mean_s'])}" + (" (volta)" if exp["reverse"] else "")
    if abs(delta) < 60:
        return {"label": "vs esperado", "value": "no tempo", "sub": basis}
    good = delta < 0
    return {"label": "vs esperado", "value": ("−" if good else "+") + _dur(abs(delta)),
            "sub": basis, "bg": GOOD_BG if good else WARN_BG,
            "color": GOOD_TXT if good else WARN_TXT,
            "label_color": GOOD_TXT if good else WARN_TXT,
            "sub_color": GOOD_TXT if good else WARN_TXT}


def _landing_route(o, d, stats, ghosts):
    dep, arr = stats["dep_ts"], stats["arr_ts"]
    fig, cv = _new_card()
    d_city = d.get("city") or d.get("iata") or d.get("icao")
    o_city = o.get("city") or _code(o)
    _header(cv, "Pouso · 1/2", f"Pousou em {d_city}" if d_city else "Pousou",
            f"{_dmy(dep)} · {o_city} → {d_city or 'destino desconhecido'}")
    _chips(cv, 210, [
        {"label": "Tempo de voo", "value": _dur(stats["duration_s"]), "sub": f"{_hm(dep)} → {_hm(arr)}"},
        _delta_chip(stats),
        {"label": "Cruzeiro", "value": f"{_fmt_int_br(stats['cruise_alt_ft'])} ft",
         "sub": f"{_fmt_int_br(stats['cruise_kt'])} kt · {_fmt_int_br(stats['cruise_kmh'])} km/h"},
        {"label": "Distância voada", "value": f"{_fmt_int_br(stats['dist_km'])} km",
         "sub": (f"linha reta {_fmt_int_br(stats['route_km'])} km" if stats["route_km"]
                 else "trajeto FR24")},
    ])
    rect = (M, 366, W - 2 * M, 870)
    ll = [(p["latitude"], p["longitude"]) for p in stats["track"]]
    pts = list(ll)
    for g in ghosts:
        pts += [(p[1], p[2]) for p in g["pts"]][::10]
    bm = _Basemap(pts, rect[2], rect[3])
    ax = _map_axes(fig, rect, bm)
    for g in ghosts:
        _draw_ghost(ax, bm, [(p[1], p[2]) for p in g["pts"]])
    if o.get("lat") is not None and d.get("lat") is not None:
        _draw_dashed(ax, bm, (o["lat"], o["lon"]), (d["lat"], d["lon"]))
    _draw_route(ax, bm, ll, stats["alt"], stats["max_alt_ft"])
    if o.get("lat") is not None:
        _pin(ax, bm, o["lat"], o["lon"], "origin", _code(o), f"decolou {_hm(dep)}")
    if d.get("lat") is not None:
        _pin(ax, bm, d["lat"], d["lon"], "dest", _code(d), f"pousou {_hm(arr)}")
    else:  # unknown destination: mark where it touched down
        x, y = bm.xy(*ll[-1])
        ax.add_patch(Circle((x, y), 14, fc=BLUE_DD, ec="white", lw=_lw(4.5), zorder=8))
        _txt(ax, x + 24, y + 4, f"pousou {_hm(arr)}", 26, "SemiBold", INK, path_effects=_HALO, zorder=9)
    leg = [("ramp", "Altitude", "solo", f"{_fmt_int_br(stats['max_alt_ft'])} ft")]
    if ghosts:
        g = ghosts[0]
        rev = g["o_icao"] != o.get("icao")
        more = f" +{len(ghosts) - 1}" if len(ghosts) > 1 else ""
        leg.append(("line", f"voo anterior · {_dmy(g['dep_ts'])[:5]}" + (" (volta)" if rev else "") + more,
                    GHOST, False))
    if o.get("lat") is not None and d.get("lat") is not None:
        leg.append(("line", "linha reta", MUTED, True))
    _legend_row(cv, 1280, leg)
    return _save(fig)


def _landing_profile(o, d, stats, ghosts):
    fid, dep, arr = stats["fr24_id"], stats["dep_ts"], stats["arr_ts"]
    ts, alt, kts = stats["ts"], stats["alt"], stats["kts"]
    c0, c1 = stats["cruise_i0"], stats["cruise_i1"]
    ll = [(p["latitude"], p["longitude"]) for p in stats["track"]]
    dist = _cumdist(ll)
    terr = _terrain(ll, f"{fid}_route")
    ia = [i for i, a in enumerate(alt) if a > 0]
    fx, fy = [dist[i] for i in ia], [alt[i] for i in ia]
    if terr:  # anchor the line to the runway elevation (FR24 says 0 ft on the ground)
        if ia[0] > 0:
            fx, fy = [dist[ia[0] - 1]] + fx, [float(np.interp(dist[ia[0] - 1], *terr))] + fy
        if ia[-1] + 1 < len(dist):
            x1 = dist[ia[-1] + 1]
            fx, fy = fx + [x1], fy + [float(np.interp(x1, *terr))]
    climb_s, cruise_s, desc_s = ts[c0] - dep, ts[c1] - ts[c0], arr - ts[c1]
    climb_rate = (alt[c0] - fy[0]) / max(1, climb_s / 60)
    desc_rate = (alt[c1] - fy[-1]) / max(1, desc_s / 60)
    o_city, d_city = o.get("city") or _code(o), d.get("city") or _code(d)

    fig, cv = _new_card()
    _header(cv, "Pouso · 2/2", "Perfil do voo", f"{o_city} → {d_city} · {_dmy(dep)}")
    if terr:
        i_t = int(np.argmax(terr[1]))
        last = {"label": "Relevo máx.", "value": f"{_fmt_int_br(terr[1][i_t] / 3.28084)} m",
                "sub": f"a {_fmt_int_br(terr[0][i_t])} km da origem"}
    else:
        last = {"label": "Máxima", "value": f"{_fmt_int_br(stats['max_kt'])} kt",
                "sub": f"{_fmt_int_br(stats['max_kmh'])} km/h"}
    _chips(cv, 210, [
        {"label": "Subida", "value": _dur(climb_s), "sub": f"~{_fmt_int_br(climb_rate)} ft/min"},
        {"label": "Cruzeiro", "value": _dur(cruise_s),
         "sub": f"{_fmt_int_br(stats['cruise_alt_ft'])} ft · {_fmt_int_br(stats['cruise_kt'])} kt"},
        {"label": "Descida", "value": _dur(desc_s), "sub": f"~{_fmt_int_br(desc_rate)} ft/min"},
        last,
    ])

    # B — altitude over terrain, by distance, phases marked
    total = dist[-1]
    _txt(cv, M, 398, "Altitude e relevo ao longo da rota", 26, "Medium", INK2)
    ax = _axes_px(fig, M + 70, 414, W - 2 * M - 70, 250)
    _style_axes(ax)
    top_alt = stats["max_alt_ft"]
    if terr:
        ax.fill_between(terr[0], terr[1], color=TERRAIN, lw=0, zorder=1)
        ax.annotate(f"{_fmt_int_br(terr[1][i_t] / 3.28084)} m", (terr[0][i_t], terr[1][i_t]),
                    xytext=(0, 8), textcoords="offset points", ha="center", va="bottom",
                    fontproperties=_F(19), color=INK2)
    ax.plot(fx, fy, color=BLUE, lw=_lw(4), zorder=3, solid_joinstyle="round")
    for i in (c0, c1):
        ax.plot([dist[i]] * 2, [0, alt[i]], color=AXIS, lw=_lw(2), ls=(0, (3, 4)), zorder=2)
    for x, lab in (((fx[0] + dist[c0]) / 2, f"subida · {_dur(climb_s)}"),
                   ((dist[c0] + dist[c1]) / 2, f"cruzeiro · {_dur(cruise_s)}"),
                   ((dist[c1] + fx[-1]) / 2, f"descida · {_dur(desc_s)}")):
        ax.text(x, top_alt * 1.08, lab, fontproperties=_F(20, "Medium"), color=INK2,
                ha="center", va="bottom")
    ax.set_ylim(0, top_alt * 1.3)
    ax.set_yticks(_nice_ticks(top_alt * 1.05))
    _km_axis(ax, total)

    # D — altitude AND speed vs minutes since wheels-up, next to previous flights
    head = "Comparado aos voos anteriores"
    _txt(cv, M, 752, head, 26, "Medium", INK2)
    exp = stats.get("expected")
    if exp and abs(stats["duration_s"] - exp["mean_s"]) >= 60:
        dl = stats["duration_s"] - exp["mean_s"]
        _txt(cv, M + _text_w(head, 26, "Medium") + 18, 752,
             f"· {_dur(abs(dl))} {'mais rápido' if dl < 0 else 'mais lento'}", 26, "SemiBold",
             GOOD_TXT if dl < 0 else WARN_TXT)
    elif not ghosts:
        _txt(cv, M + _text_w(head, 26, "Medium") + 18, 752, "· primeiro voo nesta rota",
             26, "Regular", MUTED)

    def series(points, t0, t1, idx):
        sel = [p for p in points if t0 <= p[0] <= t1]
        return [(p[0] - t0) / 60 for p in sel], [p[idx] or 0 for p in sel]

    mine = [(t, a, k) for t, a, k in zip(ts, alt, kts)]
    mx, my = series(mine, dep, arr, 1)
    _, mk = series(mine, dep, arr, 2)
    xmax = mx[-1]
    gser = []
    for g in ghosts[:4]:
        pts = [(p[0], p[3], p[4]) for p in g["pts"]]
        gx, gy = series(pts, g["dep_ts"], g["arr_ts"], 1)
        _, gk = series(pts, g["dep_ts"], g["arr_ts"], 2)
        if len(gx) > 5:
            gser.append((g, gx, gy, gk))
            xmax = max(xmax, gx[-1])
    if exp:
        xmax = max(xmax, exp["mean_s"] / 60)

    # altitude panel
    _txt(cv, M, 790, "Altitude (ft)", 20, "Medium", INK2)
    ax3 = _axes_px(fig, M + 70, 800, W - 2 * M - 70, 175)
    _style_axes(ax3)
    a_top = max([top_alt] + [max(gy) for _, _, gy, _ in gser])
    for g, gx, gy, _ in gser:
        ax3.plot(gx, gy, color=GHOST, lw=_lw(3.5), zorder=2)
        rev = g["o_icao"] != o.get("icao")
        ax3.text((gx[0] + gx[-1]) / 2, max(gy) * 1.03,
                 f"{_dmy(g['dep_ts'])[:5]} · {g['o_iata']}→{g['d_iata']}" + (" (volta)" if rev else ""),
                 fontproperties=_F(18), color=INK2, ha="center", va="bottom")
    ax3.plot(mx, my, color=BLUE, lw=_lw(4.5), zorder=4)
    ax3.text((mx[0] + mx[-1]) / 2, max(my) * 0.9, f"{_dmy(dep)[:5]} · este voo",
             fontproperties=_F(18, "SemiBold"), color=INK, ha="center", va="top", zorder=5)
    ax3.plot([mx[-1]], [0], marker="o", ms=_lw(14), mfc=BLUE_DD, mec="white", mew=_lw(3),
             zorder=6, clip_on=False)
    if exp:
        ex = exp["mean_s"] / 60
        ax3.plot([ex, ex], [0, a_top * 1.02], color=INK2, lw=_lw(2), ls=(0, (4, 4)), zorder=3)
        ax3.text(ex + 0.8, a_top * 0.55, f"esperado\n{_dur(exp['mean_s'])}", fontproperties=_F(18),
                 color=INK2, ha="left", va="center", linespacing=1.3)
    ax3.set_ylim(0, a_top * 1.25)
    ax3.set_yticks(_nice_ticks(a_top * 1.05))
    _min_axis(ax3, xmax, labels=False)

    # speed panel — same minutes axis, same previous flights
    _txt(cv, M, 1024, "Velocidade (kt)", 20, "Medium", INK2)
    ax4 = _axes_px(fig, M + 70, 1034, W - 2 * M - 70, 175)
    _style_axes(ax4)

    def airborne_speed(x, k):
        sel = [(a, b) for a, b in zip(x, k) if b >= 50]
        return ([a for a, _ in sel], [b for _, b in sel]) if sel else (x, k)

    v_all = []
    for g, gx, _, gk in gser:
        sx, sk = airborne_speed(gx, gk)
        ax4.plot(sx, sk, color=GHOST, lw=_lw(3.5), zorder=2)
        v_all += sk
        if g.get("cruise_kt"):
            ax4.text((gx[0] + gx[-1]) / 2, max(sk) + 6,
                     f"{_dmy(g['dep_ts'])[:5]} · cruzeiro {_fmt_int_br(g['cruise_kt'])} kt",
                     fontproperties=_F(18), color=INK2, ha="center", va="bottom", zorder=5)
    sx, sk = airborne_speed(mx, mk)
    ax4.plot(sx, sk, color=ORANGE, lw=_lw(4), zorder=4)
    v_all += sk
    cx0, cx1 = (ts[c0] - dep) / 60, (ts[c1] - dep) / 60
    ax4.plot([cx0, cx1], [stats["cruise_kt"]] * 2, color=INK2, lw=_lw(2), ls=(0, (5, 4)), zorder=5)
    ax4.text((cx0 + cx1) / 2, stats["cruise_kt"] - 24,
             f"{_dmy(dep)[:5]} · cruzeiro {_fmt_int_br(stats['cruise_kt'])} kt",
             fontproperties=_F(18, "SemiBold"), color=INK, ha="center", va="top", zorder=6)
    if exp:
        ex = exp["mean_s"] / 60
        ax4.plot([ex, ex], [0, 1e4], color=INK2, lw=_lw(2), ls=(0, (4, 4)), zorder=3)
    v_lo, v_hi = min(v_all), max(v_all)
    ax4.set_ylim(max(0, v_lo * 0.85), v_hi * 1.2)
    ax4.set_yticks([t for t in range(0, int(v_hi * 1.2) + 1, 100) if t >= v_lo * 0.85])
    _min_axis(ax4, xmax, labels=True)

    _txt(cv, W - M, H - 22, ("Relevo: Copernicus DEM via Open-Meteo · " if terr else "")
         + "trajeto: Flightradar24", 17, "Regular", MUTED, ha="right")
    return _save(fig)
