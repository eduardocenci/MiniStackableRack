#!/usr/bin/env python3
"""canteiro-painel — live data band shown UNDER the canteiro mosaic (pedido Eduardo 15/11/2026).

The mosaic [fixed lens | PT lens] is 32:9; a TV and most screens are 16:9. This service draws the
missing half: a 1920x540 PNG with five blocks, redrawn every minute —

  NA REDE AGORA       who is on the site Wi-Fi (ara netoverview /api/devices; fixed devices apart)
  PORTÃO              last gate passes by tag (condfy.db in the bnu docker LXC, via bnu-proxmox)
                      (these two share the first column, one above the other — Eduardo 15/11/2026)
  VENCIMENTOS         titles falling due in the next 7 days + the ones overdue up to 7 days and
                      still open, each "agendado DD/MM" (a scheduling slip was filed) or "sem
                      agendamento" (sheet tab Financeiro, same spreadsheet as the plan)
  ENTREGAS            deliveries of the last Diário de Obra (Drive Diario/<date>/diario.json)
  PLANO DA SEMANA     the week's planned activities (sheet PlanejadoRealizado; with the Diário's
                      status marks when the last Diário is of the same week)

and serves it on HTTP :8557 (/painel.png and /painel.json to PAINEL_ALLOW only; /health to anyone) to
  - canteiro-mosaico (bnu Frigate LXC 105), which stacks it under the TV output `mosaico-tv`;
  - the /live page (tailscale serve /painel -> 127.0.0.1:8557), which shows it under the video.

Evidence, never state: everything here is read from the sources of record and nothing is written
back. A block whose source fails keeps its last good data and shows its age in amber.

Authoritative copy: scripts/raspberry-pi/bnu-raspberrypi/canteiro-painel/canteiro-painel.py
Live copy:          bnu-raspberrypi:~/canteiro-jobs/canteiro-painel.py (image canteiro-jobs:local,
                    container canteiro-painel — ../docker/canteiro-jobs/compose.yml)
"""
from __future__ import annotations

import base64
import datetime as dt
import http.server
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

TZ = ZoneInfo("America/Sao_Paulo")


def E(k: str, d: str = "") -> str:
    return os.environ.get(k, d).strip()


PORT = int(E("PAINEL_PORT", "8557"))
ALLOW = {s.strip() for s in E("PAINEL_ALLOW", "127.0.0.1,10.1.1.160").split(",") if s.strip()}
STATE_DIR = Path(E("STATE_DIR", "/var/lib/canteiro-painel"))
ARA_NTO_URL = E("ARA_NTO_URL", "http://ara-raspberrypi2:5000")
FIXED = {s.strip().lower() for s in E(
    "FIXED_DEVICE_NAMES", "Roteador Starlink,Câmera do canteiro (iM9),ara-raspberrypi,ara-raspberrypi2").split(",")}
PROXMOX_HOST = E("PROXMOX_HOST", "bnu-proxmox")
PROXMOX_USER = E("PROXMOX_USER", "root")
PROXMOX_PW = E("PROXMOX_PW")
CONDFY_LXC = E("CONDFY_LXC", "101")
SHEET_ID = E("SHEET_ID")
SHEET_TAB = E("SHEET_TAB", "PlanejadoRealizado")
GSA_KEYFILE = E("GSA_KEYFILE", "/config/gsa.json")
RCLONE_REMOTE = E("RCLONE_REMOTE", "ceuazul:").rstrip("/")
DIARIO_DIR = E("DIARIO_DIR", "Diario")
RENDER_S = int(E("RENDER_S", "60"))
TTL = {"rede": 60, "tags": int(E("TAGS_S", "180")), "diario": int(E("DIARIO_S", "1800")),
       "plano": int(E("PLANO_S", "1800")), "venc": int(E("VENC_S", "900"))}
FIN_TAB = E("FIN_TAB", "Financeiro")
VENC_DIAS = int(E("VENC_DIAS", "7"))      # titles due from today to today + N days…
VENC_ATRAS = int(E("VENC_ATRAS", "7"))    # …plus the ones that fell due in the last N days and are still open

