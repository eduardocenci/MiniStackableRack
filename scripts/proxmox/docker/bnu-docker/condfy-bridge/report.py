#!/usr/bin/env python3
"""Weekly presence report — gate passages → presence blocks → JPEG → WhatsApp.

Condfy access events are undirected ("passou por portão …"), so presence is
inferred with the house rule: the person never sleeps at the condo, therefore
each local day's passages alternate entrada/saída starting with an entrada.
A trailing unpaired passage is an entrada whose saída was not recorded (free
exit, gate held open …): it is drawn hatched and excluded from the total.

Pure stdlib except render_image(), which imports Pillow lazily so the offline
test suite runs without it.

>>> fmt_minutes(214)
'3 h 34 min'
>>> fmt_minutes(45)
'45 min'
>>> fmt_minutes(0)
'0 min'
"""
from datetime import date, datetime, timedelta

WEEKDAYS_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
             "seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "dom": 6}

TEAL_FILL = (203, 235, 222)
TEAL_EDGE = (15, 110, 86)
TEAL_TEXT = (4, 52, 44)
INK = (20, 20, 20)
INK_SOFT = (95, 94, 90)
INK_MUTED = (150, 148, 141)
LINE = (225, 224, 217)
CARD = (244, 243, 238)


def fmt_minutes(total_min):
    """Whole minutes → 'H h MM min' / 'M min'."""
    total_min = int(round(total_min))
    h, m = divmod(total_min, 60)
    if h and m:
        return f"{h} h {m} min"
    if h:
        return f"{h} h"
    return f"{m} min"


def parse_dow(text, default=6):
    """Day-of-week name/number → Python weekday (Mon=0). Unknown → default."""
    text = (text or "").strip().lower()
    if text.isdigit():
        n = int(text)
        return n if 0 <= n <= 6 else default
    return DAY_NAMES.get(text[:3], default)


def last_fire(now_local, dow, hour):
    """Most recent scheduled weekly fire time at or before now_local.

    >>> from zoneinfo import ZoneInfo
    >>> tz = ZoneInfo("America/Sao_Paulo")
    >>> last_fire(datetime(2026, 8, 9, 12, 0, tzinfo=tz), 6, 8)
    datetime.datetime(2026, 8, 9, 8, 0, tzinfo=zoneinfo.ZoneInfo(key='America/Sao_Paulo'))
    >>> last_fire(datetime(2026, 8, 9, 7, 59, tzinfo=tz), 6, 8)
    datetime.datetime(2026, 8, 2, 8, 0, tzinfo=zoneinfo.ZoneInfo(key='America/Sao_Paulo'))
    >>> last_fire(datetime(2026, 8, 12, 9, 0, tzinfo=tz), 6, 8)
    datetime.datetime(2026, 8, 9, 8, 0, tzinfo=zoneinfo.ZoneInfo(key='America/Sao_Paulo'))
    """
    days_back = (now_local.weekday() - dow) % 7
    candidate = (now_local - timedelta(days=days_back)).replace(
        hour=hour, minute=0, second=0, microsecond=0)
    if candidate > now_local:
        candidate -= timedelta(days=7)
    return candidate


def week_bounds(fire_local):
    """The reported week: the 7 days ending the day before the fire date.

    >>> week_bounds(datetime(2026, 8, 9, 8, 0))
    (datetime.date(2026, 8, 2), datetime.date(2026, 8, 8))
    """
    end = fire_local.date() - timedelta(days=1)
    return end - timedelta(days=6), end


def pair_day(times):
    """Alternate entrada/saída within one local day.

    Returns (blocks, open_entry): blocks = [(t_in, t_out), …]; open_entry is a
    trailing unpaired entrada or None.

    >>> pair_day([1, 2, 3, 4])
    ([(1, 2), (3, 4)], None)
    >>> pair_day([1, 2, 3])
    ([(1, 2)], 3)
    >>> pair_day([])
    ([], None)
    """
    blocks = [(times[i], times[i + 1]) for i in range(0, len(times) - 1, 2)]
    return blocks, (times[-1] if len(times) % 2 else None)


