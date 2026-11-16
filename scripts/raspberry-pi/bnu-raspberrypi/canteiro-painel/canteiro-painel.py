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
import urllib.parse
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
TTL = {"rede": 60, "tags": int(E("TAGS_S", "60")), "diario": int(E("DIARIO_S", "1800")),
       "plano": int(E("PLANO_S", "1800")), "venc": int(E("VENC_S", "900")), "hoje": 60}
LISTENER_URL, LISTENER_TOKEN, OBRA_JID = E("LISTENER_URL").rstrip("/"), E("LISTENER_TOKEN"), E("OBRA_CHAT_JID")
# Supplier-on-the-network notice (pedido Eduardo 15/11/2026): the first time a TAG shows up on the
# site Wi-Fi in the day, one WhatsApp message. off = never send; teste = to TEST_JID (Casa
# SmokeTests); live = to the obra group (OBRA_CHAT_JID, "Cenci Céu Azul Casa-Hangar").
REDE_AVISO = E("REDE_AVISO", "off").lower()
REDE_AVISO_FORA = {t.strip().lower() for t in E("REDE_AVISO_FORA", "Eduardo,Jorge,Ênio,Ariane").split(",") if t.strip()}
WAHA_URL, WAHA_KEY, WAHA_SESSION, TEST_JID = E("WAHA_URL").rstrip("/"), E("WAHA_KEY"), E("WAHA_SESSION", "default"), E("TEST_JID")
HOJE_DIA = E("PAINEL_HOJE")            # YYYY-MM-DD: read that day's trucks instead of today's (tests)
HOJE_CONF = int(E("HOJE_CONF", "60"))  # below this Claudinho's answer is "provável" and gives no automatic baixa
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
    agora = time.time()
    meia_noite = dt.datetime.combine(dt.datetime.now(TZ).date(), dt.time(), TZ).timestamp()
    novos: dict[str, dict] = {}    # people's devices seen today in THIS answer
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
        if not fixed:
            # the tag is what the nickname carries in its last parentheses: "moto-g14 (Ariane)"
            visto = agora if d.get("online") else (_utc(d.get("last_seen_utc")) or 0)
            if visto >= meia_noite:
                novos[(d.get("mac") or name).lower()] = {"nome": name, "ts": visto, "on": bool(d.get("online"))}
    pessoas.sort(key=lambda x: x["desde"] or 0)
    saidas.sort(key=lambda x: -x["saiu"])
    # netoverview drops an offline device from /api/devices once it is more than 30 days old
    # (most of the crew, since 2026-11), so "seen today / in the last hour" cannot come from one
    # answer: the panel sees every device while it is online (one read a minute) and keeps the
    # day's list itself.
    vistos = _rede_hoje(dt.datetime.now(TZ).date().isoformat(), novos)
    try:
        _avisar_rede(dt.datetime.now(TZ), novos)
    except Exception as ex:  # noqa: BLE001 — a notice that fails must never take the card down
        log(f"aviso rede: {ex.__class__.__name__}: {ex}")
    tags: dict[str, bool] = {}     # tag -> seen in the last hour (else: earlier today)
    sem_tag = hora = 0
    for v in vistos.values():
        na_hora = agora - v["ts"] <= 3600
        hora += na_hora
        tg = re.search(r"\(([^()]+)\)\s*$", v["nome"])
        if tg:
            tags[tg.group(1).strip()] = tags.get(tg.group(1).strip(), False) or na_hora
        else:
            sem_tag += 1
    return {"pessoas": pessoas, "fixos_on": fixos_on, "fixos_total": fixos_total, "saidas": saidas[:4], "hora": hora,
            "tags": [{"tag": k, "on": v} for k, v in sorted(tags.items(), key=lambda kv: (not kv[1], kv[0].lower()))],
            "sem_tag": sem_tag}