W, H = 1920, 540
BG, CARD, LINE = (14, 17, 22), (24, 29, 37), (44, 52, 64)
FG, DIM, AMBER, GREEN, RED, BLUE = (236, 239, 244), (150, 160, 175), (236, 172, 60), (96, 200, 120), (232, 96, 96), (110, 170, 240)
GF = "/usr/share/fonts/truetype/google/"
F_HEAD = GF + "BarlowCondensed-Bold.ttf"
F_SEMI = GF + "BarlowCondensed-SemiBold.ttf"
F_BODY = GF + "SourceSans3[wght].ttf"
F_FALL = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

MONTHS_PT = {"janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
             "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}
WD_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(TZ):%d/%m %H:%M:%S}] {msg}", flush=True)


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    for p in (path, F_FALL):
        try:
            f = ImageFont.truetype(p, size)
            if "[wght]" in p:          # variable font: default instance is too thin for a TV
                f.set_variation_by_axes([560])
            return f
        except OSError:
            continue
    return ImageFont.load_default()


# ------------------------------------------------------------------ sources
def src_rede() -> dict:
    with urllib.request.urlopen(ARA_NTO_URL + "/api/devices", timeout=15) as r:
        devs = json.loads(r.read().decode())
    if isinstance(devs, dict):
        devs = devs.get("devices", [])
    pessoas, fixos_on, fixos_total, saidas = [], 0, 0, []
    for d in devs:
        name = (d.get("display_name") or d.get("nickname") or d.get("hostname") or d.get("ip") or "?").strip()
        fixed = name.lower() in FIXED
        since = _utc(d.get("last_status_change_utc"))
        if fixed:
            fixos_total += 1
            fixos_on += bool(d.get("online"))
        elif d.get("online"):
            pessoas.append({"nome": name, "desde": since})
        elif since:
            saidas.append({"nome": name, "saiu": since})
    pessoas.sort(key=lambda x: x["desde"] or 0)
    saidas.sort(key=lambda x: -x["saiu"])
    return {"pessoas": pessoas, "fixos_on": fixos_on, "fixos_total": fixos_total, "saidas": saidas[:4]}


def _utc(s: str | None) -> float | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.timestamp()
    except ValueError:
        return None


CONDFY_QUERY = """
import sqlite3, json
c = sqlite3.connect("file:/data/condfy.db?mode=ro", uri=True)
rows = [dict(zip(("ts_local","person","gate","method"), r)) for r in c.execute(
    "select ts_local, person, gate, method from events order by ts_utc desc limit 12")]
print("TAGS_JSON=" + json.dumps(rows, ensure_ascii=False))
"""


def src_tags() -> dict:
    """Last gate passes: bnu-proxmox -> pct exec <LXC> -> docker exec condfy-bridge (same path the Diário uses)."""
    if not PROXMOX_PW:
        raise RuntimeError("PROXMOX_PW not set")
    import paramiko  # noqa: WPS433
    b64 = base64.b64encode(CONDFY_QUERY.encode()).decode()
    cmd = (f"pct exec {CONDFY_LXC} -- docker exec condfy-bridge python3 -c "
           f"\"import base64;exec(base64.b64decode('{b64}').decode())\"")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(PROXMOX_HOST, username=PROXMOX_USER, password=PROXMOX_PW, timeout=30,
                allow_agent=False, look_for_keys=False)
    try:
        _, out, err = cli.exec_command(cmd, timeout=60)
        txt, e = out.read().decode(), err.read().decode()
    finally:
        cli.close()
    line = next((ln for ln in txt.splitlines() if ln.startswith("TAGS_JSON=")), None)
    if line is None:
        raise RuntimeError(f"no TAGS_JSON; stderr: {e.strip()[-120:]}")
    return {"eventos": json.loads(line[len("TAGS_JSON="):])}


