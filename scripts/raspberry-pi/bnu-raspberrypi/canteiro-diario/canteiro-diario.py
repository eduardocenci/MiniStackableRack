#!/usr/bin/env python3
"""canteiro-diario — Diário de Obra ARA: evidence pack, publish (WhatsApp + print), print-only.

Three sub-commands, one script (decisão Eduardo 09/09/2026, "execute, deploy"):

  collect [--date D] [--force]   Stage 1 (bnu-raspberrypi, Mon–Fri 20:15 BRT). Builds the
                                 evidence pack of day D and uploads it to Drive
                                 CeuAzul/Diario/<D>/pack/ with public links + manifest.json
                                 (and CeuAzul/Diario/manifest-latest.json, stable link).
  publish [--date D] [--test]    Stage 3 (bnu-raspberrypi, Mon–Fri every 10 min 20:50→23:50).
                                 Waits for Diario/<D>/diario.json (written by the Claude Code
                                 cloud routine — stage 2), renders resumo/completo (Chromium),
                                 uploads the outputs, sends the JPG to the WhatsApp group and
                                 prints the PDF on the BNU printer. Idempotent via sent.json.
  print [--date D]               Print-only role (mia-raspberrypi): prints Diario/<D>/resumo.pdf
                                 once (marker printed-<host>.json).

Pack layout (Drive CeuAzul/Diario/<D>/pack/, all files public "anyone with the link"):
  manifest.json      counts, first/last person event, file index {name: {id, url}}
  events.json        Frigate events of the day (id, start/end BRT, label, score, box, sub_label)
  hist15.json        person/car events per 15 min
  presence.json      Wi-Fi presence: day 06:00–19:00, previous night 20:00→05:00 (strict), arrival
  tags.json          condfy gate passes of the day (via bnu-proxmox → LXC 101), or null
  plan.json          PlanejadoRealizado row of the week (planned / realized)
  whatsapp.md/.json  obra-group messages of the day (waha-listener archive)
  sheets/hour_HH.jpg contact sheets (6 cols) of every Frigate snapshot, per hour
  snaps.zip          every event snapshot (snaps/<id>.jpg, 640×480) in one object — Drive
                     creates files one API call at a time, ~500 loose files took 20 min
  vehicles_sheet.jpg / people_sheet.jpg   quick-scan sheets (car events ≥20 s / person close-ups)
  frames/<D>_HHMM.jpg (1152×648) + frames_sheet.jpg   timelapse frames of the day
  montage.jpg        Timelapse/DiaDeTrabalho/<D>.jpg (ontem × hoje, 3 posições) when present
  sunset_pos2.jpg / sunset_pos3.jpg   calibrated sunset frames (same framing every day)

Env (env/canteiro-diario.env — see canteiro-diario.env.example). Failures (rc≠0) are
posted to ALERT_JID (Casa SmokeTests) — regra Eduardo 04/09/2026.
"""
from __future__ import annotations

import argparse
import base64
import collections
import concurrent.futures as cf
import datetime as dt
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")
FAILURES: list[str] = []

# ---------------------------------------------------------------- config
E = os.environ.get
WAHA_URL = E("WAHA_URL", "http://10.1.1.126:3000")
WAHA_KEY = E("WAHA_KEY", "")
WAHA_SESSION = E("WAHA_SESSION", "default")
GROUP_JID = E("GROUP_JID", "")
TEST_JID = E("TEST_JID", GROUP_JID)
ALERT_JID = E("ALERT_JID", TEST_JID)
FRIGATE_URL = E("FRIGATE_URL", "http://bnu-frigate:5000")
FRIGATE_CAMERA = E("FRIGATE_CAMERA", "canteiro")
ARA_NTO_URL = E("ARA_NTO_URL", "http://ara-raspberrypi:5000")
LISTENER_URL = E("LISTENER_URL", "")
LISTENER_TOKEN = E("LISTENER_TOKEN", "")
OBRA_CHAT_JID = E("OBRA_CHAT_JID", "")
SHEET_ID = E("SHEET_ID", "")
SHEET_TAB = E("SHEET_TAB", "PlanejadoRealizado")
GSA_KEYFILE = E("GSA_KEYFILE", "/config/gsa.json")
PROXMOX_HOST = E("PROXMOX_HOST", "bnu-proxmox")
PROXMOX_USER = E("PROXMOX_USER", "root")
PROXMOX_PW = E("PROXMOX_PW", "")
CONDFY_LXC = E("CONDFY_LXC", "101")
RCLONE_REMOTE = E("RCLONE_REMOTE", "ceuazul:").rstrip("/")
DIARIO_DIR = E("DIARIO_DIR", "Diario")
TIMELAPSE_DIR = E("TIMELAPSE_DIR", "Timelapse")
PRINTER = E("PRINTER", "")
PRINT_OPTIONS = E("PRINT_OPTIONS", "-o media=A4")
STATE_DIR = Path(E("STATE_DIR", "/var/lib/canteiro-diario"))
GITHUB_RAW_JSON = E("GITHUB_RAW_JSON", "")   # optional fallback: raw URL pattern with {date}
GITHUB_TOKEN = E("GITHUB_TOKEN", "")
FIXED_DEVICE_NAMES = {s.strip().lower() for s in
                      E("FIXED_DEVICE_NAMES", "Roteador Starlink,Câmera do canteiro (iM9),ara-raspberrypi").split(",")}