def _rede_hoje(dia: str, novos: dict[str, dict]) -> dict[str, dict]:
    """The people's devices seen on the site Wi-Fi today: {mac: {nome, ts of the last sighting}},
    kept in STATE_DIR so that a restart of the container does not forget the morning."""
    f = STATE_DIR / "rede-hoje.json"
    try:
        st = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    vistos = st.get("vistos", {}) if st.get("dia") == dia else {}
    for k, v in novos.items():
        if v["ts"] >= vistos.get(k, {}).get("ts", 0):
            vistos[k] = v
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"dia": dia, "vistos": vistos,
                                   "avisados": st.get("avisados", []) if st.get("dia") == dia else []},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
    except OSError as ex:
        log(f"rede-hoje: {ex}")
    return vistos


def _tag(nome: str) -> str:
    m = re.search(r"\(([^()]+)\)\s*$", nome)
    return m.group(1).strip() if m else ""


def aviso_texto(tag: str, hora: str, n: int) -> str:
    return (f"\U0001F4F6 *{tag} no canteiro* — {hora}\n"
            f"_Aviso automático · {'aparelho' if n == 1 else f'{n} aparelhos'} da {tag} na rede Wi-Fi da obra_")


def _avisar_rede(agora: dt.datetime, novos: dict[str, dict]) -> list[str]:
    """One message per tag per day, when one of its devices is ONLINE now and the tag was not
    announced today. Tags in REDE_AVISO_FORA (owners, manager) never announce. The list of the
    day lives next to the sightings (rede-hoje.json), so a restart does not repeat a notice;
    a notice that could not be sent is tried again on the next read."""
    if REDE_AVISO not in ("teste", "live"):
        return []
    chat = TEST_JID if REDE_AVISO == "teste" else OBRA_JID
    if not (WAHA_URL and WAHA_KEY and chat):
        raise RuntimeError("WAHA_URL / WAHA_KEY / chat not set")
    f = STATE_DIR / "rede-hoje.json"
    st = json.loads(f.read_text(encoding="utf-8"))
    if st.get("dia") != agora.date().isoformat():
        return []
    presentes: dict[str, int] = {}
    for v in novos.values():
        t = _tag(v["nome"])
        if t and v.get("on") and t.lower() not in REDE_AVISO_FORA:
            presentes[t] = presentes.get(t, 0) + 1
    enviados = []
    for t, n in sorted(presentes.items()):
        if t in st.get("avisados", []):
            continue
        body = json.dumps({"session": WAHA_SESSION, "chatId": chat, "text": aviso_texto(t, f"{agora:%H:%M}", n)}).encode()
        req = urllib.request.Request(f"{WAHA_URL}/api/sendText", data=body,
                                     headers={"Content-Type": "application/json", "X-Api-Key": WAHA_KEY})
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        st.setdefault("avisados", []).append(t)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
        enviados.append(t)
        log(f"aviso rede ({REDE_AVISO}): {t} {agora:%H:%M}")
    return enviados


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