def src_diario() -> dict:
    """Newest Diario/<date>/diario.json on the Drive (the public copy — no `equipe`)."""
    env = dict(os.environ)
    r = subprocess.run(["rclone", "lsf", "--dirs-only", f"{RCLONE_REMOTE}/{DIARIO_DIR}/"],
                       capture_output=True, text=True, timeout=120, env=env)
    if r.returncode:
        raise RuntimeError("rclone lsf: " + r.stderr.strip()[-120:])
    days = sorted(d.strip("/") for d in r.stdout.split() if re.fullmatch(r"\d{4}-\d{2}-\d{2}/", d))
    for day in reversed(days[-6:]):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "diario.json"
            g = subprocess.run(["rclone", "copyto", f"{RCLONE_REMOTE}/{DIARIO_DIR}/{day}/diario.json", str(dest)],
                               capture_output=True, text=True, timeout=120, env=env)
            if g.returncode == 0 and dest.exists():
                j = json.loads(dest.read_text(encoding="utf-8"))
                return {"date": day, "week": j.get("week"),
                        "entregas": [{k: _plain(e.get(k)) for k in ("contrato", "o_que", "previsto", "status", "status_label")}
                                     for e in (j.get("entregas") or [])],
                        "plano": [{k: _plain(p.get(k)) for k in ("item", "status", "status_label")}
                                  for p in (j.get("plano") or [])]}
    raise RuntimeError("no diario.json in the last 6 day folders")


def _plain(v) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(v or ""))).strip()


def src_plano() -> dict:
    """PlanejadoRealizado row whose range contains today (same parsing as canteiro-diario.collect_plan)."""
    if not SHEET_ID or not Path(GSA_KEYFILE).exists():
        raise RuntimeError("sheet not configured")
    import gspread  # noqa: WPS433
    rows = gspread.service_account(filename=GSA_KEYFILE).open_by_key(SHEET_ID).worksheet(SHEET_TAB).get_all_values()
    today = dt.datetime.now(TZ).date().isoformat()
    year, prev_month, weeks = 2026, 0, []
    for r in rows[2:]:
        if len(r) < 2 or not r[0].strip().isdigit():
            continue
        m = re.match(r"\s*(\d+)\s+(\w+)\s*-\s*(\d+)\s+(\w+)", r[1].strip(), re.I)
        if not m:
            continue
        d1, m1, d2, m2 = int(m.group(1)), MONTHS_PT.get(m.group(2).lower(), 0), int(m.group(3)), MONTHS_PT.get(m.group(4).lower(), 0)
        if not m1 or not m2:
            continue
        if m1 < prev_month:
            year += 1
        prev_month = m1
        y2 = year + 1 if m2 < m1 else year
        weeks.append({"week": int(r[0]), "range": r[1].strip(), "start": dt.date(year, m1, d1).isoformat(),
                      "end": dt.date(y2, m2, d2).isoformat(), "planned": r[2] if len(r) > 2 else ""})
    for i, w in enumerate(weeks):
        if w["start"] <= today <= w["end"]:
            nxt = weeks[i + 1] if i + 1 < len(weeks) and weeks[i + 1]["planned"].strip() else None
            return {"week": w["week"], "range": w["range"], "itens": _itens(w["planned"]),
                    "proxima": ({"week": nxt["week"], "range": nxt["range"], "itens": _itens(nxt["planned"])} if nxt else None)}
    raise RuntimeError("no week row contains today")


def _itens(txt: str) -> list[str]:
    out = []
    for ln in re.split(r"[\n;]+", txt or ""):
        ln = re.sub(r"^[\s\-–•*\d.)]+", "", ln).strip()
        if ln:
            out.append(ln)
    return out


TAX_RE = re.compile(r"^\s*(ISS|INSS|IRRF|PIS|COFINS|CSLL|CSRF)\b", re.I)
VENC_RE = re.compile(r"venc(?:imento|e|\.)?\s*(?:normal\s*|em\s*|:\s*)?(\d{2})/(\d{2})(?:/(\d{4}))?", re.I)
BR_DATE = re.compile(r"^\s*(\d{2})/(\d{2})/(\d{4})\s*$")
# Financeiro tab, 0-based columns (layout v7 — home-ara finance/sheets_client.py FIELD)
FC = {"id": 0, "fornecedor": 3, "parcela": 9, "data_nf": 10, "numero": 11, "valor_nf": 14, "pago": 20, "obs": 22}


