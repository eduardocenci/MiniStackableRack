# -*- coding: utf-8 -*-
"""diario_render — Diário de Obra ARA: diario.json + evidence pack → resumo (1 page A4) + completo (A4) as
HTML/PDF/JPG. Deterministic: the LLM (cloud routine) only writes diario.json; every figure comes from the pack.

render(diario: dict, pack: Path, out: Path) -> dict   writes out/resumo.html, completo.html, resumo.pdf,
completo.pdf, resumo.jpg and returns {"resumo_pages": n, "completo_pages": n, ...}.

Rendering engine: Chromium headless (--print-to-pdf) + poppler (pdfinfo/pdftoppm). Fonts: Barlow Condensed +
Source Sans 3 installed in the image (Dockerfile). Layout and CSS are the ones reviewed by Eduardo on 09/09/2026.
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

CHROMIUM = os.environ.get("CHROMIUM_BIN", "chromium")
X0_DEFAULT, X1_DEFAULT = 6 * 60 + 45, 17 * 60 + 15
STATUS_LABEL = {"ok": "feito", "part": "em andamento", "no": "não", "na": "n/a"}


# ---------------------------------------------------------------- images
def data_uri(path: Path, width: int | None = None, quality: int = 76) -> tuple[str, tuple[int, int]]:
    im = Image.open(path).convert("RGB")
    if width and im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(), im.size


def img_tag(path: Path | None, alt: str, width: int | None = None, quality: int = 76) -> str:
    if not path or not path.exists():
        return f'<div class="missing">imagem indisponível: {html.escape(alt)}</div>'
    uri, (w, h) = data_uri(path, width, quality)
    return f'<img src="{uri}" width="{w}" height="{h}" alt="{html.escape(alt)}">'


def hhmm_to_min(s: str) -> int:
    m = re.match(r"(\d{1,2}):(\d{2})", s or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 0


# ---------------------------------------------------------------- timeline SVG
def timeline_svg(diario: dict, pack: Path) -> str:
    hist = json.loads((pack / "hist15.json").read_text(encoding="utf-8")) if (pack / "hist15.json").exists() else []
    events = json.loads((pack / "events.json").read_text(encoding="utf-8")) if (pack / "events.json").exists() else []
    tags_raw = json.loads((pack / "tags.json").read_text(encoding="utf-8")) if (pack / "tags.json").exists() else None
    tl = diario.get("timeline") or {}
    bands, spans = tl.get("bands") or [], tl.get("spans") or []
    if not hist:
        return '<p class="missing">sem eventos Frigate no dia</p>'
    mins = [b["min"] for b in hist]
    X0 = min(X0_DEFAULT, (min(mins) // 60) * 60 - 15)
    X1 = max(X1_DEFAULT, (max(mins) // 60) * 60 + 75)
    W, H = 1000, 330
    ML, MR = 58, 16
    PW = W - ML - MR

    def x(m): return ML + (m - X0) / (X1 - X0) * PW
    BAR_TOP, BAR_H = 34, 150
    VEH_Y = BAR_TOP + BAR_H + 30
    TAG_Y = VEH_Y + 34
    WIFI_Y = TAG_Y + 34
    AXIS_Y = WIFI_Y + 30
    maxp = max(1, max(b["person"] for b in hist))

    def ybar(v): return BAR_TOP + BAR_H - v / maxp * BAR_H
    s = [f'<svg class="tl" viewBox="0 0 {W} {H}" role="img" preserveAspectRatio="xMidYMid meet">',
         '<title>Linha do tempo do dia</title>']
    for i, b in enumerate(bands):
        a, e = hhmm_to_min(b.get("from")), hhmm_to_min(b.get("to"))
        anc = b.get("anchor") or "start"; dy = 14 * (i % 2)
        s.append(f'<rect x="{x(a):.1f}" y="{BAR_TOP-4}" width="{max(2, x(e)-x(a)):.1f}" height="{BAR_H+4}" class="band"/>')
        lx = x(a) + 4 if anc == "start" else x(e) - 4
        s.append(f'<text x="{lx:.1f}" y="{BAR_TOP+10+dy}" class="bandlab" text-anchor="{anc}">{html.escape(b.get("label",""))}</text>')
    for hr in range(X0 // 60 + (1 if X0 % 60 else 0), X1 // 60 + 1):
        xx = x(hr * 60)
        s.append(f'<line x1="{xx:.1f}" y1="{BAR_TOP}" x2="{xx:.1f}" y2="{AXIS_Y}" class="grid"/>')
        s.append(f'<text x="{xx:.1f}" y="{AXIS_Y+16}" class="tick" text-anchor="middle">{hr:02d}:00</text>')
    step = 10 if maxp > 15 else 5
    for v in range(0, maxp + 1, step):
        s.append(f'<line x1="{ML}" y1="{ybar(v):.1f}" x2="{W-MR}" y2="{ybar(v):.1f}" class="grid"/>')
        s.append(f'<text x="{ML-6}" y="{ybar(v)+4:.1f}" class="tick" text-anchor="end">{v}</text>')
    s.append(f'<text x="{ML-6}" y="{BAR_TOP-2}" class="rowlab" text-anchor="end">pessoas</text>')
    bw = (x(15) - x(0)) - 2
    for b in hist:
        v = b["person"]
        if not v:
            continue
        yy = ybar(v)
        s.append(f'<rect x="{x(b["min"])+1:.1f}" y="{yy:.1f}" width="{bw:.1f}" height="{BAR_TOP+BAR_H-yy:.1f}" rx="2" class="bar">'
                 f'<title>{b["t"]}: {v} eventos de pessoa, {b.get("car",0)} de veículo</title></rect>')
        if v >= maxp * 0.72:
            s.append(f'<text x="{x(b["min"])+1+bw/2:.1f}" y="{yy-5:.1f}" class="val" text-anchor="middle">{v}</text>')
    s.append(f'<text x="{ML-6}" y="{VEH_Y+4}" class="rowlab" text-anchor="end">veículos</text>')
    for e in events:
        if e.get("label") == "car":
            m = hhmm_to_min(e["start"]) + int(e["start"][6:8]) / 60
            s.append(f'<line x1="{x(m):.1f}" y1="{VEH_Y-9}" x2="{x(m):.1f}" y2="{VEH_Y+9}" class="veh"><title>{e["start"]} veículo</title></line>')
    s.append(f'<text x="{ML-6}" y="{TAG_Y+4}" class="rowlab" text-anchor="end">portão</text>')
    if tags_raw:
        seen: dict[str, int] = {}
        for t in tags_raw:
            person = (t.get("person") or "?").split()[0]
            hm = (t.get("ts_local") or "")[11:16]
            m = hhmm_to_min(hm)
            n = seen.get(person, 0); seen[person] = n + 1
            kind = "in" if n % 2 == 0 else "out"
            xx = x(m)
            if kind == "in":
                pts = f"{xx-6:.1f},{TAG_Y+6} {xx+6:.1f},{TAG_Y+6} {xx:.1f},{TAG_Y-6}"
            else:
                pts = f"{xx-6:.1f},{TAG_Y-6} {xx+6:.1f},{TAG_Y-6} {xx:.1f},{TAG_Y+6}"
            s.append(f'<polygon points="{pts}" class="tag"><title>{html.escape(person)} {hm} {t.get("gate","")}</title></polygon>')
            anchor = "start" if kind == "in" else "end"; dx = 9 if anchor == "start" else -9
            s.append(f'<text x="{xx+dx:.1f}" y="{TAG_Y+4}" class="taglab" text-anchor="{anchor}">{html.escape(person)} {hm}</text>')
    else:
        s.append(f'<text x="{ML+6}" y="{TAG_Y+4}" class="bandlab">sem dados de tag</text>')
    s.append(f'<text x="{ML-6}" y="{WIFI_Y+4}" class="rowlab" text-anchor="end">wi-fi</text>')
    rows = {"crew": WIFI_Y - 8, "vis": WIFI_Y + 1, "unk": WIFI_Y + 9}
    for sp in spans:
        a, e = hhmm_to_min(sp.get("from")), hhmm_to_min(sp.get("to"))
        k = sp.get("kind") if sp.get("kind") in rows else "unk"
        s.append(f'<rect x="{x(a):.1f}" y="{rows[k]}" width="{max(3, x(e)-x(a)):.1f}" height="6" rx="3" class="wifi {k}"><title>{html.escape(sp.get("label",""))}</title></rect>')
        if sp.get("label"):
            ly = WIFI_Y - 11 if k == "crew" else (WIFI_Y + 6 if k == "vis" else WIFI_Y + 15)
            s.append(f'<text x="{x(a)+4:.1f}" y="{ly}" class="bandlab">{html.escape(sp["label"])}</text>')
    s.append(f'<line x1="{ML}" y1="{AXIS_Y}" x2="{W-MR}" y2="{AXIS_Y}" class="axis"/>')
    s.append("</svg>")
    return "\n".join(s)


# ---------------------------------------------------------------- HTML
CSS = r"""
<style>
@page { size: A4; margin: 10mm 11mm 12mm 11mm; }
:root{--bg:#fff;--panel:#f3f4f2;--ink:#1c1f1e;--ink2:#55605c;--ink3:#7c8783;--line:#cfd5d0;--accent:#c9581a;--pine:#b48a4c;--pine-soft:#efe3cf;
 --ok:#2c7a4b;--ok-soft:#dcefe3;--warn:#9a6a00;--warn-soft:#f4ead0;--no:#8a4a3a;--no-soft:#f0dcd5;--series-p:#2a78d6;--series-v:#c9581a;--band:rgba(180,138,76,.18);--grid:#e1e5e1}
*{box-sizing:border-box} html,body{margin:0;padding:0;background:#fff}
body{color:var(--ink);font-family:"Source Sans 3","Segoe UI",system-ui,sans-serif;font-size:9pt;line-height:1.3;-webkit-print-color-adjust:exact;print-color-adjust:exact}
body.compact{font-size:8.5pt} body.compact2{font-size:8pt}
/* last resort for the one-pager: scale the whole page (Chromium honours zoom in print) */
body.z94{zoom:.94} body.z88{zoom:.88} body.z82{zoom:.82} body.z76{zoom:.76}
h1,h2,h3,.eyebrow,.chip,.stat b,.rowlab,.pgtitle{font-family:"Barlow Condensed","Arial Narrow",sans-serif}
h1{font-size:23pt;line-height:.95;margin:2pt 0 4pt;font-weight:700;letter-spacing:-.01em}
h2{font-size:12.5pt;margin:0 0 3pt;font-weight:600} h3{font-size:10.5pt;margin:0 0 3pt;font-weight:600}
.eyebrow{font-size:7.4pt;letter-spacing:.14em;text-transform:uppercase;color:var(--ink2);font-weight:600}
p{margin:0 0 5pt}
header{border-bottom:2pt solid var(--ink);padding-bottom:5pt;margin-bottom:6pt}
.verdict{font-size:9.9pt;line-height:1.28;margin:0} .verdict b{color:var(--accent)}
.meta{display:flex;flex-wrap:wrap;gap:1pt 12pt;margin-top:4pt;color:var(--ink2);font-size:7.9pt} .meta span b{color:var(--ink);font-weight:600}
section{margin:0 0 6pt}
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:5pt;margin:2pt 0 0}
.stat{background:var(--panel);border-top:1.4pt solid var(--line);padding:3pt 6pt}
.stat b{display:block;font-size:17pt;line-height:1;font-weight:700;font-variant-numeric:tabular-nums}
.stat span{font-size:7.2pt;color:var(--ink2);line-height:1.2;display:block;margin-top:1pt}
table{border-collapse:collapse;width:100%;font-size:7.9pt;line-height:1.25}
th{text-align:left;font-family:"Barlow Condensed",sans-serif;font-size:7pt;letter-spacing:.08em;text-transform:uppercase;color:var(--ink2);font-weight:600;padding:2pt 4pt;border-bottom:1.2pt solid var(--line)}
td{padding:2.2pt 4pt;border-bottom:.6pt solid var(--line);vertical-align:top} td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr{break-inside:avoid}
.chip{display:inline-block;font-size:6.6pt;letter-spacing:.07em;text-transform:uppercase;font-weight:600;padding:1pt 4pt;border-radius:2pt;white-space:nowrap;line-height:1.35}
.chip.ok{background:var(--ok-soft);color:var(--ok)} .chip.part{background:var(--warn-soft);color:var(--warn)}
.chip.no{background:var(--no-soft);color:var(--no)} .chip.na{background:var(--panel);color:var(--ink3);border:.6pt solid var(--line)}
figure{margin:0;break-inside:avoid} figure img{max-width:100%;height:auto}
figcaption{font-size:7.8pt;color:var(--ink2);margin-top:3pt;line-height:1.3} figcaption b{color:var(--ink);font-weight:600}
.wide img{width:auto;max-width:100%;max-height:58mm;height:auto;display:block;margin:0 auto;border:.5pt solid var(--line)}
body.compact .wide img{max-height:52mm} body.compact2 .wide img{max-height:46mm}
.frames{display:grid;grid-template-columns:1fr 1fr;gap:9pt 8pt} .frames img{width:100%;height:auto;display:block;border:.5pt solid var(--line)}
.frames figcaption .t{font-family:"Barlow Condensed",sans-serif;font-size:9.5pt;font-weight:700;color:var(--ink);margin-right:4pt}
.snaps{display:grid;grid-template-columns:repeat(3,1fr);gap:7pt} .snaps img{width:100%;height:auto;display:block;border:.5pt solid var(--line)}
.chartbox{background:var(--panel);border:.6pt solid var(--line);padding:8pt 6pt 4pt} .chartbox svg{width:100%;height:auto;display:block}
.legend{display:flex;flex-wrap:wrap;gap:3pt 12pt;font-size:7.6pt;color:var(--ink2);margin:5pt 3pt 0}
.legend i{display:inline-block;width:8pt;height:8pt;vertical-align:-1pt;margin-right:4pt;border-radius:1.5pt}
.tl .bar{fill:var(--series-p)} .tl .veh{stroke:var(--series-v);stroke-width:2} .tl .tag{fill:var(--pine);stroke:#fff;stroke-width:1}
.tl .band{fill:var(--band)} .tl .grid{stroke:var(--grid);stroke-width:1} .tl .axis{stroke:var(--ink2);stroke-width:1.2}
.tl .tick{fill:var(--ink2);font-size:12px;font-family:"Source Sans 3",sans-serif;font-variant-numeric:tabular-nums}
.tl .rowlab{fill:var(--ink2);font-size:13px;letter-spacing:.06em;text-transform:uppercase}
.tl .val{fill:var(--ink);font-size:12px;font-family:"Source Sans 3",sans-serif;font-weight:600}
.tl .bandlab{fill:var(--ink2);font-size:11.5px;font-family:"Source Sans 3",sans-serif} .tl .taglab{fill:var(--ink);font-size:11.5px;font-family:"Source Sans 3",sans-serif}
.tl .wifi.crew{fill:var(--ink3)} .tl .wifi.vis{fill:var(--pine)} .tl .wifi.unk{fill:var(--series-v);opacity:.7}
ul{padding-left:12pt;margin:0 0 5pt} li{margin:2pt 0}
.two{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12pt}
.note{border-left:2pt solid var(--pine);padding:4pt 8pt;background:var(--pine-soft);font-size:8.4pt}
.src{font-size:8.4pt;color:var(--ink2)} .src b{color:var(--ink);font-weight:600}
.missing{font-size:8pt;color:var(--ink3);border:.6pt dashed var(--line);padding:6pt;text-align:center}
.pg{break-before:page} .pgtitle{font-size:7.4pt;letter-spacing:.14em;text-transform:uppercase;color:var(--ink3);margin-bottom:6pt;font-weight:600}
.foot{position:fixed;bottom:-8mm;left:0;right:0;font-size:7pt;color:#777;display:flex;justify-content:space-between}
</style>
"""


def chip(kind: str, label: str | None) -> str:
    kind = kind if kind in ("ok", "part", "no", "na") else "na"
    return f'<span class="chip {kind}">{html.escape(label or STATUS_LABEL[kind])}</span>'


def esc(s) -> str:
    return html.escape(str(s or ""))


def rich(s) -> str:
    """Allow the routine's light inline markup (<b>, <i>) but escape everything else."""
    t = esc(s)
    return re.sub(r"&lt;(/?)(b|i)&gt;", r"<\1\2>", t)


def find_frame(pack: Path, date: str, hhmm: str) -> Path | None:
    hhmm = hhmm.replace(":", "")[:4]
    p = pack / "frames" / f"{date}_{hhmm}.jpg"
    if p.exists():
        return p
    cands = sorted((pack / "frames").glob(f"{date}_*.jpg")) if (pack / "frames").exists() else []
    if not cands:
        return None
    want = int(hhmm[:2]) * 60 + int(hhmm[2:])
    return min(cands, key=lambda c: abs(int(c.name[11:13]) * 60 + int(c.name[13:15]) - want))


def find_snap(pack: Path, events: list, s: dict) -> Path | None:
    if s.get("event_id") and (pack / "snaps" / f"{s['event_id']}.jpg").exists():
        return pack / "snaps" / f"{s['event_id']}.jpg"
    if s.get("time") and events:
        want = hhmm_to_min(s["time"]) * 60 + (int(s["time"][6:8]) if len(s["time"]) >= 8 else 0)
        best = min(events, key=lambda e: abs(hhmm_to_min(e["start"]) * 60 + int(e["start"][6:8]) - want))
        p = pack / "snaps" / f"{best['id']}.jpg"
        return p if p.exists() else None
    return None


def build_html(diario: dict, pack: Path, full: bool, body_class: str = "") -> str:
    d = diario
    date = d.get("date", "")
    events = json.loads((pack / "events.json").read_text(encoding="utf-8")) if (pack / "events.json").exists() else []
    stats = "".join(f'<div class="stat"><b>{esc(s.get("value"))}</b><span>{esc(s.get("label"))}</span></div>' for s in (d.get("stats") or [])[:5])
    entregas = "".join(f'<tr><td><b>{esc(r.get("contrato"))}</b></td><td>{esc(r.get("o_que"))}</td><td>{esc(r.get("previsto"))}</td>'
                       f'<td>{chip(r.get("status","na"), r.get("status_label"))} {rich(r.get("nota"))}</td></tr>' for r in d.get("entregas") or [])
    plano_slim = "".join(f'<tr><td><b>{esc(r.get("item"))}</b></td><td>{chip(r.get("status","na"), r.get("status_label"))}</td><td>{rich(r.get("evidencia_curta") or r.get("evidencia"))}</td></tr>'
                         for r in d.get("plano") or [])
    montage = pack / "montage.jpg"
    if montage.exists():
        antes = f'<figure class="wide">{img_tag(montage, "montagem ontem × hoje", 1500, 72)}<figcaption>{rich(d.get("antes_depois_caption"))}</figcaption></figure>'
    else:
        p2, p3 = pack / "sunset_pos2.jpg", pack / "sunset_pos3.jpg"
        antes = ('<div class="two">' + "".join(f'<figure>{img_tag(p, "pôr do sol", 1100)}</figure>' for p in (p2, p3) if p.exists()) + "</div>"
                 + f'<figcaption>{rich(d.get("antes_depois_caption"))}</figcaption>')
    week = d.get("week")
    eyebrow = d.get("eyebrow") or f"Casa Hangar · Aeródromo Céu Azul, Araquari · semana {week} de obra"
    page1 = f"""
<header>
  <div class="eyebrow">{esc(eyebrow)}</div>
  <h1>Diário de Obra — {esc(d.get("weekday_label") or date)}</h1>
  <p class="verdict"><b>{esc(d.get("headline"))}</b> {rich(d.get("verdict"))}</p>
  <div class="meta">
    <span><b>Jornada</b> {rich(d.get("jornada"))}</span>
    <span><b>Tempo</b> {rich(d.get("tempo"))}</span>
    <span><b>Fontes</b> {rich(d.get("fontes") or "Frigate bnu · timelapse Drive · WhatsApp · tags do portão · Wi-Fi do canteiro · PlanejadoRealizado")}</span>
  </div>
</header>
<section><div class="eyebrow">Em números</div><div class="stats">{stats}</div></section>
<section><h2>Entregas esperadas pelos contratos</h2>
  <table><thead><tr><th style="width:21%">Contrato</th><th style="width:31%">O que</th><th style="width:18%">Previsto</th><th>{esc(date[8:10]+"/"+date[5:7])} na câmera</th></tr></thead><tbody>{entregas}</tbody></table>
</section>
<section><h2>Planejado × realizado — semana {esc(week)} ({esc(d.get("week_range"))})</h2>
  <table><thead><tr><th style="width:29%">Plano da semana (Ênio, PlanejadoRealizado)</th><th style="width:11%">{esc(date[8:10]+"/"+date[5:7])}</th><th>Evidência</th></tr></thead><tbody>{plano_slim}</tbody></table>
</section>
<section><h2>Antes e depois</h2>{antes}</section>
"""
    # no fixed footer: a position:fixed element with a negative bottom offset spilled past the page box and
    # produced a second, footer-only page on every print (1st automatic run, 09/09/2026), which also made the
    # one-page check fall through to the compact classes for nothing.
    head = f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>Diário de Obra {esc(date)}</title>{CSS}</head><body class="{body_class}">"""
    if not full:
        return head + page1 + "</body></html>"
    hours = json.loads((pack / "hist15.json").read_text(encoding="utf-8")) if (pack / "hist15.json").exists() else []
    hh: dict[int, list[int]] = {}
    for b in hours:
        h = b["min"] // 60; hh.setdefault(h, [0, 0]); hh[h][0] += b["person"]; hh[h][1] += b.get("car", 0)
    rows_html = "".join(f"<tr><td>{h:02d}:00–{h+1:02d}:00</td><td class='num'>{p}</td><td class='num'>{c}</td></tr>" for h, (p, c) in sorted(hh.items()))
    tl = d.get("timeline") or {}
    tl_text = "".join(f"<p>{rich(t)}</p>" for t in (tl.get("text") or []))
    frames = ""
    for f in (d.get("frames") or [])[:6]:
        p = find_frame(pack, date, f.get("time", ""))
        frames += f'<figure>{img_tag(p, f.get("time",""), 1100)}<figcaption><span class="t">{esc(f.get("time"))}</span>{rich(f.get("caption"))}</figcaption></figure>'
    snaps = ""
    for s in (d.get("snaps") or [])[:6]:
        p = find_snap(pack, events, s)
        snaps += f'<figure>{img_tag(p, s.get("time",""), 640, 78)}<figcaption><b>{esc(s.get("time"))}</b> {rich(s.get("caption"))}</figcaption></figure>'
    pessoas = "".join(f"<li>{rich(x)}</li>" for x in d.get("pessoas") or [])
    veiculos = "".join(f"<li>{rich(x)}</li>" for x in d.get("veiculos") or [])
    grupo = "".join(f"<li>{rich(x)}</li>" for x in d.get("grupo") or [])
    fontes = "".join(f"<p>{rich(x)}</p>" for x in d.get("fontes_limites") or [])
    proximo = "".join(f"<li>{rich(x)}</li>" for x in d.get("proximo") or [])
    pages = f"""
<section class="pg"><div class="pgtitle">Diário de Obra · {esc(date)} · linha do tempo</div><h2>Linha do tempo</h2>
  <div class="chartbox">{timeline_svg(d, pack)}
    <div class="legend"><span><i style="background:var(--series-p)"></i>eventos de pessoa por 15 min (Frigate)</span><span><i style="background:var(--series-v)"></i>veículo detectado</span>
    <span><i style="background:var(--pine)"></i>passagem de tag no portão (▲ entrada · ▼ saída)</span><span><i style="background:var(--band)"></i>contexto</span></div></div>
  <div class="two" style="margin-top:8pt"><div>{tl_text}</div>
    <div><table style="max-width:260pt"><thead><tr><th>Hora</th><th class="num">Eventos pessoa</th><th class="num">Eventos veículo</th></tr></thead><tbody>{rows_html}</tbody></table></div></div>
</section>
<section class="pg"><div class="pgtitle">Diário de Obra · {esc(date)} · o dia em frames</div><h2>O dia em frames</h2>
  <p>Frames do timelapse (câmera PT, 2304×1296, a cada 15 min).</p><div class="frames">{frames}</div></section>
<section class="pg"><div class="pgtitle">Diário de Obra · {esc(date)} · pessoas e veículos</div><h2>Pessoas e veículos</h2>
  <div class="two"><div><h3>Quem esteve na obra</h3><ul>{pessoas}</ul></div><div><h3>Veículos</h3><ul>{veiculos}</ul></div></div>
  <div class="snaps" style="margin-top:8pt">{snaps}</div></section>
<section class="pg"><div class="pgtitle">Diário de Obra · {esc(date)} · grupo, fontes e pendências</div>
  <h2>O que o grupo disse sobre o dia</h2><ul>{grupo}</ul>{('<div class="note">' + rich(d.get("grupo_nota")) + '</div>') if d.get("grupo_nota") else ""}
  <h2 style="margin-top:10pt">Fontes e limites</h2><div class="two"><div class="src">{fontes}</div><div><h3>Pendências para o próximo diário</h3><ul>{proximo}</ul></div></div>
</section>
"""
    return head + page1 + pages + "</body></html>"


# ---------------------------------------------------------------- PDF / JPG
def _chromium_env() -> dict:
    """Chromium (new headless) builds its temporary profile under the default user-data dir (~/.config/chromium)
    and its NSS db under ~/.local — in the container uid 1000 has no passwd entry, so HOME=/ and is not
    writable: "Failed to create headless user data directory container" (1st automatic run, 09/09/2026).
    Hand it a writable HOME whenever the real one is not."""
    env = dict(os.environ)
    home = env.get("HOME") or ""
    if not (home and os.access(home, os.W_OK)):
        env["HOME"] = tempfile.gettempdir()
    return env


def chromium_pdf(html_path: Path, pdf_path: Path) -> None:
    profile = Path(tempfile.mkdtemp(prefix="chromium-profile-"))   # explicit, writable, removed afterwards
    cmd = [CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--no-pdf-header-footer",
           f"--user-data-dir={profile}", "--run-all-compositor-stages-before-draw", "--virtual-time-budget=15000",
           f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=240, env=_chromium_env())
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if r.returncode or not pdf_path.exists():
        raise RuntimeError(f"chromium rc={r.returncode}: {(r.stderr or r.stdout)[-300:]}")


def pdf_pages(pdf_path: Path) -> int:
    r = subprocess.run(["pdfinfo", str(pdf_path)], capture_output=True, text=True, timeout=60)
    m = re.search(r"Pages:\s+(\d+)", r.stdout)
    return int(m.group(1)) if m else -1


def pdf_to_jpg(pdf_path: Path, jpg_path: Path, dpi: int = 200, max_h: int = 2400, quality: int = 88) -> None:
    prefix = jpg_path.with_suffix("")
    subprocess.run(["pdftoppm", "-r", str(dpi), "-f", "1", "-l", "1", "-jpeg", "-singlefile", str(pdf_path), str(prefix)],
                   check=True, capture_output=True, timeout=120)
    im = Image.open(prefix.with_suffix(".jpg")).convert("RGB")
    if im.height > max_h:
        im = im.resize((round(im.width * max_h / im.height), max_h), Image.LANCZOS)
    im.save(jpg_path, "JPEG", quality=quality, optimize=True)


def render(diario: dict, pack: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    res: dict = {}
    # resumo: must be exactly one page — tighten twice, then scale the page down step by step
    # (09/09/2026: 8 plan rows + 6 deliveries still spilled a few points into a 2nd page at compact2)
    for cls in ("", "compact", "compact2", "compact2 z94", "compact2 z88", "compact2 z82", "compact2 z76"):
        (out / "resumo.html").write_text(build_html(diario, pack, False, cls), encoding="utf-8")
        chromium_pdf(out / "resumo.html", out / "resumo.pdf")
        n = pdf_pages(out / "resumo.pdf")
        res["resumo_pages"], res["resumo_class"] = n, cls or "normal"
        if n == 1:
            break
    (out / "completo.html").write_text(build_html(diario, pack, True), encoding="utf-8")
    chromium_pdf(out / "completo.html", out / "completo.pdf")
    res["completo_pages"] = pdf_pages(out / "completo.pdf")
    pdf_to_jpg(out / "resumo.pdf", out / "resumo.jpg")
    res["resumo_jpg_kb"] = round((out / "resumo.jpg").stat().st_size / 1024)
    return res


if __name__ == "__main__":  # local test: python diario_render.py diario.json pack/ out/
    import sys
    dj, pk, ot = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    print(json.dumps(render(json.loads(dj.read_text(encoding="utf-8")), pk, ot), indent=1))