WORKDAY_START, WORKDAY_END = 6, 19       # presence day window (BRT hours)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def fail(msg: str) -> None:
    print("FAIL: " + msg, file=sys.stderr)
    FAILURES.append(msg)


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)


def rc_remote(*parts: str) -> str:
    return RCLONE_REMOTE + "/" + "/".join(p.strip("/") for p in parts if p)


def run(cmd: list[str], timeout: int = 600, check: bool = False) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd[:3])}… rc={r.returncode}: {(r.stderr or r.stdout)[-300:]}")
    return r


# ---------------------------------------------------------------- WhatsApp (WAHA)
def waha_post(endpoint: str, payload: dict, timeout: int = 120) -> int:
    req = urllib.request.Request(f"{WAHA_URL}/api/{endpoint}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "X-Api-Key": WAHA_KEY})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def send_text(chat: str, text: str) -> int:
    return waha_post("sendText", {"session": WAHA_SESSION, "chatId": chat, "text": text})


def send_image(chat: str, path: Path, caption: str) -> int:
    b64 = base64.b64encode(path.read_bytes()).decode()
    return waha_post("sendImage", {"session": WAHA_SESSION, "chatId": chat,
                                   "file": {"mimetype": "image/jpeg", "filename": path.name, "data": b64},
                                   "caption": caption})


def send_alert(text: str) -> None:
    """Best-effort: never let the alert itself break the run."""
    if not (WAHA_URL and WAHA_KEY and ALERT_JID):
        print("alerta nao configurado: " + text.replace("\n", " | "), file=sys.stderr)
        return
    try:
        send_text(ALERT_JID, text)
    except Exception as e:  # noqa: BLE001
        print(f"alerta falhou: {e}", file=sys.stderr)


# ---------------------------------------------------------------- helpers
def day_bounds(d: dt.date) -> tuple[int, int]:
    a = dt.datetime(d.year, d.month, d.day, tzinfo=TZ)
    return int(a.timestamp()), int((a + dt.timedelta(days=1)).timestamp())


def http_json(url: str, timeout: int = 60, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def http_bytes(url: str, timeout: int = 60) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def pil():
    from PIL import Image, ImageDraw, ImageFont  # noqa: WPS433
    try:
        font = ImageFont.truetype(FONT, 22)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()
    return Image, ImageDraw, font


def contact_sheet(items: list[tuple[str, Path | bytes, bool]], out: Path, cols: int = 6,
                  tw: int = 320, th: int = 240, quality: int = 80) -> None:
    """items: (label, image path or bytes, highlight). Writes a JPEG contact sheet."""
    Image, ImageDraw, font = pil()
    rows = max(1, (len(items) + cols - 1) // cols)
    sheet = Image.new("RGB", (cols * tw, rows * th), "black")
    dr = ImageDraw.Draw(sheet)
    for i, (label, src, hl) in enumerate(items):
        try:
            im = Image.open(io.BytesIO(src) if isinstance(src, bytes) else src).convert("RGB").resize((tw, th))
        except Exception:  # noqa: BLE001
            im = Image.new("RGB", (tw, th), "gray")
        x, y = (i % cols) * tw, (i // cols) * th
        sheet.paste(im, (x, y))
        dr.rectangle([x, y, x + min(tw, len(label) * 12 + 6), y + 26], fill=(0, 0, 0))
        dr.text((x + 3, y + 1), label, fill=(255, 255, 0) if hl else (255, 255, 255), font=font)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=quality)


# ---------------------------------------------------------------- stage 1: collect
def collect_frigate(d: dt.date, pack: Path) -> dict:
    a, b = day_bounds(d)
    ev = http_json(f"{FRIGATE_URL}/api/events?camera={FRIGATE_CAMERA}&after={a}&before={b}&limit=5000", 120)
    ev.sort(key=lambda e: e["start_time"])
    slim = []
    for e in ev:
        st = dt.datetime.fromtimestamp(e["start_time"], TZ)
        en = dt.datetime.fromtimestamp(e["end_time"], TZ) if e.get("end_time") else None
        slim.append({"id": e["id"], "start": st.strftime("%H:%M:%S"), "end": en.strftime("%H:%M:%S") if en else None,
                     "dur_s": round((e["end_time"] or e["start_time"]) - e["start_time"]),
                     "label": e["label"], "score": round(float(e.get("data", {}).get("top_score") or e.get("top_score") or 0), 2),
                     "box": [round(v, 3) for v in (e.get("data", {}).get("box") or [])],
                     "sub_label": e.get("sub_label")})
    (pack / "events.json").write_text(json.dumps(slim, ensure_ascii=False, indent=0), encoding="utf-8")
    # 15-min histogram
    bins: dict[int, dict] = collections.defaultdict(lambda: {"person": 0, "car": 0, "other": 0})
    for e in ev:
        st = dt.datetime.fromtimestamp(e["start_time"], TZ)
        k = st.hour * 60 + (st.minute // 15) * 15
        bins[k][e["label"] if e["label"] in ("person", "car") else "other"] += 1
    hist = [{"t": f"{k // 60:02d}:{k % 60:02d}", "min": k, **v} for k, v in sorted(bins.items())]
    (pack / "hist15.json").write_text(json.dumps(hist), encoding="utf-8")
    # snapshots
    snaps = pack / "snaps"
    snaps.mkdir(parents=True, exist_ok=True)

    def get(e):
        p = snaps / f"{e['id']}.jpg"
        try:
            p.write_bytes(http_bytes(f"{FRIGATE_URL}/api/events/{e['id']}/snapshot.jpg?quality=80", 90))
            return True
        except Exception as ex:  # noqa: BLE001
            print(f"snapshot {e['id']}: {ex}", file=sys.stderr)
            return False
    with cf.ThreadPoolExecutor(6) as ex:
        ok = sum(ex.map(get, ev))
    log(f"frigate: {len(ev)} events, {ok} snapshots")
    # hourly contact sheets
    byhour: dict[int, list] = collections.defaultdict(list)
    for e in ev:
        byhour[dt.datetime.fromtimestamp(e["start_time"], TZ).hour].append(e)
    for hr, es in sorted(byhour.items()):
        items = []
        for e in es:
            st = dt.datetime.fromtimestamp(e["start_time"], TZ)
            dur = (e["end_time"] or e["start_time"]) - e["start_time"]
            items.append((f"{st:%H:%M:%S} {e['label'][:3]} {dur:.0f}s", snaps / f"{e['id']}.jpg", e["label"] == "car"))
        contact_sheet(items, pack / "sheets" / f"hour_{hr:02d}.jpg")
    # vehicles (car events >= 20 s) and people close-ups (tall boxes)
    veh = [e for e in ev if e["label"] == "car" and ((e["end_time"] or e["start_time"]) - e["start_time"]) >= 20]
    if veh:
        contact_sheet([(f"{dt.datetime.fromtimestamp(e['start_time'], TZ):%H:%M:%S} car {(e['end_time'] or e['start_time']) - e['start_time']:.0f}s",
                        snaps / f"{e['id']}.jpg", True) for e in veh], pack / "vehicles_sheet.jpg", cols=4, tw=400, th=300)
    ppl = [e for e in ev if e["label"] == "person" and (e.get("data", {}).get("box") or [0, 0, 0, 0])[3] >= 0.45]
    if ppl:
        contact_sheet([(f"{dt.datetime.fromtimestamp(e['start_time'], TZ):%H:%M:%S} per", snaps / f"{e['id']}.jpg", False)
                       for e in ppl[:96]], pack / "people_sheet.jpg", cols=6, tw=320, th=240)
    # one object instead of ~500: Drive creates files one API call at a time (~0.4 files/s observed)
    import zipfile  # noqa: WPS433
    with zipfile.ZipFile(pack / "snaps.zip", "w", zipfile.ZIP_STORED) as zf:
        for p in sorted(snaps.glob("*.jpg")):
            zf.write(p, f"snaps/{p.name}")
    shutil.rmtree(snaps, ignore_errors=True)
    persons = [e for e in ev if e["label"] == "person"]
    return {"events": len(ev), "person_events": len(persons), "car_events": sum(e["label"] == "car" for e in ev),
            "first_person": dt.datetime.fromtimestamp(persons[0]["start_time"], TZ).strftime("%H:%M") if persons else None,
            "last_person": dt.datetime.fromtimestamp(persons[-1]["start_time"], TZ).strftime("%H:%M") if persons else None,
            "snapshots": ok, "vehicle_events_20s": len(veh)}


def presence_window(f: dt.datetime, t: dt.datetime) -> list[dict]:
    fu, tu = f.astimezone(dt.timezone.utc), t.astimezone(dt.timezone.utc)
    d = http_json(f"{ARA_NTO_URL}/api/presence?from={fu:%Y-%m-%dT%H:%M:%SZ}&to={tu:%Y-%m-%dT%H:%M:%SZ}", 60)
    out = []
    for x in d.get("devices", []):
        name = (x.get("display_name") or x.get("hostname") or x.get("ip") or "")
        if name.lower() in FIXED_DEVICE_NAMES or (x.get("hostname") or "").lower().startswith("ara-raspberrypi"):
            continue
        def brt(s):
            try:
                return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ).strftime("%H:%M")
            except Exception:  # noqa: BLE001
                return s
        out.append({"name": name, "ip": x.get("ip"), "mac": x.get("mac"), "first_seen": brt(x.get("first_seen_window_utc", "")),
                    "last_seen": brt(x.get("last_seen_window_utc", "")), "present_at_start": x.get("present_at_window_start"),
                    "transitions": x.get("transitions")})
    return out


def collect_presence(d: dt.date, pack: Path) -> dict:
    day0 = dt.datetime(d.year, d.month, d.day, tzinfo=TZ)
    res = {"note": ("last_seen is clamped to the window end: a device seen at 16:00 and again at 06:30 next day "
                    "shows last_seen=window end. Use the strict night window for absence; never infer lodging."),
           "day_06_19": presence_window(day0 + dt.timedelta(hours=WORKDAY_START), day0 + dt.timedelta(hours=WORKDAY_END)),
           "prev_night_20_05": presence_window(day0 - dt.timedelta(hours=4), day0 + dt.timedelta(hours=5)),
           "arrival_05_08": presence_window(day0 + dt.timedelta(hours=5), day0 + dt.timedelta(hours=8))}
    (pack / "presence.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"devices_day": len(res["day_06_19"])}


CONDFY_QUERY = """
import sqlite3, json
c = sqlite3.connect("file:/data/condfy.db?mode=ro", uri=True)
rows = [dict(zip(("ts_local","person","gate","method"), r)) for r in c.execute(
    "select ts_local, person, gate, method from events where ts_local >= '{d0}' and ts_local < '{d1}' order by ts_utc")]
print("TAGS_JSON=" + json.dumps(rows, ensure_ascii=False))
"""


def collect_tags(d: dt.date, pack: Path) -> dict:
    """Gate passes via bnu-proxmox → pct exec <LXC> → docker exec condfy-bridge (paramiko, password).
    The dates are baked into the base64 payload so the remote command carries no nested quotes."""
    if not PROXMOX_PW:
        (pack / "tags.json").write_text("null", encoding="utf-8")
        return {"tags": None, "reason": "PROXMOX_PW not set"}
    try:
        import paramiko  # noqa: WPS433
        d1 = d + dt.timedelta(days=1)
        b64 = base64.b64encode(CONDFY_QUERY.format(d0=d.isoformat(), d1=d1.isoformat()).encode()).decode()
        cmd = (f"pct exec {CONDFY_LXC} -- docker exec condfy-bridge python3 -c "
               f"\"import base64;exec(base64.b64decode('{b64}').decode())\"")
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cli.connect(PROXMOX_HOST, username=PROXMOX_USER, password=PROXMOX_PW, timeout=30, allow_agent=False, look_for_keys=False)
        _, out, err = cli.exec_command(cmd, timeout=90)
        txt = out.read().decode(); e = err.read().decode(); cli.close()
        line = next((ln for ln in txt.splitlines() if ln.startswith("TAGS_JSON=")), None)
        if line is None:
            raise RuntimeError(f"no TAGS_JSON in output; stderr: {e.strip()[-160:]}")
        rows = json.loads(line[len("TAGS_JSON="):])
        (pack / "tags.json").write_text(json.dumps(rows, ensure_ascii=False, indent=0), encoding="utf-8")
        return {"tags": len(rows)}
    except Exception as ex:  # noqa: BLE001
        print(f"tags: {ex}", file=sys.stderr)
        (pack / "tags.json").write_text("null", encoding="utf-8")
        return {"tags": None, "reason": str(ex)[:120]}


MONTHS_PT = {"janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
             "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}


def collect_plan(d: dt.date, pack: Path) -> dict:
    """PlanejadoRealizado row whose date range contains d. Week 1 = 6–12 Jul 2026; ranges carry no year."""
    if not SHEET_ID or not Path(GSA_KEYFILE).exists():
        (pack / "plan.json").write_text("null", encoding="utf-8")
        return {"plan": None, "reason": "sheet not configured"}
    try:
        import gspread  # noqa: WPS433
        gc = gspread.service_account(filename=GSA_KEYFILE)
        rows = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB).get_all_values()
        year = 2026
        prev_month = 0
        weeks = []
        for r in rows[2:]:
            if len(r) < 2 or not r[0].strip().isdigit():
                continue
            m = re.match(r"\s*(\d+)\s+(\w+)\s*-\s*(\d+)\s+(\w+)", r[1].strip(), re.I)
            if not m:
                continue
            d1, m1, d2, m2 = int(m.group(1)), MONTHS_PT.get(m.group(2).lower(), 0), int(m.group(3)), MONTHS_PT.get(m.group(4).lower(), 0)
            if m1 and m1 < prev_month:
                year += 1
            prev_month = m1 or prev_month
            y2 = year + 1 if (m2 and m2 < m1) else year
            start, end = dt.date(year, m1, d1), dt.date(y2, m2, d2)
            weeks.append({"week": int(r[0]), "range": r[1].strip(), "start": start.isoformat(), "end": end.isoformat(),
                          "planned": r[2] if len(r) > 2 else "", "realized": r[3] if len(r) > 3 else ""})
        found = None
        for i, w in enumerate(weeks):
            if w["start"] <= d.isoformat() <= w["end"]:
                found = dict(w)
                # next week's row → Friday "Resumo da semana" / "Plano da próxima semana"; null while Ênio has not filled it
                nxt = weeks[i + 1] if i + 1 < len(weeks) else None
                found["next"] = ({k: nxt[k] for k in ("week", "range", "start", "end", "planned")}
                                 if nxt and nxt["planned"].strip() else None)
                break
        (pack / "plan.json").write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"plan_week": found["week"] if found else None}
    except Exception as ex:  # noqa: BLE001
        print(f"plan: {ex}", file=sys.stderr)
        (pack / "plan.json").write_text("null", encoding="utf-8")
        return {"plan": None, "reason": str(ex)[:120]}


def collect_whatsapp(d: dt.date, pack: Path) -> dict:
    if not (LISTENER_URL and LISTENER_TOKEN and OBRA_CHAT_JID):
        (pack / "whatsapp.json").write_text("null", encoding="utf-8")
        return {"whatsapp": None, "reason": "listener not configured"}
    try:
        a, b = day_bounds(d)
        q = urllib.parse.urlencode({"chat": OBRA_CHAT_JID, "since": a, "limit": 500})
        msgs = http_json(f"{LISTENER_URL.rstrip('/')}/messages?{q}", 60, {"X-Listener-Token": LISTENER_TOKEN})
        if isinstance(msgs, dict):
            msgs = msgs.get("messages") or msgs.get("items") or []
        keep = []
        for m in msgs:
            ts = m.get("timestamp") or m.get("ts") or m.get("t") or 0
            ts = float(ts)
            if ts > 10**12:
                ts /= 1000
            if not (a <= ts < b):
                continue
            keep.append({"time": dt.datetime.fromtimestamp(ts, TZ).strftime("%H:%M"),
                         "author": m.get("author_name") or m.get("pushName") or m.get("author") or m.get("from") or "?",
                         "text": m.get("body") or m.get("text") or m.get("caption") or "",
                         "media": m.get("media_name") or m.get("filename") or (m.get("media") or {}).get("filename") if isinstance(m.get("media"), dict) else m.get("media_name")})
        (pack / "whatsapp.json").write_text(json.dumps(keep, ensure_ascii=False, indent=0), encoding="utf-8")
        lines = [f"# WhatsApp — grupo da obra — {d.isoformat()} (horários Brasília)", ""]
        for m in keep:
            lines.append(f"- **{m['time']} {m['author']}**: {m['text']}" + (f" 📎[{m['media']}]" if m.get("media") else ""))
        (pack / "whatsapp.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"whatsapp": len(keep)}
    except Exception as ex:  # noqa: BLE001
        print(f"whatsapp: {ex}", file=sys.stderr)
        (pack / "whatsapp.json").write_text("null", encoding="utf-8")
        return {"whatsapp": None, "reason": str(ex)[:120]}


def collect_timelapse(d: dt.date, pack: Path, tmp: Path) -> dict:
    Image, ImageDraw, font = pil()
    src = tmp / "tl"; src.mkdir(parents=True, exist_ok=True)
    r = run(["rclone", "copy", rc_remote(TIMELAPSE_DIR, "trabalho", f"{d:%Y-%m}"), str(src),
             "--include", f"{d.isoformat()}_*.jpg", "--transfers", "8"], timeout=900)
    if r.returncode:
        fail(f"rclone timelapse trabalho rc={r.returncode}: {r.stderr[-200:]}")
    frames = sorted(src.glob(f"{d.isoformat()}_*.jpg"))
    out = pack / "frames"; out.mkdir(parents=True, exist_ok=True)
    items = []
    for f in frames:
        im = Image.open(f).convert("RGB")
        im.resize((1152, 648)).save(out / f.name, quality=80)
        items.append((f.name[11:13] + ":" + f.name[13:15], f, False))
    if items:
        contact_sheet(items, pack / "frames_sheet.jpg", cols=5, tw=480, th=270, quality=82)
    # montage (posted to the group at ~20:12) and calibrated sunset frames
    got_montage = False
    r = run(["rclone", "copyto", rc_remote(TIMELAPSE_DIR, "DiaDeTrabalho", f"{d.isoformat()}.jpg"), str(tmp / "montage.jpg")], timeout=300)
    if r.returncode == 0 and (tmp / "montage.jpg").exists():
        shutil.copy(tmp / "montage.jpg", pack / "montage.jpg"); got_montage = True
    for pos in ("posicao2", "posicao3"):
        dst = tmp / f"sun_{pos}"; dst.mkdir(exist_ok=True)
        r = run(["rclone", "copy", rc_remote(TIMELAPSE_DIR, pos, "por-do-sol"), str(dst), "--include", f"{d.isoformat()}_*.jpg"], timeout=300)
        fs = sorted(dst.glob("*.jpg"))
        if fs:
            Image.open(fs[0]).convert("RGB").resize((1600, 900)).save(pack / f"sunset_{pos}.jpg", quality=82)
    return {"frames": len(frames), "montage": got_montage}


def drive_publish_pack(d: dt.date, pack: Path, tmp: Path) -> dict:
    """Upload pack/, make it public (anyone with the link), index file ids → manifest links."""
    remote_pack = rc_remote(DIARIO_DIR, d.isoformat(), "pack")
    run(["rclone", "copy", str(pack), remote_pack, "--transfers", "8", "--checkers", "8"], timeout=1800, check=True)
    r = run(["rclone", "link", remote_pack], timeout=120)
    folder_link = r.stdout.strip() if r.returncode == 0 else ""
    if r.returncode:
        fail(f"rclone link pack rc={r.returncode}: {r.stderr[-200:]}")
    r = run(["rclone", "lsjson", "-R", "--files-only", remote_pack], timeout=300, check=True)
    files = {}
    for x in json.loads(r.stdout):
        fid = x.get("ID")
        files[x["Path"]] = {"id": fid, "size": x.get("Size"),
                            "url": f"https://drive.google.com/uc?export=download&id={fid}" if fid else None}
    r = run(["rclone", "lsjson", "--dirs-only", rc_remote(DIARIO_DIR, d.isoformat())], timeout=120)
    return {"folder_link": folder_link, "files": files,
            "pack_folder_id": next((x.get("ID") for x in (json.loads(r.stdout) if r.returncode == 0 else []) if x.get("Path") == "pack"), None)}


def day_folder_id(d: dt.date) -> str | None:
    r = run(["rclone", "lsjson", "--dirs-only", rc_remote(DIARIO_DIR)], timeout=120)
    if r.returncode:
        return None
    return next((x.get("ID") for x in json.loads(r.stdout) if x.get("Path") == d.isoformat()), None)


def cmd_collect(d: dt.date, force: bool) -> int:
    if d.weekday() >= 5 and not force:
        log(f"{d} é fim de semana — sem diário (use --force)."); return 0
    tmp = Path(tempfile.mkdtemp(prefix="diario-"))
    pack = tmp / "pack"; pack.mkdir()
    manifest = {"date": d.isoformat(), "weekday": ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"][d.weekday()] + "-feira" if d.weekday() < 5 else ["sábado", "domingo"][d.weekday() - 5],
                "generated_at": dt.datetime.now(TZ).isoformat(timespec="seconds"), "host": socket.gethostname(), "sources": {}}
    for name, fn in (("frigate", lambda: collect_frigate(d, pack)), ("presence", lambda: collect_presence(d, pack)),
                     ("tags", lambda: collect_tags(d, pack)), ("plan", lambda: collect_plan(d, pack)),
                     ("whatsapp", lambda: collect_whatsapp(d, pack)), ("timelapse", lambda: collect_timelapse(d, pack, tmp))):
        try:
            manifest["sources"][name] = fn(); log(f"{name}: {manifest['sources'][name]}")
        except Exception as ex:  # noqa: BLE001
            fail(f"{name}: {ex}"); manifest["sources"][name] = {"error": str(ex)[:200]}
    if manifest["sources"].get("frigate", {}).get("events", 0) == 0:
        fail("frigate: 0 eventos no dia (câmera/Frigate fora?) — pack gerado mesmo assim")
    # upload + links
    try:
        pub = drive_publish_pack(d, pack, tmp)
        manifest.update({"pack_folder_link": pub["folder_link"], "pack_folder_id": pub["pack_folder_id"], "files": pub["files"]})
        manifest["day_folder_id"] = day_folder_id(d)
        mpath = tmp / "manifest.json"
        mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        run(["rclone", "copyto", str(mpath), rc_remote(DIARIO_DIR, d.isoformat(), "manifest.json")], timeout=120, check=True)
        run(["rclone", "copyto", str(mpath), rc_remote(DIARIO_DIR, "manifest-latest.json")], timeout=120, check=True)
        links = {}
        for p in (rc_remote(DIARIO_DIR, d.isoformat(), "manifest.json"), rc_remote(DIARIO_DIR, "manifest-latest.json")):
            r = run(["rclone", "link", p], timeout=120)
            links[p] = r.stdout.strip() if r.returncode == 0 else f"ERR {r.stderr[-100:]}"
        log("manifest links: " + json.dumps(links))
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / f"collect-{d.isoformat()}.json").write_text(json.dumps({"links": links, "sources": manifest["sources"]}, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as ex:  # noqa: BLE001
        fail(f"upload do pack: {ex}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAILURES else 0


# ---------------------------------------------------------------- stage 3: publish / print
def drive_get(remote_path: str, dest: Path, timeout: int = 300) -> bool:
    r = run(["rclone", "copyto", remote_path, str(dest)], timeout=timeout)
    return r.returncode == 0 and dest.exists()


def drive_exists(remote_path: str) -> bool:
    r = run(["rclone", "lsjson", remote_path], timeout=120)
    return r.returncode == 0 and bool(json.loads(r.stdout or "[]"))


def fetch_diario_json(d: dt.date, dest: Path) -> bool:
    if drive_get(rc_remote(DIARIO_DIR, d.isoformat(), "diario.json"), dest):
        return True
    if GITHUB_RAW_JSON:
        try:
            url = GITHUB_RAW_JSON.format(date=d.isoformat())
            req = urllib.request.Request(url, headers={"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {})
            with urllib.request.urlopen(req, timeout=60) as r:
                dest.write_bytes(r.read())
            return True
        except Exception as ex:  # noqa: BLE001
            print(f"github fallback: {ex}", file=sys.stderr)
    return False


def publish_lock(d: dt.date):
    """One publish of a day at a time: the */10 cron tick and a manual `docker exec … publish` must not
    overlap (both would send the image and print before sent.json exists). Non-blocking — the loser logs
    and exits 0. Returns the open lock file (held until the process exits) or None."""
    import fcntl  # noqa: WPS433  (POSIX only — the script runs on the Pis)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(STATE_DIR / f"publish-{d.isoformat()}.lock", "w")  # noqa: SIM115
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def cmd_publish(d: dt.date, test: bool, force: bool) -> int:
    if d.weekday() >= 5 and not force:
        return 0
    lock = publish_lock(d)
    if lock is None:
        log(f"{d}: outro publish em andamento — nada a fazer."); return 0
    marker = rc_remote(DIARIO_DIR, d.isoformat(), "sent.json")
    if not force and drive_exists(marker):
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="diario-pub-"))
    dj = tmp / "diario.json"
    if not fetch_diario_json(d, dj):
        log(f"{d}: diario.json ainda não existe — nada a fazer."); shutil.rmtree(tmp, ignore_errors=True); return 0
    try:
        diario = json.loads(dj.read_text(encoding="utf-8"))
        pack = tmp / "pack"; pack.mkdir()
        run(["rclone", "copy", rc_remote(DIARIO_DIR, d.isoformat(), "pack"), str(pack), "--transfers", "8",
             "--exclude", "snaps/**"], timeout=900, check=True)
        # only the snapshots the routine picked (from snaps.zip; legacy packs have snaps/ files)
        wanted = {s["event_id"] for s in diario.get("snaps", []) if s.get("event_id")}
        if wanted:
            (pack / "snaps").mkdir(exist_ok=True)
            zpath = pack / "snaps.zip"
            if zpath.exists():
                import zipfile  # noqa: WPS433
                with zipfile.ZipFile(zpath) as zf:
                    for eid in wanted:
                        try:
                            (pack / "snaps" / f"{eid}.jpg").write_bytes(zf.read(f"snaps/{eid}.jpg"))
                        except KeyError:
                            print(f"snap {eid} not in zip", file=sys.stderr)
            else:
                for eid in wanted:
                    drive_get(rc_remote(DIARIO_DIR, d.isoformat(), "pack", "snaps", f"{eid}.jpg"), pack / "snaps" / f"{eid}.jpg", 120)
        import diario_render  # noqa: WPS433
        out = tmp / "out"; out.mkdir()
        res = diario_render.render(diario, pack, out)
        log(f"render: {res}")
        for name in ("resumo.pdf", "completo.pdf", "resumo.jpg", "resumo.html", "completo.html", "semana.pdf", "semana.jpg", "semana.html"):
            p = out / name
            if p.exists():
                run(["rclone", "copyto", str(p), rc_remote(DIARIO_DIR, d.isoformat(), name)], timeout=300, check=True)
        status = {"date": d.isoformat(), "host": socket.gethostname(), "at": dt.datetime.now(TZ).isoformat(timespec="seconds"),
                  "render": res, "test": test}
        chat = TEST_JID if test else GROUP_JID
        caption = diario.get("caption") or f"📋 Diário de Obra {d:%d/%m}"
        try:
            status["whatsapp"] = {"chat": chat, "http": send_image(chat, out / "resumo.jpg", caption)}
            log(f"whatsapp → {chat}: {status['whatsapp']['http']}")
        except Exception as ex:  # noqa: BLE001
            fail(f"whatsapp sendImage: {ex}"); status["whatsapp"] = {"error": str(ex)[:200]}
        if (out / "semana.jpg").exists():   # Friday: the weekly page as a 2nd image (decisão Eduardo 10/09/2026)
            sem = diario.get("semana") or {}
            cap2 = sem.get("caption") or f"📋 Resumo da semana {sem.get('week') or diario.get('week')}"
            try:
                status["whatsapp_semana"] = {"chat": chat, "http": send_image(chat, out / "semana.jpg", cap2)}
                log(f"whatsapp (semana) → {chat}: {status['whatsapp_semana']['http']}")
            except Exception as ex:  # noqa: BLE001
                fail(f"whatsapp sendImage semana: {ex}"); status["whatsapp_semana"] = {"error": str(ex)[:200]}
        status["print"] = do_print(out / "resumo.pdf")
        if not FAILURES or force:
            mp = tmp / "sent.json"; mp.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
            run(["rclone", "copyto", str(mp), marker], timeout=120, check=True)
    except Exception as ex:  # noqa: BLE001
        fail(f"publish: {ex.__class__.__name__}: {ex}")
        traceback.print_exc()
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAILURES else 0


def do_print(pdf: Path) -> dict:
    if not PRINTER:
        return {"skipped": "PRINTER not set"}
    if not pdf.exists():
        fail("print: resumo.pdf ausente"); return {"error": "no pdf"}
    cmd = ["lp", "-d", PRINTER] + PRINT_OPTIONS.split() + [str(pdf)]
    r = run(cmd, timeout=120)
    if r.returncode:
        fail(f"lp rc={r.returncode}: {(r.stderr or r.stdout)[-200:]}"); return {"error": (r.stderr or r.stdout)[-200:]}
    log(f"print: {r.stdout.strip()}")
    return {"job": r.stdout.strip()}


def cmd_print(d: dt.date, force: bool) -> int:
    if d.weekday() >= 5 and not force:
        return 0
    host = socket.gethostname()
    marker = rc_remote(DIARIO_DIR, d.isoformat(), f"printed-{host}.json")
    if not force and drive_exists(marker):
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="diario-print-"))
    pdf = tmp / "resumo.pdf"
    if not drive_get(rc_remote(DIARIO_DIR, d.isoformat(), "resumo.pdf"), pdf):
        shutil.rmtree(tmp, ignore_errors=True); return 0      # not published yet
    status = {"date": d.isoformat(), "host": host, "at": dt.datetime.now(TZ).isoformat(timespec="seconds"), "print": do_print(pdf)}
    if not FAILURES:
        mp = tmp / "printed.json"; mp.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
        run(["rclone", "copyto", str(mp), marker], timeout=120)
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAILURES else 0


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["collect", "publish", "print", "alerttest"])
    ap.add_argument("--date", help="YYYY-MM-DD (default: today, America/Sao_Paulo)")
    ap.add_argument("--test", action="store_true", help="publish: send the image to TEST_JID instead of GROUP_JID")
    ap.add_argument("--force", action="store_true", help="ignore weekday and marker checks")
    a = ap.parse_args()
    d = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(TZ).date()
    if a.cmd == "alerttest":
        fail("teste de alerta (alerttest)"); return 1
    if a.cmd == "collect":
        return cmd_collect(d, a.force)
    if a.cmd == "publish":
        return cmd_publish(d, a.test, a.force)
    return cmd_print(d, a.force)


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:  # noqa: BLE001
        fail("excecao: " + traceback.format_exc().strip().splitlines()[-1])
        rc = 1
    if rc:
        host = socket.gethostname()
        corpo = "\n".join(f"• {f}" for f in FAILURES[-8:]) or "• (sem detalhe registrado)"
        send_alert(f"⚠️ canteiro-diario {sys.argv[1] if len(sys.argv) > 1 else ''} em {host} falhou (rc={rc})\n{corpo}")
    sys.exit(rc)