def _money(s: str) -> float:
    """Sheet currency as displayed ("R$3,391.50" or "R$ 3.391,50") -> float; 0.0 when not a number."""
    s = re.sub(r"[^\d,.\-]", "", s or "")
    if not s or s in "-.,":
        return 0.0
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if len(s) - s.rfind(",") == 3 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _vendor(s: str) -> str:
    skip = {"de", "da", "do", "das", "dos", "e", "ltda", "s/a", "sa", "epp", "me", "eireli", "-", "ind", "com", "imp",
            "exp", "industria", "indústria", "comercio", "comércio", "importacao", "exportacao", "empreiteira",
            "mao", "mão", "obra", "obras", "municipio", "município", "prefeitura"}
    out = []
    for w in re.split(r"[\s\-]+", s.strip()):
        k = w.lower().strip(".,")
        if not k or k in skip:
            continue
        out.append(w.upper() if len(w) <= 4 else w.capitalize())
        if len(out) == 2:
            break
    return " ".join(out) or (s.strip()[:24] or "?")


def src_venc() -> dict:
    """Titles falling due, from the Financeiro tab (same spreadsheet as the plan). Read-only.

    A row is OPEN while Pago (U) does not start with "Sim" ("Sim" is Jorge's, "Sim (IA), date" the
    ingest's). "Agend. (IA), DD/MM/YYYY" there = a scheduling slip was filed: scheduled, not paid.
    Due date: a retention guia (Parcela = ISS/INSS/IRRF…) or a parcel without NF number carries it
    in the Data column (K); an invoiced row carries the NF date in K and its boleto's due date in
    Observações ("venc. DD/MM/AAAA") — an invoiced row with no such note is skipped rather than
    guessed."""
    if not SHEET_ID or not Path(GSA_KEYFILE).exists():
        raise RuntimeError("sheet not configured")
    import gspread  # noqa: WPS433
    rows = gspread.service_account(filename=GSA_KEYFILE).open_by_key(SHEET_ID).worksheet(FIN_TAB).get_all_values()
    hoje = dt.datetime.now(TZ).date()
    lo, hi = hoje - dt.timedelta(days=VENC_ATRAS), hoje + dt.timedelta(days=VENC_DIAS)
    cid, forn, tit, antigos_n, antigos_total = "", "", [], 0, 0.0
    for r in rows[2:]:
        r = r + [""] * (FC["obs"] + 1 - len(r))
        if r[FC["id"]].strip():
            cid, forn = r[FC["id"]].strip(), r[FC["fornecedor"]].strip()
        pago, parcela, obs = r[FC["pago"]].strip(), r[FC["parcela"]].strip(), r[FC["obs"]]
        if pago.lower().startswith("sim"):
            continue
        valor = _money(r[FC["valor_nf"]])
        if valor < 0 or (r[FC["valor_nf"]].strip() and valor == 0):
            continue                      # credit notes and zeroed (cancelled) rows
        tax = bool(TAX_RE.match(parcela))
        venc = None
        mk = BR_DATE.match(r[FC["data_nf"]])
        mo = VENC_RE.search(obs)
        if tax or not r[FC["numero"]].strip():
            if mk:
                venc = dt.date(int(mk.group(3)), int(mk.group(2)), int(mk.group(1)))
        elif mo:
            year = int(mo.group(3)) if mo.group(3) else (int(mk.group(3)) if mk else hoje.year)
            venc = dt.date(year, int(mo.group(2)), int(mo.group(1)))
            if mk and not mo.group(3) and venc < dt.date(int(mk.group(3)), int(mk.group(2)), int(mk.group(1))):
                venc = venc.replace(year=year + 1)
        if venc is None:
            continue
        if not valor:
            mv = re.search(r"R\$\s*([\d.]+,\d{2})", obs)
            valor = _money(mv.group(1)) if mv else 0.0
        if venc < lo:
            antigos_n += 1
            antigos_total += valor
            continue
        if venc > hi:
            continue
        ag = ""
        if pago.lower().startswith("agend"):
            ma = re.search(r"(\d{2}/\d{2})/\d{4}", pago)
            ag = ma.group(1) if ma else "sim"
        if tax:
            o_que = parcela
        elif r[FC["numero"]].strip():
            o_que = "NF " + r[FC["numero"]].strip().lstrip("0")
        else:
            o_que = "parcela " + parcela if parcela else "parcela"
        tit.append({"venc": venc.isoformat(), "quem": _vendor(forn), "contrato": cid, "o_que": o_que,
                    "valor": round(valor, 2), "agendado": ag})
    tit.sort(key=lambda t: (t["venc"], -t["valor"]))
    return {"titulos": tit, "antigos_n": antigos_n, "antigos_total": round(antigos_total, 2)}