def week_data(db, person_key, start, end):
    """Presence structure for [start, end] (dates, inclusive) from the events table."""
    rows = db.execute(
        "SELECT ts_local, gate, method FROM events "
        "WHERE type_name='CONTROLE_ACESSOS' AND person_key=? "
        "AND ts_local >= ? AND ts_local < ? ORDER BY ts_utc, id",
        (person_key, start.isoformat(),
         (end + timedelta(days=1)).isoformat()),
    ).fetchall()

    by_day = {}
    for row in rows:
        ts = datetime.fromisoformat(row["ts_local"])
        by_day.setdefault(ts.date(), []).append(
            {"ts": ts, "gate": row["gate"] or "?", "method": row["method"] or ""})

    days, total_min, passages = [], 0, 0
    for offset in range(7):
        day = start + timedelta(days=offset)
        events = by_day.get(day, [])
        blocks, open_entry = pair_day([e["ts"] for e in events])
        minutes = sum((b - a).total_seconds() / 60 for a, b in blocks)
        total_min += minutes
        passages += len(events)
        days.append({"date": day, "events": events, "blocks": blocks,
                     "open_entry": open_entry, "minutes": minutes})

    return {"start": start, "end": end, "days": days,
            "total_min": total_min, "passages": passages,
            "days_present": sum(1 for d in days if d["events"])}


def report_caption(data, display_name):
    start, end = data["start"], data["end"]
    lines = [
        f"🏠 *Céu Azul — presença semanal: {display_name}*",
        f"Semana {start.strftime('%d/%m')} a {end.strftime('%d/%m/%Y')}",
        f"Total: *{fmt_minutes(data['total_min'])}* · "
        f"{data['days_present']} de 7 dias · {data['passages']} passagens de tag",
    ]
    if any(d["open_entry"] for d in data["days"]):
        lines.append("⚠️ Há entrada sem saída registrada — fora do total.")
    lines.append("_Entradas/saídas pareadas por passagem de tag (não pernoita)._")
    return "\n".join(lines)


DAY_START_H = 6
DAY_END_H = 19


def _axis_range(data):
    """Whole-hour axis bounds: the fixed working day, widened only if needed.

    The window is deliberately constant (06:00–19:00) so consecutive weekly
    reports are read against the same scale — a block half-way down the grid
    means the same hour every week. It expands only when a passage falls
    outside it, because clipping a real block would be a lie.

    >>> from datetime import datetime as dt
    >>> mk = lambda *hs: {"days": [{"events": [{"ts": dt(2026, 8, 4, h, m)}
    ...                                        for h, m in hs]}]}
    >>> _axis_range(mk((7, 26), (11, 8)))
    (6, 19)
    >>> _axis_range({"days": []})
    (6, 19)
    >>> _axis_range(mk((5, 40), (21, 15)))
    (5, 22)
    >>> _axis_range(mk((23, 50),))
    (6, 24)
    """
    hours = [e["ts"].hour + e["ts"].minute / 60
             for day in data["days"] for e in day["events"]]
    if not hours:
        return DAY_START_H, DAY_END_H
    lo = min(DAY_START_H, int(min(hours)))
    hi = max(DAY_END_H, min(24, int(max(hours)) + 1))
    return lo, hi


