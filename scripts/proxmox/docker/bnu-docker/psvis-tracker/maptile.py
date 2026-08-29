"""Render the flight path over OpenStreetMap tiles (no heavy GIS deps).

Standard slippy-map math; tiles are disk-cached under DATA_DIR/tiles both for
speed and out of politeness to the OSM tile servers (a flight needs ~6-12
tiles). Attribution is drawn on the image as OSM's policy requires.
"""
import logging
import math
import os

import requests
from PIL import Image, ImageDraw

log = logging.getLogger("psvis.map")

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
HEADERS = {"User-Agent": "psvis-tracker/1.0 (+MiniStackableRack home automation)"}
TILE_CACHE = os.path.join(os.environ.get("DATA_DIR", "/data"), "tiles")

BLUE = (42, 120, 214)      # dataviz categorical slot 1 — the path
INK = (11, 11, 11)


def _lonlat_to_px(lon, lat, zoom):
    """Web-mercator world pixel coordinates at `zoom`."""
    scale = 256 * (2 ** zoom)
    x = (lon + 180) / 360 * scale
    lat_r = math.radians(lat)
    y = (1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * scale
    return x, y


def _get_tile(z, x, y):
    n = 2 ** z
    x, y = x % n, y % n
    path = os.path.join(TILE_CACHE, f"{z}_{x}_{y}.png")
    if not os.path.exists(path):
        os.makedirs(TILE_CACHE, exist_ok=True)
        r = requests.get(TILE_URL.format(z=z, x=x, y=y), headers=HEADERS, timeout=15)
        r.raise_for_status()
        with open(path, "wb") as fh:
            fh.write(r.content)
    return Image.open(path).convert("RGB")


# Airplane silhouette, nose pointing up (heading 0°=N), in local px coords.
_PLANE = [
    (0, -16), (2, -10), (3, -5), (15, 3), (15, 7), (3, 4), (2, 10),
    (8, 15), (8, 18), (0, 14), (-8, 18), (-8, 15), (-2, 10), (-3, 4),
    (-15, 7), (-15, 3), (-3, -5), (-2, -10),
]


def _plane_points(cx, cy, heading_deg, scale=1.0):
    th = math.radians(heading_deg)
    c, s = math.cos(th), math.sin(th)
    return [
        (cx + (x * c - y * s) * scale, cy + (x * s + y * c) * scale)
        for x, y in _PLANE
    ]


def render_path(track, width=1200, height=520, plane_heading=None):
    """PIL Image with the track drawn over OSM tiles, or raises.

    With `plane_heading` (degrees) the current position is drawn as an
    airplane icon rotated to the flight heading instead of the end dot —
    used by the en-route update, where the flight is still moving."""
    lons = [p["longitude"] for p in track]
    lats = [p["latitude"] for p in track]
    pad = 0.12
    dlon = (max(lons) - min(lons)) or 0.01
    dlat = (max(lats) - min(lats)) or 0.01
    lon0, lon1 = min(lons) - pad * dlon, max(lons) + pad * dlon
    lat0, lat1 = min(lats) - pad * dlat, max(lats) + pad * dlat

    zoom = 12
    while zoom > 2:
        x0, y0 = _lonlat_to_px(lon0, lat1, zoom)
        x1, y1 = _lonlat_to_px(lon1, lat0, zoom)
        if (x1 - x0) <= width and (y1 - y0) <= height:
            break
        zoom -= 1

    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left, top = cx - width / 2, cy - height / 2

    img = Image.new("RGB", (width, height), (221, 221, 221))
    tx0, ty0 = int(left // 256), int(top // 256)
    tx1, ty1 = int((left + width) // 256), int((top + height) // 256)
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            try:
                tile = _get_tile(zoom, tx, ty)
                img.paste(tile, (int(tx * 256 - left), int(ty * 256 - top)))
            except Exception as exc:  # noqa: BLE001 — a hole beats no map
                log.warning("tile %s/%s/%s failed: %s", zoom, tx, ty, exc)

    draw = ImageDraw.Draw(img)
    pts = []
    for lon, lat in zip(lons, lats):
        x, y = _lonlat_to_px(lon, lat, zoom)
        pts.append((x - left, y - top))
    draw.line(pts, fill=BLUE, width=4, joint="curve")
    if plane_heading is None:
        for p, fill in ((pts[0], (255, 255, 255)), (pts[-1], BLUE)):
            draw.ellipse([p[0] - 7, p[1] - 7, p[0] + 7, p[1] + 7], fill=fill, outline=INK, width=2)
    else:
        p = pts[0]
        draw.ellipse([p[0] - 7, p[1] - 7, p[0] + 7, p[1] + 7],
                     fill=(255, 255, 255), outline=INK, width=2)
        cx, cy = pts[-1]
        # white halo behind for contrast on any tile, then the blue aircraft
        draw.polygon(_plane_points(cx, cy, plane_heading, 1.55), fill=(255, 255, 255))
        draw.polygon(_plane_points(cx, cy, plane_heading, 1.25), fill=BLUE, outline=INK)

    draw.text((width - 8, height - 6), "© OpenStreetMap", fill=INK, anchor="rs")
    return img