SOURCES = {"rede": src_rede, "tags": src_tags, "venc": src_venc, "diario": src_diario, "plano": src_plano}
CACHE: dict[str, dict] = {k: {"data": None, "ok_ts": 0.0, "try_ts": 0.0, "erro": None} for k in SOURCES}


def refresh() -> None:
    now = time.time()
    for k, fn in SOURCES.items():
        c = CACHE[k]
        if now - c["try_ts"] < TTL[k]:
            continue
        c["try_ts"] = now
        try:
            c["data"], c["ok_ts"], c["erro"] = fn(), time.time(), None
        except Exception as ex:  # noqa: BLE001 — a failing source must never stop the panel
            c["erro"] = f"{ex.__class__.__name__}: {ex}"[:160]
            log(f"{k}: {c['erro']}")


# ------------------------------------------------------------------ drawing
def fit(dr: ImageDraw.ImageDraw, txt: str, f, maxw: int) -> str:
    if dr.textlength(txt, font=f) <= maxw:
        return txt
    while txt and dr.textlength(txt + "…", font=f) > maxw:
        txt = txt[:-1]
    return txt.rstrip() + "…"


def hhmm(ts: float | None) -> str:
    if not ts:
        return "—"
    t = dt.datetime.fromtimestamp(ts, TZ)
    now = dt.datetime.now(TZ)
    if t.date() == now.date():
        return f"{t:%H:%M}"
    return f"{WD_PT[t.weekday()]} {t:%H:%M}" if (now - t).days < 6 else f"{t:%d/%m}"


def age_txt(k: str) -> tuple[str, tuple]:
    c = CACHE[k]
    if not c["ok_ts"]:
        return ("sem dados", RED)
    a = time.time() - c["ok_ts"]
    if a > 3 * TTL[k] + 120:
        return (f"dado de {hhmm(c['ok_ts'])}", AMBER)
    return ("", DIM)