def _grade_plano(gc, sheet_id: str, tab: str) -> list[list[dict]]:
    """The PlanejadoRealizado tab WITH its formatting: rows of cells {"v": text, "itens": [{"t", "x"}]}.
    `x` = the line is struck through. Ênio copies the plan into "Realizado" and strikes the lines that
    were NOT executed (the struck lines of week 19 are back in the plan of week 20) - a plain
    get_all_values() returns the text only and every struck line read as done (until 2026-11-15)."""
    meta = gc.open_by_key(sheet_id).fetch_sheet_metadata(params={
        "ranges": [f"'{tab}'!A1:D200"], "includeGridData": "true",
        "fields": "sheets.data.rowData.values(formattedValue,effectiveFormat.textFormat.strikethrough,"
                  "textFormatRuns(startIndex,format.strikethrough))"})
    out = []
    for r in meta["sheets"][0]["data"][0].get("rowData", []):
        cells = []
        for c in r.get("values", []):
            txt = c.get("formattedValue", "") or ""
            base = bool(c.get("effectiveFormat", {}).get("textFormat", {}).get("strikethrough"))
            runs = c.get("textFormatRuns") or []
            if runs:
                risc = [False] * len(txt)
                for i, run in enumerate(runs):
                    a = run.get("startIndex", 0)
                    b = runs[i + 1].get("startIndex", len(txt)) if i + 1 < len(runs) else len(txt)
                    if run.get("format", {}).get("strikethrough"):
                        risc[a:b] = [True] * (min(b, len(txt)) - a)
            else:
                risc = [base] * len(txt)
            itens, pos = [], 0
            for ln in txt.split("\n"):
                marcas = [risc[pos + i] for i, ch in enumerate(ln) if not ch.isspace() and ch not in "-–•*"]
                limpo = re.sub(r"^[\s\-–•*\d.)]+", "", ln).strip()
                if limpo:
                    itens.append({"t": limpo, "x": bool(marcas) and sum(marcas) * 2 > len(marcas)})
                pos += len(ln) + 1
            cells.append({"v": txt, "itens": itens})
        out.append(cells)
    return out