def render_image(data, display_name, out_path):
    """Draw the weekly grid to a JPEG. Imports Pillow lazily."""
    from PIL import Image, ImageDraw, ImageFont

    def font(size, bold=False):
        name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        return ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{name}", size)

    lo, hi = _axis_range(data)
    pph = 58
    width = 1080
    margin = 42
    gutter = 96
    grid_top = 322
    grid_h = (hi - lo) * pph
    foot_h = 120
    height = grid_top + 44 + grid_h + foot_h
    col_w = (width - margin * 2 - gutter) / 7

    img = Image.new("RGB", (width, height), (255, 255, 255))
    d = ImageDraw.Draw(img)

    start, end = data["start"], data["end"]
    d.text((margin, 40), "Presença no condomínio", font=font(40, True), fill=INK)
    d.text((margin, 96),
           f"{display_name} · Céu Azul · semana de {start.strftime('%d/%m')} "
           f"a {end.strftime('%d/%m/%Y')}",
           font=font(24), fill=INK_SOFT)

    cards = [
        ("Total da semana", fmt_minutes(data["total_min"])),
        ("Dias presentes", f"{data['days_present']} de 7"),
        ("Passagens de tag", str(data["passages"])),
    ]
    card_w = (width - margin * 2 - 2 * 20) / 3
    for i, (label, value) in enumerate(cards):
        x = margin + i * (card_w + 20)
        d.rounded_rectangle([x, 150, x + card_w, 262], radius=14, fill=CARD)
        d.text((x + 24, 172), label, font=font(20), fill=INK_SOFT)
        d.text((x + 24, 204), value, font=font(36, True), fill=INK)

    grid_left = margin + gutter
    for offset, day in enumerate(data["days"]):
        x = grid_left + offset * col_w
        name = WEEKDAYS_PT[day["date"].weekday()]
        shade = INK_MUTED if day["date"].weekday() >= 5 else (
            INK if day["events"] else INK_SOFT)
        label = f"{name} {day['date'].strftime('%d')}"
        w = d.textlength(label, font=font(22, True))
        d.text((x + (col_w - w) / 2, grid_top - 36), label,
               font=font(22, True), fill=shade)

    for h in range(lo, hi + 1):
        y = grid_top + (h - lo) * pph
        d.line([grid_left, y, width - margin, y], fill=LINE, width=1)
        d.text((grid_left - 14 - d.textlength(f"{h:02d}:00", font=font(18)), y - 10),
               f"{h:02d}:00", font=font(18), fill=INK_MUTED)
    for offset in range(8):
        x = grid_left + offset * col_w
        d.line([x, grid_top, x, grid_top + grid_h], fill=LINE, width=1)

    def y_of(ts):
        return grid_top + (ts.hour + ts.minute / 60 + ts.second / 3600 - lo) * pph

    for offset, day in enumerate(data["days"]):
        x0 = grid_left + offset * col_w + 7
        x1 = grid_left + (offset + 1) * col_w - 7
        xm = (x0 + x1) / 2
        if not day["events"]:
            d.text((xm - d.textlength("—", font=font(24)) / 2,
                    grid_top + grid_h / 2 - 14), "—", font=font(24), fill=INK_MUTED)
            continue
        for t_in, t_out in day["blocks"]:
            y0, y1 = y_of(t_in), max(y_of(t_out), y_of(t_in) + 7)
            d.rounded_rectangle([x0, y0, x1, y1], radius=7,
                                fill=TEAL_FILL, outline=TEAL_EDGE, width=2)
            mins = (t_out - t_in).total_seconds() / 60
            label_in = t_in.strftime("%H:%M")
            label_out = t_out.strftime("%H:%M")
            if y1 - y0 >= 108:
                for text, ty, bold in [
                        (label_in, y0 + 8, False),
                        (fmt_minutes(mins), (y0 + y1) / 2 - 14, True),
                        (label_out, y1 - 30, False)]:
                    w = d.textlength(text, font=font(21, bold))
                    d.text((xm - w / 2, ty), text, font=font(21, bold), fill=TEAL_TEXT)
            else:
                text = f"{label_in}–{label_out} · {fmt_minutes(mins)}"
                w = d.textlength(text, font=font(18))
                ty = y0 - 26 if y0 - 26 > grid_top else y1 + 6
                d.text((xm - w / 2, ty), text, font=font(18), fill=TEAL_TEXT)
        if day["open_entry"]:
            entry = day["open_entry"]
            y0 = y_of(entry)
            d.rounded_rectangle([x0, y0, x1, y0 + 26], radius=7, outline=TEAL_EDGE, width=2)
            text = f"{entry.strftime('%H:%M')} · saída não registrada"
            w = d.textlength(text, font=font(17))
            d.text((xm - w / 2, y0 + 32), text, font=font(17), fill=INK_SOFT)

    foot_y = grid_top + grid_h + 34
    d.text((margin, foot_y),
           "Fonte: registros de acesso Condfy (tag) · entradas/saídas pareadas por dia",
           font=font(19), fill=INK_MUTED)
    d.text((margin, foot_y + 30),
           "— assume-se que não pernoita no condomínio. Dias sem barra: sem passagem de tag.",
           font=font(19), fill=INK_MUTED)

    img.save(out_path, "JPEG", quality=92)
    return out_path