def render() -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(im)
    f_t, f_s, f_b, f_bs, f_n = font(F_HEAD, 30), font(F_SEMI, 22), font(F_BODY, 24), font(F_BODY, 20), font(F_HEAD, 56)
    now = dt.datetime.now(TZ)
    pad, gap, top, bot = 14, 12, 10, H - 10
    w1, w2, w3 = 350, 440, 470
    x2 = pad + w1 + gap
    x3 = x2 + w2 + gap
    x4 = x3 + w3 + gap
    rede_h = 176
    boxes = {"rede": (pad, top, pad + w1, top + rede_h),
             "tags": (pad, top + rede_h + gap, pad + w1, bot),
             "venc": (x2, top, x2 + w2, bot),
             "entregas": (x3, top, x3 + w3, bot),
             "plano": (x4, top, W - pad, bot)}
    for b in boxes.values():
        dr.rounded_rectangle(b, radius=10, fill=CARD, outline=LINE)

    def head(name: str, title: str, sub: str = "", key: str | None = None) -> tuple[int, int, int, int]:
        x0, y0, x1, y1 = boxes[name]
        dr.text((x0 + 16, y0 + 8), title, font=f_t, fill=AMBER)
        tw = dr.textlength(title, font=f_t)
        note, col = age_txt(key) if key else ("", DIM)
        right = note or sub
        if right:
            right = fit(dr, right, f_s, int(x1 - x0 - tw - 48))
            dr.text((x1 - 16 - dr.textlength(right, font=f_s), y0 + 16), right, font=f_s, fill=col if note else DIM)
        dr.line((x0 + 16, y0 + 50, x1 - 16, y0 + 50), fill=LINE, width=2)
        return x0 + 16, y0 + 58, x1 - 16, y1 - 12

    # 1a — network (top of the first column)
    xa, y, xb, ye = head("rede", "NA REDE AGORA", f"{now:%H:%M}", "rede")
    d = CACHE["rede"]["data"]
    if d:
        n = len(d["pessoas"])
        dr.text((xa, y - 8), str(n), font=f_n, fill=GREEN if n else DIM)
        nw = dr.textlength(str(n), font=f_n)
        dr.text((xa + nw + 12, y), "aparelho" + ("" if n == 1 else "s") + " de pessoas", font=f_b, fill=FG)
        dr.text((xa + nw + 12, y + 28), f"fixos: {d['fixos_on']}/{d['fixos_total']} no ar", font=f_bs,
                fill=DIM if d["fixos_on"] == d["fixos_total"] else RED)
        y += 62
        if n:
            names = ", ".join(p["nome"] for p in d["pessoas"])
            for ln in wrap(dr, names, f_bs, int(xb - xa))[:2]:
                dr.text((xa, y), ln, font=f_bs, fill=FG)
                y += 25
        elif d["saidas"]:
            s = d["saidas"][0]
            dr.text((xa, y), fit(dr, f"último a sair: {s['nome']} · {hhmm(s['saiu'])}", f_bs, int(xb - xa)), font=f_bs, fill=DIM)

    # 1b — gate (bottom of the first column)
    xa, y, xb, ye = head("tags", "PORTÃO — TAGS", "últimas", "tags")
    d = CACHE["tags"]["data"]
    if d:
        last_day = None
        for e in d["eventos"]:
            ts = str(e.get("ts_local") or "")
            day, hm = ts[:10], ts[11:16]
            if y + (54 if day != last_day else 28) > ye:
                break
            if day != last_day:
                try:
                    dd = dt.date.fromisoformat(day)
                    lab = "HOJE" if dd == now.date() else f"{WD_PT[dd.weekday()].upper()} {dd:%d/%m}"
                except ValueError:
                    lab = day
                dr.text((xa, y), lab, font=f_s, fill=DIM)
                y += 26
                last_day = day
            dr.text((xa, y), hm, font=f_bs, fill=BLUE)
            gate = str(e.get("gate") or "").replace("portão ", "")
            gw = dr.textlength(gate, font=f_bs)
            dr.text((xb - gw, y), gate, font=f_bs, fill=DIM)
            dr.text((xa + 62, y), fit(dr, str(e.get("person") or "?"), f_bs, int(xb - xa - 62 - gw - 12)), font=f_bs, fill=FG)
            y += 27
        if not d["eventos"]:
            dr.text((xa, y), "nenhuma passagem registrada", font=f_bs, fill=DIM)

    # 2 — titles falling due
    d = CACHE["venc"]["data"]
    xa, y, xb, ye = head("venc", "VENCIMENTOS", f"até {WD_PT[(now + dt.timedelta(days=VENC_DIAS)).weekday()]} "
                         f"{now + dt.timedelta(days=VENC_DIAS):%d/%m}", "venc")
    if d:
        hoje = now.date()
        if d["antigos_n"]:
            ye -= 30
            dr.text((xa, ye + 6), fit(dr, f"+ {d['antigos_n']} mais antigos sem baixa · R$ {brl(d['antigos_total'])}",
                                      f_bs, int(xb - xa)), font=f_bs, fill=DIM)
        avencer = [t for t in d["titulos"] if t["venc"] >= hoje.isoformat()]
        vencidos = [t for t in d["titulos"] if t["venc"] < hoje.isoformat()][::-1]   # most recent first

        def linha(t: dict, late: bool) -> None:
            nonlocal y
            dd = dt.date.fromisoformat(t["venc"])
            v = "R$ " + brl(t["valor"]) if t["valor"] else "valor a definir"
            vw = dr.textlength(v, font=f_b)
            dr.text((xb - vw, y), v, font=f_b, fill=FG)
            dr.text((xa, y), fit(dr, t["quem"], f_b, int(xb - xa - vw - 14)), font=f_b, fill=FG)
            y += 28
            if t["agendado"]:
                st, c = "agendado" + (f" {t['agendado']}" if t["agendado"] != "sim" else ""), GREEN
            else:
                st, c = "sem agendamento", (RED if late else AMBER)
            sw = dr.textlength(st, font=f_bs)
            dr.ellipse((xb - sw - 22, y + 6, xb - sw - 10, y + 18), fill=c)
            dr.text((xb - sw, y), st, font=f_bs, fill=c)
            quando = "hoje" if dd == hoje else f"{WD_PT[dd.weekday()]} {dd:%d/%m}"
            dr.text((xa, y), fit(dr, f"{quando} · {t['o_que']}", f_bs, int(xb - xa - sw - 34)), font=f_bs, fill=DIM)
            y += 29

        for titulo, lista, late in (("A VENCER", avencer, False), (f"VENCIDOS SEM BAIXA — ÚLTIMOS {VENC_ATRAS} DIAS", vencidos, True)):
            if not lista and late:
                continue
            if y + 26 + 57 > ye:
                break
            dr.text((xa, y), titulo, font=f_s, fill=RED if late else DIM)
            y += 27
            if not lista:
                dr.text((xa, y), "nenhum título nos próximos dias", font=f_bs, fill=DIM)
                y += 30
            shown = 0
            for t in lista:
                if y + 57 > ye - (24 if len(lista) - shown > 1 else 0):
                    break
                linha(t, late)
                shown += 1
            if len(lista) > shown:
                resto = lista[shown:]
                ag = sum(1 for x in resto if x["agendado"])
                dr.text((xa, y), f"+ {len(resto)}" + (f" ({ag} agendado{'s' if ag > 1 else ''})" if ag else "")
                        + f" · R$ {brl(sum(x['valor'] for x in resto))}", font=f_bs, fill=DIM)
                y += 26
            y += 4

    # 3 — deliveries
    dj = CACHE["diario"]["data"]
    sub = ""
    if dj:
        dd = dt.date.fromisoformat(dj["date"])
        sub = f"diário de {WD_PT[dd.weekday()]} {dd:%d/%m}"
    xa, y, xb, ye = head("entregas", "ENTREGAS", sub, "diario")
    if dj:
        col = {"ok": GREEN, "part": AMBER, "no": RED, "na": BLUE}
        for e in dj["entregas"]:
            if y + 58 > ye:
                break
            c = col.get(e["status"], DIM)
            dr.ellipse((xa, y + 9, xa + 14, y + 23), fill=c)
            dr.text((xa + 24, y), fit(dr, e["o_que"], f_b, int(xb - xa - 24)), font=f_b, fill=FG)
            y += 29
            st = e["status_label"]
            sw = dr.textlength(st, font=f_bs)
            dr.text((xb - sw, y), st, font=f_bs, fill=c)
            det = " · ".join(t for t in (re.sub(r"^CON-\d+\w?\s*", "", e["contrato"]), e["previsto"])
                             if t and t.strip("—-– "))
            dr.text((xa + 24, y), fit(dr, det, f_bs, int(xb - xa - 24 - sw - 14)), font=f_bs, fill=DIM)
            y += 33
        if not dj["entregas"]:
            dr.text((xa, y), "nenhuma entrega no último diário", font=f_b, fill=DIM)

    # 4 — week plan
    pl = CACHE["plano"]["data"]
    xa, y, xb, ye = head("plano", f"PLANO DA SEMANA {pl['week']}" if pl else "PLANO DA SEMANA", pl["range"] if pl else "", "plano")
    if pl:
        marks = []
        if dj and dj.get("week") == pl["week"]:
            marks = dj["plano"]
        col = {"ok": GREEN, "part": AMBER, "no": RED, "na": DIM}
        itens = pl["itens"] or ["(semana sem plano na planilha)"]
        for it in itens:
            if y + 30 > ye:
                break
            m = _match(it, marks)
            c = col.get(m["status"], DIM) if m else LINE
            dr.ellipse((xa, y + 9, xa + 14, y + 23), fill=c if m else None, outline=c if m else DIM, width=2)
            st = m["status_label"] if m and m["status"] != "ok" else ""   # green dot says "concluído"
            sw = dr.textlength(st, font=f_bs) if st else 0
            if st:
                dr.text((xb - sw, y + 3), st, font=f_bs, fill=c)
            dr.text((xa + 24, y), fit(dr, it, f_b, int(xb - xa - 24 - sw - (14 if st else 0))), font=f_b, fill=FG)
            y += 30
        nx = pl.get("proxima")
        if nx and y + 60 < ye:
            y += 6
            dr.text((xa, y), f"PRÓXIMA — SEMANA {nx['week']} ({nx['range']})", font=f_s, fill=DIM)
            y += 28
            for it in nx["itens"]:
                if y + 26 > ye:
                    break
                dr.text((xa, y), fit(dr, "· " + it, f_bs, int(xb - xa)), font=f_bs, fill=DIM)
                y += 26
    return im