def src_plano() -> dict:
    """PlanejadoRealizado row whose range contains today (same parsing as canteiro-diario.collect_plan)."""
    if not SHEET_ID or not Path(GSA_KEYFILE).exists():
        raise RuntimeError("sheet not configured")
    import gspread  # noqa: WPS433
    grade = _grade_plano(gspread.service_account(filename=GSA_KEYFILE), SHEET_ID, SHEET_TAB)
    rows = [[c["v"] for c in g] for g in grade]
    today = dt.datetime.now(TZ).date().isoformat()
    year, prev_month, weeks = 2026, 0, []
    for g, r in list(zip(grade, rows))[2:]:
        if len(r) < 2 or not r[0].strip().isdigit():
            continue
        m = re.match(r"\s*(\d+)\s+(\w+)\s*-\s*(\d+)\s+(\w+)", r[1].strip(), re.I)
        if not m:
            continue
        d1, m1, d2, m2 = int(m.group(1)), MONTHS_PT.get(m.group(2).lower(), 0), int(m.group(3)), MONTHS_PT.get(m.group(4).lower(), 0)
        if not m1 or not m2:
            continue
        real = g[3]["itens"] if len(g) > 3 else []
        if m1 < prev_month:
            year += 1
        prev_month = m1
        y2 = year + 1 if m2 < m1 else year
        weeks.append({"week": int(r[0]), "range": r[1].strip(), "start": dt.date(year, m1, d1).isoformat(),
                      "end": dt.date(y2, m2, d2).isoformat(), "planned": r[2] if len(r) > 2 else "", "real": real})
    for i, w in enumerate(weeks):
        if w["start"] <= today <= w["end"]:
            nxt = weeks[i + 1] if i + 1 < len(weeks) and weeks[i + 1]["planned"].strip() else None
            return {"week": w["week"], "range": w["range"], "itens": _itens(w["planned"]), "real": w["real"],
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


ALERTA_RE = re.compile(r"^(?:\U0001F69A|\U0001F6A7)\s*\*?Caminh[ãa]o\s*(?:de\s+)?(.*?)\s*no canteiro\*?\s*—\s*(\d{1,2}:\d{2})(.*)")
CLAUD_RE = re.compile(r"^\U0001FAE1\s*(\d{1,3})\s*%\s*confiante\s*—\s*([^·]+?)\s*·\s*(.+?)\s*\((CON-\d+\w?)[^)]*\)\s*:\s*(.*)", re.S)
CLAUD2_RE = re.compile(r"^\U0001FAE1\s*(\d{1,3})\s*%\s*confiante\s*—\s*([^·]+?)\s*·\s*([^:(]+?)\s*[:(](.*)", re.S)
SAIU_RE = re.compile(r"^\U0001FAE1\s*Saiu às (\d{1,2}:\d{2})\s*—\s*o caminhão das (\d{1,2}:\d{2})")


def src_hoje() -> dict:
    """Today's trucks, from the obra group itself: every 🚚/🚧 alert of canteiro-alertas and the 🫡
    answer Claudinho gives right after it ("NN % confiante — entrega · Empresa (CON-NNN …): o quê").
    One row per identified contract/company (or per unidentified truck). Evidence, never state: the
    Diário of the night is what records a delivery."""
    if not (LISTENER_URL and LISTENER_TOKEN and OBRA_JID):
        raise RuntimeError("LISTENER_URL / LISTENER_TOKEN / OBRA_CHAT_JID not set")
    dia = dt.date.fromisoformat(HOJE_DIA) if HOJE_DIA else dt.datetime.now(TZ).date()
    t0 = dt.datetime.combine(dia, dt.time(), TZ).timestamp()
    q = urllib.parse.urlencode({"chat": OBRA_JID, "since": t0, "limit": 3000})
    req = urllib.request.Request(f"{LISTENER_URL}/messages?{q}", headers={"X-Listener-Token": LISTENER_TOKEN})
    with urllib.request.urlopen(req, timeout=20) as r:
        msgs = json.loads(r.read().decode())["messages"]
    return {"dia": dia.isoformat(), "caminhoes": _caminhoes([m for m in msgs if m.get("ts", 0) < t0 + 86400])}


def _caminhoes(msgs: list[dict]) -> list[dict]:
    rows: list[dict] = []          # one per truck / per identified contract
    aberto: dict | None = None     # last alert still waiting for Claudinho's answer
    for m in sorted(msgs, key=lambda x: x.get("ts", 0)):
        b = str(m.get("body") or "").strip()
        if not m.get("from_me") or not b:
            continue
        head = b.split("\n", 1)[0]
        a = ALERTA_RE.match(head)
        if a:
            veic, hora, resto = a.group(1).strip(" *"), a.group(2), a.group(3)
            if "saindo" in resto:                       # 2nd and last message of the same truck
                ch = re.search(r"chegou às (\d{1,2}:\d{2})", resto)
                alvo = next((r for r in rows if ch and ch.group(1) in r["horas"]), None)
                if alvo:
                    alvo["saiu"] = hora
                    aberto = None
                    continue
            aberto = {"chegou": hora, "horas": [hora], "veiculo": veic, "saiu": None, "conf": 0, "tipo": "",
                      "empresa": "", "con": "", "o_que": "", "ts": m["ts"]}
            rows.append(aberto)
            continue
        sx = SAIU_RE.match(b)
        if sx:
            alvo = next((r for r in rows if sx.group(2) in r["horas"]), None)
            if alvo:
                alvo["saiu"] = sx.group(1)
            continue
        c = CLAUD_RE.match(b) or CLAUD2_RE.match(b)
        if c and aberto and m["ts"] - aberto["ts"] <= 900:
            full = c.re is CLAUD_RE
            conf, tipo, emp = int(c.group(1)), c.group(2).strip(), c.group(3).strip()
            con = c.group(4) if full else ""
            oq = re.split(r"(?<=[^\d])\.\s", (c.group(5) if full else ""), maxsplit=1)[0].strip()
            mesmo = next((r for r in rows if r is not aberto and (
                (con and r["con"] == con) or (not con and r["empresa"] and _key(r["empresa"]) == _key(emp)))), None)
            if mesmo:                                    # same contract again today: one row, first arrival
                rows.remove(aberto)
                mesmo["horas"] += aberto["horas"]
                mesmo["conf"] = max(mesmo["conf"], conf)
            else:
                aberto.update(conf=conf, tipo=tipo, empresa=emp, con=con, o_que=oq)
            aberto = None
    for r in rows:
        r.pop("ts", None)
    return rows


SOURCES = {"rede": src_rede, "tags": src_tags, "venc": src_venc, "diario": src_diario, "plano": src_plano,
           "hoje": src_hoje}
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
    rede_h = 208   # room for two rows of tag pills
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
        h1 = d.get("hora", n)
        t1 = f"{h1} na última hora"
        dr.text((xa + nw + 12, y + 28), t1, font=f_bs, fill=DIM)
        if d["fixos_on"] != d["fixos_total"]:   # the site's own gear (camera, Pi...) only shows up when one is down
            dr.text((xa + nw + 12 + dr.textlength(t1 + " · ", font=f_bs), y + 28),
                    f"fixos {d['fixos_on']}/{d['fixos_total']}", font=f_bs, fill=RED)
        y += 60
        # Hoje: one pill per tag (company/person in the nickname's parentheses) seen today -
        # lit while one of its devices was on the network in the last hour, greyed out after.
        dr.text((xa, y + 3), "Hoje:", font=f_bs, fill=DIM)
        x = x0p = xa + dr.textlength("Hoje:", font=f_bs) + 10
        tags = d.get("tags") or []
        resto = d.get("sem_tag", 0)
        if not tags and not resto:
            dr.text((x, y + 3), "ninguém na rede", font=f_bs, fill=DIM)
        for i, t in enumerate(tags):
            w = int(dr.textlength(t["tag"], font=f_bs)) + 20
            if x + w > xb and x > x0p:
                x, y = xa, y + 31
            if y + 27 > ye + 8:
                resto += len(tags) - i
                break
            if t["on"]:
                dr.rounded_rectangle((x, y, x + w, y + 27), radius=13, fill=(30, 74, 48), outline=GREEN, width=2)
            else:
                dr.rounded_rectangle((x, y, x + w, y + 27), radius=13, fill=None, outline=LINE, width=2)
            dr.text((x + 10, y + 2), t["tag"], font=f_bs, fill=FG if t["on"] else (104, 112, 126))
            x += w + 8
        if resto and (tags or d.get("sem_tag")):
            dr.text((x, y + 3), f"+ {resto} sem tag" if resto == d.get("sem_tag", 0) else f"+ {resto}", font=f_bs, fill=DIM)

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

        # What needs someone's attention comes first: overdue without a payment scheduled (red),
        # then falling due without one (amber). A title already scheduled (green) is drawn only
        # in the room the others leave - hidden ones are counted in the section's "+ N" line.
        secs = [("A VENCER", avencer, False), (f"VENCIDOS SEM BAIXA — ÚLTIMOS {VENC_ATRAS} DIAS", vencidos, True)]
        pick: list[set[int]] = [set(), set()]

        def altura(sel: list[set[int]]) -> int:
            h = 0
            for (_, lista, late), k in zip(secs, sel):
                if not lista and late:
                    continue
                h += 27 + (57 * len(k) + (26 if len(k) < len(lista) else 0) if lista else 30) + 4
            return h

        fila = ([(1, i) for i, t in enumerate(vencidos) if not t["agendado"]]
                + [(0, i) for i, t in enumerate(avencer) if not t["agendado"]]
                + [(0, i) for i, t in enumerate(avencer) if t["agendado"]]
                + [(1, i) for i, t in enumerate(vencidos) if t["agendado"]])
        for si, i in fila:
            pick[si].add(i)
            if altura(pick) > ye - y:
                pick[si].discard(i)
        for (titulo, lista, late), k in zip(secs, pick):
            if not lista and late:
                continue
            if y + 27 + 26 > ye:
                break
            dr.text((xa, y), titulo, font=f_s, fill=RED if late else DIM)
            y += 27
            if not lista:
                dr.text((xa, y), "nenhum título nos próximos dias", font=f_bs, fill=DIM)
                y += 30
            for i, t in enumerate(lista):
                if i in k:
                    linha(t, late)
            resto = [t for i, t in enumerate(lista) if i not in k]
            if resto:
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
        col = {"part": AMBER, "no": RED, "na": BLUE}
        rows = [e for e in dj["entregas"] if not _filler(e)]
        feitos = [e for e in rows if _recebido(e)]
        falta = [e for e in rows if not _recebido(e)]
        tw = int(xb - xa - 24)
        # Today, before tonight's Diário: the trucks the camera alerted and Claudinho identified.
        # Blue = seen by the camera (evidence); green stays for what a Diário recorded.
        hj = CACHE["hoje"]["data"]
        hoje = hj["caminhoes"] if hj and hj["dia"] > dj["date"] else []
        hd = dt.date.fromisoformat(hj["dia"]) if hj else now.date()
        quando = "hoje" if hd == now.date() else f"{WD_PT[hd.weekday()]} {hd:%d/%m}"
        for t in hoje:
            if t["con"] and t["conf"] >= HOJE_CONF and _key(t["tipo"]).startswith("entrega"):
                for e in [e for e in falta if e["contrato"].split(" ")[0] == t["con"]]:
                    falta.remove(e)
                    feitos.insert(0, dict(e, _cam=True, previsto="", status_label=f"chegou {quando} {t['chegou']}"))
        if hoje:
            dr.text((xa, y), "HOJE" if hd == now.date() else f"{WD_PT[hd.weekday()].upper()} {hd:%d/%m}", font=f_s, fill=DIM)
            dr.text((xb - dr.textlength("pela câmera", font=f_bs), y + 2), "pela câmera", font=f_bs, fill=BLUE)
            y += 27
            for i, t in enumerate(hoje):
                if y + 56 > ye - 120:                    # always leave room for the Diário's sections
                    dr.text((xa, y - 2), f"+ {len(hoje) - i} caminhões", font=f_bs, fill=DIM)
                    y += 24
                    break
                ident = bool(t["empresa"])
                fraco = ident and t["conf"] < HOJE_CONF
                cc = BLUE if ident and not fraco else AMBER if fraco else DIM
                dr.ellipse((xa, y + 9, xa + 14, y + 23), fill=cc if ident else None, outline=cc, width=2)
                # no exit message yet: "no canteiro" only while that is still likely (2 h), then nothing
                hh, mm = (int(x) for x in t["chegou"].split(":"))
                idade = (now - dt.datetime.combine(hd, dt.time(hh, mm), TZ)).total_seconds()
                st = f"saiu {t['saiu']}" if t["saiu"] else "no canteiro" if 0 <= idade < 7200 else ""
                sw = dr.textlength(st, font=f_bs)
                dr.text((xb - sw, y + 3), st, font=f_bs, fill=DIM if t["saiu"] else BLUE)
                quem = t["empresa"] if ident else "caminhão " + (t["veiculo"] or "de tipo incerto")
                dr.text((xa + 24, y), fit(dr, f"{t['chegou']}  {quem}", f_b, int(tw - sw - 12)), font=f_b, fill=FG)
                y += 28
                if ident:
                    d2 = " · ".join(x for x in ((f"provável {t['conf']} %" if fraco else ""), t["tipo"], t["o_que"]) if x)
                else:
                    d2 = "não identificado"
                dr.text((xa + 24, y), fit(dr, d2, f_bs, tw), font=f_bs, fill=AMBER if fraco else DIM)
                y += 28
            y += 4

        def bloco(e: dict, done: bool, maxl: int, draw: bool) -> int:
            """Height of one delivery; draws it at y when asked."""
            c = BLUE if e.get("_cam") else GREEN if done else col.get(e["status"], AMBER)
            lines = wrap(dr, e["o_que"], f_b, tw)
            if len(lines) > maxl:
                lines = lines[:maxl - 1] + [fit(dr, " ".join(lines[maxl - 1:]), f_b, tw)]
            # The Diário writes from its own day ("executada ontem"): on the panel, days later, that
            # reads wrong - relative words become that day's date. And it often says the same thing
            # twice ("no canteiro até 19/11" / "no canteiro"): a received item shows it ONCE, in
            # green on the right, in its fuller form; the grey line keeps only the vendor.
            st, prev = _abs_dia(e["status_label"], dd, now.date()), _abs_dia(e["previsto"], dd, now.date())
            if e.get("_cam"):
                st = e["status_label"]
            if not prev.strip("—-– "):
                prev = ""
            if e.get("_cam"):
                pass
            elif done and prev and (RECEBIDO_RE.search(prev) or not RECEBIDO_RE.search(st)):
                if RECEBIDO_RE.search(prev):
                    st, prev = prev, ""
            elif prev and (_key(prev) == _key(st) or _key(st) in _key(prev)):
                st, prev = prev, ""
            det = " · ".join(t for t in (re.sub(r"^CON-\d+\w?\s*", "", e["contrato"]).strip("—-– "), prev) if t)
            sw = dr.textlength(st, font=f_bs)
            dl = wrap(dr, det, f_bs, int(tw - sw - 14))[:2] if det else []
            if len(dl) == 2:
                dl[1] = fit(dr, " ".join(wrap(dr, det, f_bs, int(tw - sw - 14))[1:]), f_bs, tw)
            if draw:
                dr.ellipse((xa, y + 9, xa + 14, y + 23), fill=c)
                yy = y
                for ln in lines:
                    dr.text((xa + 24, yy), ln, font=f_b, fill=FG)
                    yy += 28
                dr.text((xb - sw, yy), st, font=f_bs, fill=c)
                for ln in dl:
                    dr.text((xa + 24, yy), ln, font=f_bs, fill=DIM)
                    yy += 25
            return 28 * len(lines) + 25 * max(1, len(dl)) + 9

        secs = [("A RECEBER", falta, False), ("JÁ RECEBIDOS", feitos, True)]
        maxl = 4   # as many lines of text per item as the card has room for
        while maxl > 1 and sum(27 + (sum(bloco(e, d, maxl, False) for e in li) if li else 30) + 6
                               for _, li, d in secs if li or not d) > ye - y:
            maxl -= 1
        for titulo, lista, done in secs:
            if not lista and done:
                continue
            if y + 27 + 30 > ye:
                break
            dr.text((xa, y), titulo, font=f_s, fill=DIM)
            y += 27
            if not lista:
                dr.text((xa, y), "nada pendente no último diário", font=f_bs, fill=DIM)
                y += 30
            for i, e in enumerate(lista):
                h = bloco(e, done, maxl, False)
                if y + h > ye:
                    dr.text((xa, y - 4), f"+ {len(lista) - i}", font=f_bs, fill=DIM)
                    break
                bloco(e, done, maxl, True)
                y += h
            y += 6

    # 4 — week plan
    pl = CACHE["plano"]["data"]
    xa, y, xb, ye = head("plano", f"PLANO DA SEMANA {pl['week']}" if pl else "PLANO DA SEMANA", pl["range"] if pl else "", "plano")
    if pl:
        marks = []
        if dj and dj.get("week") == pl["week"]:
            marks = dj["plano"]
        col = {"ok": GREEN, "part": AMBER, "no": RED, "na": DIM}
        itens = pl["itens"] or ["(semana sem plano na planilha)"]
        # Ênio's own "Realizado" column decides when it is filled: a line he struck through there
        # was NOT executed; an unstruck one was. The Diário's marks only fill what he left out.
        real = [dict(r, item=r["t"]) for r in (pl.get("real") or [])]
        usados: set[int] = set()
        for it in itens:
            if y + 30 > ye:
                break
            rm = _match(it, real) if real else None
            if rm:
                usados.add(id(rm))
            if rm and rm["x"]:
                dr.ellipse((xa, y + 9, xa + 14, y + 23), fill=None, outline=RED, width=2)
                txt = fit(dr, it, f_b, int(xb - xa - 24))
                dr.text((xa + 24, y), txt, font=f_b, fill=DIM)
                dr.line((xa + 24, y + 17, xa + 24 + dr.textlength(txt, font=f_b), y + 17), fill=DIM, width=2)
                y += 30
                continue
            m = {"status": "ok", "status_label": ""} if rm else _match(it, marks)
            c = col.get(m["status"], DIM) if m else LINE
            dr.ellipse((xa, y + 9, xa + 14, y + 23), fill=c if m else None, outline=c if m else DIM, width=2)
            st = m["status_label"] if m and m["status"] != "ok" else ""   # green dot says "concluído"
            sw = dr.textlength(st, font=f_bs) if st else 0
            if st:
                dr.text((xb - sw, y + 3), st, font=f_bs, fill=c)
            dr.text((xa + 24, y), fit(dr, it, f_b, int(xb - xa - 24 - sw - (14 if st else 0))), font=f_b, fill=FG)
            y += 30
        # What was done beyond the plan is listed in full, under its own heading - it comes before
        # next week's plan, which only gets the room that is left (pedido Eduardo 15/11/2026).
        extras = [r for r in real if id(r) not in usados and not r["x"]]
        if extras and y + 28 + 28 <= ye:
            y += 4
            dr.text((xa, y), f"ALÉM DO PLANO — {len(extras)} FEITO{'S' if len(extras) > 1 else ''}", font=f_s, fill=GREEN)
            y += 28
            for i, r in enumerate(extras):
                if y + 28 > ye:
                    dr.text((xa + 24, y - 2), f"+ {len(extras) - i}", font=f_bs, fill=DIM)
                    break
                lns = wrap(dr, r["t"], f_b, int(xb - xa - 24))
                if y + 28 * len(lns) > ye:            # no room for the whole text: one line, cut
                    lns = [fit(dr, r["t"], f_b, int(xb - xa - 24))]
                dr.text((xa + 1, y - 3), "+", font=f_t, fill=GREEN)
                for ln in lns:
                    dr.text((xa + 24, y), ln, font=f_b, fill=FG)
                    y += 28
                y += 1
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


RECEBIDO_RE = re.compile(r"\b(entreg(ue|ues|a|ou)|recebid[oa]s?|executad[oa]s?|conclu[ií]d[oa]s?|"
                         r"descarregad[oa]s?|retirad[oa]s?|instalad[oa]s?|chegou|chegaram|no canteiro)\b", re.I)


def _recebido(e: dict) -> bool:
    """Has this delivery already happened? The Diário marks most of them `na` and says it in words
    ("entregue seg 09/11", "executada ontem", "no canteiro"), so the label decides."""
    if e["status"] == "ok":
        return True
    if e["status"] in ("no", "part"):
        return False
    return bool(RECEBIDO_RE.search(e["status_label"]) or RECEBIDO_RE.search(e["previsto"]))


REL_DIA = {"anteontem": -2, "ontem": -1, "hoje": 0, "amanhã": 1, "amanha": 1}


def _abs_dia(txt: str, ref: dt.date, today: dt.date) -> str:
    """"executada ontem" in the Diário of Fri 13/11, read on Sunday -> "executada qui 12/11"."""
    if ref == today:
        return txt

    def sub(m: re.Match) -> str:
        d = ref + dt.timedelta(days=REL_DIA[m.group(0).lower()])
        return f"{WD_PT[d.weekday()]} {d:%d/%m}"
    return re.sub(r"\b(anteontem|ontem|hoje|amanh[ãa])\b", sub, txt, flags=re.I)


def _filler(e: dict) -> bool:
    """The Diário's closing row "Nenhuma outra entrega prevista para hoje" is not a delivery."""
    return not e["contrato"].strip("—-– ") and e["o_que"].lower().startswith(("nenhum", "sem "))


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