def wrap(dr: ImageDraw.ImageDraw, txt: str, f, maxw: int) -> list[str]:
    lines, cur = [], ""
    for w in txt.split(" "):
        t = (cur + " " + w).strip()
        if dr.textlength(t, font=f) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    return lines + ([cur] if cur else [])


def brl(v: float) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())[:40]


def _match(item: str, marks: list[dict]) -> dict | None:
    """The Diário rewrites the sheet's wording (and fixes its typos): closest item above 0.7."""
    import difflib  # noqa: WPS433
    k, best, score = _key(item), None, 0.7
    for m in marks:
        r = difflib.SequenceMatcher(None, k, _key(m["item"])).ratio()
        if r > score:
            best, score = m, r
    return best


# ------------------------------------------------------------------ serve
LOCK = threading.Lock()
LAST = {"png": b"", "ts": 0.0}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401, ANN002 — quiet
        return

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")
        # /health carries no names (timestamps + error text) and feeds the globalnet LED: open.
        if self.client_address[0] not in ALLOW and not path.endswith("/health"):
            self.send_error(403)
            return
        if path.endswith("/painel.png"):
            with LOCK:
                body = LAST["png"]
            ctype = "image/png"
        elif path.endswith("/painel.json") or path.endswith("/health"):
            snap = {"ts": LAST["ts"], "fontes": {k: {"ok_ts": c["ok_ts"], "erro": c["erro"]} for k, c in CACHE.items()}}
            if path.endswith("/painel.json"):
                snap["dados"] = {k: c["data"] for k, c in CACHE.items()}
            body, ctype = json.dumps(snap, ensure_ascii=False).encode(), "application/json; charset=utf-8"
        else:
            self.send_error(404)
            return
        if not body:
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def tick() -> None:
    refresh()
    buf = io.BytesIO()
    render().save(buf, "PNG", compress_level=3)
    with LOCK:
        LAST["png"], LAST["ts"] = buf.getvalue(), time.time()
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_DIR / "painel.tmp.png"
        tmp.write_bytes(buf.getvalue())
        tmp.replace(STATE_DIR / "painel.png")
    except OSError as ex:
        log(f"state dir: {ex}")


def main() -> int:
    if "--once" in sys.argv:          # render one PNG to the path given and exit (test)
        for k in TTL:
            TTL[k] = 0
        tick()
        out = Path(sys.argv[sys.argv.index("--once") + 1])
        out.write_bytes(LAST["png"])
        print(json.dumps({k: {"ok": bool(c["ok_ts"]), "erro": c["erro"]} for k, c in CACHE.items()}, ensure_ascii=False))
        return 0
    if len(sys.argv) > 1:
        print(f"unknown argument: {sys.argv[1]}", file=sys.stderr)
        return 2
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"canteiro-painel on :{PORT}, clients {sorted(ALLOW)}")
    while True:
        t0 = time.time()
        try:
            tick()
        except Exception as ex:  # noqa: BLE001
            log(f"tick: {ex.__class__.__name__}: {ex}")
        time.sleep(max(5, RENDER_S - (time.time() - t0)))


if __name__ == "__main__":
    sys.exit(main())
