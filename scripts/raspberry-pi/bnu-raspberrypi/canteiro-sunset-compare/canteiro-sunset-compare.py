#!/usr/bin/env python3
"""canteiro-sunset-compare — grades do pôr do sol da obra ARA (WhatsApp).

Todo dia às 20:10 America/Sao_Paulo (container canteiro-sunset-compare no
bnu-raspberrypi, supercronic com TZ=America/Sao_Paulo — ver
docker/canteiro-jobs/; o "dia" é calculado aqui com ZoneInfo, nunca com a
hora local) o script decide o que emitir — cada produto é uma grade 2×2,
o MOSAICO das duas lentes antes e depois (decisão Eduardo 14/11/2026: a
câmera PT não se move mais; lente fixa à esquerda, olhando a rua, lente PT
à direita, parada na posição de repouso — juntas, "quase 180°"):

    linha 1 = baseline   [ lentefixa | posicao1 ]
    linha 2 = hoje       [ lentefixa | posicao1 ]      1200 px/célula → 2400×1350

  🌇 Dia de Trabalho (seg–sex)    baseline = ontem; segunda usa SEXTA
  🏗️ Semana de Trabalho (sexta)   baseline = sexta anterior; legenda mostra
                                  seg–sex da semana
  📆 Mês de Trabalho (dia 25,     baseline = dia 26 do mês anterior —
     qualquer dia da semana)      janela de medição da empreiteira 26→25

Até 13/11/2026 a grade era 2×3 (posicao3 | posicao1 | posicao2 — a lente PT
em três enquadramentos, alinhados por software pelo pilar do galpão,
pilar_align.py). Sem excursões não há tremor a corrigir: o alinhamento saiu.
Uma baseline anterior a 14/11 mostra o enquadramento antigo das lentes — a
legenda avisa.

Fotos vêm do timelapse do ara Pi (upload às 20:00 — daí a folga de 10 min
+ retries; cache local reusa downloads entre produtos da mesma execução).
A fonte é `<pos>/Luminancia125/` — a janela do pôr do sol cujo frame de
pos1 tem luminância mais perto de 125, escolhida no ara ao fim da sequência
(decisão Eduardo 11/09/2026: a luz em T varia com o clima, 100 a 128; com a
seleção fica entre 123 e 126). Se um dia não tiver a pasta (seleção não
rodou), cai para `por-do-sol` e avisa no SmokeTests.
Cada grade é arquivada no topo do Timelapse em
`<DiaDeTrabalho|SemanaDeTrabalho|MesDeTrabalho>/YYYY-MM-DD.jpg` e enviada
no WhatsApp via WAHA `sendImage` (base64 — funciona neste Core build,
mesmo padrão do canteiro-watchdog). Cada célula leva uma etiqueta DD/MM.

Config: ~/canteiro-jobs/env/canteiro-sunset-compare.env (env_file do
compose) — WAHA_URL, WAHA_KEY, WAHA_SESSION, GROUP_JID, TEST_JID,
RCLONE_REMOTE (default ceuazul:Timelapse; remote no rclone.conf de
~eduardocenci montado no container, token da mesma conta Google do upload
do ara Pi).

Teste manual (vai ao TEST_JID — grupo Casa SmokeTests):
  docker exec canteiro-sunset-compare python3 /app/canteiro-sunset-compare.py --test [all|daily|semana|mes] [chatId]
  (+ --as-of AAAA-MM-DD refaz a grade daquele dia e a ARQUIVA no Drive; + --no-archive só envia)
  (sem filtro: testa os produtos que valeriam hoje; com filtro, força-os)
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")

WAHA_URL     = os.environ.get("WAHA_URL", "http://10.1.1.126:3000")
WAHA_KEY     = os.environ["WAHA_KEY"]
WAHA_SESSION = os.environ.get("WAHA_SESSION", "default")
GROUP_JID    = os.environ["GROUP_JID"]
TEST_JID     = os.environ.get("TEST_JID", GROUP_JID)
REMOTE       = os.environ.get("RCLONE_REMOTE", "ceuazul:Timelapse")
POSITIONS    = ["lentefixa", "posicao1"]   # colunas do mosaico: lente fixa (esq., rua) | lente PT (dir.)
MOSAIC_SINCE = date(2026, 11, 14)          # lentes reposicionadas; antes disso o enquadramento era outro
LUM_FOLDER   = "Luminancia125"   # janela escolhida por luminancia (ara: timelapse-capture luminancia)
# Sem Luminancia125 naquele dia (a selecao nao rodou — sequencia morta por
# queda de energia, 27/10/2026): tenta as janelas do por do sol nesta ordem.
FALLBACK_WINS = ["por-do-sol", "por-do-sol-menos-10min", "por-do-sol-mais-10min",
                 "por-do-sol-menos-20min", "por-do-sol-mais-20min"]
FALLBACK_WIN = FALLBACK_WINS[0]
# Linha de cima (baseline) sem as fotos das duas lentes: recua dia a dia ate este limite por
# um dia completo, em vez de nao emitir a grade (28/10/2026 nao saiu porque
# 27/10 nao tinha fotos; a legenda diz qual dia foi usado).
BASELINE_BACK_DAYS = 7
CELL_W       = 1200
RETRIES      = 8      # fotos de hoje: espera o upload das 20:00 até ~20:35
RETRY_WAIT_S = 180


def rclone_fetch(pos, day, dest_dir, retries=1, window=LUM_FOLDER):
    """Baixa a foto de `pos` no dia `day` da pasta `window` (Luminancia125 por
    padrão); retorna caminho local ou None. Checa o cache local primeiro —
    vários produtos na mesma execução reusam o que já foi baixado."""
    sub = os.path.join(dest_dir, f"{pos}-{window}-{day:%Y%m%d}")
    os.makedirs(sub, exist_ok=True)
    pat = day.strftime("%Y-%m-%d") + "_*.jpg"

    def found():
        hits = sorted(f for f in os.listdir(sub)
                      if f.startswith(day.strftime("%Y-%m-%d")))
        return os.path.join(sub, hits[-1]) if hits else None

    for i in range(retries):
        f = found()
        if f:
            return f
        r = subprocess.run(["rclone", "copy", f"{REMOTE}/{pos}/{window}",
                            sub, "--include", pat],
                           capture_output=True, timeout=180)
        if r.returncode != 0:
            print(f"rclone copy {pos}/{window} rc={r.returncode}: "
                  f"{r.stderr.decode(errors='replace')[-300:]}", file=sys.stderr)
        f = found()
        if f:
            return f
        if i + 1 < retries:
            print(f"{pos}/{window} de {day:%d/%m} ainda nao esta no Drive; aguardando {RETRY_WAIT_S}s")
            time.sleep(RETRY_WAIT_S)
    return None


def montage_grid(cells, days, out):
    """Grade 2x2 em PIL: `cells` = linha1(lentefixa, posicao1) + linha2(idem);
    `days` = (dia de cima, dia de baixo) para a etiqueta DD/MM de cada célula."""
    from PIL import Image, ImageDraw, ImageFont
    n = len(POSITIONS)
    ims = [Image.open(c).convert("RGB") for c in cells]
    W, H = ims[0].size
    tw = CELL_W
    th = round(CELL_W * H / W)
    canvas = Image.new("RGB", (tw * n, th * 2), (18, 18, 18))
    dr = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default(size=34)
    except Exception:
        font = ImageFont.load_default()
    for idx, im in enumerate(ims):
        r, c = divmod(idx, n)
        canvas.paste(im.resize((tw, th)), (c * tw, r * th))
        tag = days[r].strftime("%d/%m")
        tx, ty = c * tw + tw - 104, r * th + 12     # canto superior direito: o carimbo
        dr.rectangle([tx - 6, ty - 4, tx + 92, ty + 38], fill=(0, 0, 0))   # da câmera fica embaixo
        dr.text((tx, ty), tag, fill=(255, 220, 0), font=font)
    canvas.save(out, quality=88)
    return os.path.exists(out) and os.path.getsize(out) > 100_000


def waha_post(endpoint, payload):
    req = urllib.request.Request(
        f"{WAHA_URL}/api/{endpoint}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Api-Key": WAHA_KEY},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.status


def send_text(chat, text):
    return waha_post("sendText", {"session": WAHA_SESSION, "chatId": chat, "text": text})


def send_image(chat, path, caption):
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    return waha_post("sendImage", {
        "session": WAHA_SESSION, "chatId": chat,
        "file": {"mimetype": "image/jpeg",
                 "filename": os.path.basename(path), "data": b64},
        "caption": caption,
    })


def previous_workday(today):
    return today - timedelta(days=3 if today.weekday() == 0 else 1)


def week_baseline(today):
    return today - timedelta(days=7)  # sexta anterior


def month_baseline(today):
    # Mês de medição da empreiteira: 26 do mês anterior → 25 do atual.
    prev_last = today.replace(day=1) - timedelta(days=1)
    return prev_last.replace(day=26)


def month_caption_range(today):
    prev_last = today.replace(day=1) - timedelta(days=1)
    return f"26/{prev_last:%m} - 25/{today:%m}"


def fetch_day(day, tmp, retries):
    """2 celulas (lentefixa, posicao1) de um dia: Luminancia125, senao a primeira janela
    do por do sol que existir. Devolve (cells, faltam, fallback)."""
    cells, faltam, fallback = [], [], []
    for pos in POSITIONS:
        f = rclone_fetch(pos, day, tmp, retries)
        if not f:
            for w in FALLBACK_WINS:
                f = rclone_fetch(pos, day, tmp, 1, window=w)
                if f:
                    fallback.append(f"{pos} {day:%d/%m} ({w})")
                    break
        if f:
            cells.append(f)
        else:
            faltam.append(f"{pos} {day:%d/%m}")
    return cells, faltam, fallback


def make_grid_cells(day_top, day_bottom, tmp, retries_bottom):
    """4 celulas na ordem linha1(lentefixa,posicao1)+linha2(idem). Fonte Luminancia125;
    sem ela naquele dia, cai para a primeira janela do por do sol que existir
    (listado em `fallback`). Linha de cima incompleta: recua ate
    BASELINE_BACK_DAYS por um dia com as duas fotos. Devolve
    (cells, faltam, fallback, dia_de_cima_usado)."""
    top_used, top_cells, top_fb, first_faltam = None, [], [], []
    d = day_top
    for _ in range(BASELINE_BACK_DAYS + 1):
        cells, faltam, fb = fetch_day(d, tmp, 1)
        if not faltam:
            top_used, top_cells, top_fb = d, cells, fb
            break
        if d == day_top:
            first_faltam = faltam
        print(f"linha de cima: {d:%d/%m} incompleta ({', '.join(faltam)}), recuando um dia")
        d -= timedelta(days=1)
    if top_used is None:
        return [], first_faltam, [], day_top
    bot_cells, bot_faltam, bot_fb = fetch_day(day_bottom, tmp, retries_bottom)
    return top_cells + bot_cells, bot_faltam, top_fb + bot_fb, top_used


def main():
    argv = sys.argv[1:]
    no_archive = "--no-archive" in argv   # teste que nao sobrescreve a grade arquivada no Drive
    argv = [a for a in argv if a != "--no-archive"]
    as_of = None   # --as-of YYYY-MM-DD: "hoje" para testes/backfill (so com --test)
    if "--as-of" in argv:
        i = argv.index("--as-of")
        as_of = date.fromisoformat(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    test = bool(argv) and argv[0] == "--test"
    rest = argv[1:] if test else []
    which = rest[0] if rest and rest[0] in ("all", "daily", "semana", "mes") else None
    chat_arg = (rest[1] if len(rest) > 1 else None) if which else (rest[0] if rest else None)
    chat = (chat_arg or TEST_JID) if test else GROUP_JID
    prefix = "[TESTE] " if test else ""
    now = datetime.now(TZ)
    today = as_of if (as_of and test) else now.date()
    wd = today.weekday()

    # (nome, dia_de_cima, legenda, pasta de arquivo no Drive)
    products = []
    if wd < 5 or (test and which in ("all", "daily")):
        products.append(("daily", previous_workday(today),
                         f"🌇 *Dia de Trabalho ({today:%d/%m})*", "DiaDeTrabalho"))
    if wd == 4 or (test and which in ("all", "semana")):
        monday = today - timedelta(days=wd if wd < 5 else 0)
        products.append(("semana", week_baseline(today),
                         f"🏗️ *Semana de Trabalho ({monday:%d/%m} - {today:%d/%m})*",
                         "SemanaDeTrabalho"))
    if today.day == 25 or (test and which in ("all", "mes")):
        products.append(("mes", month_baseline(today),
                         f"📆 *Mês de Trabalho ({month_caption_range(today)})*",
                         "MesDeTrabalho"))
    if not products:
        print("nada a enviar hoje (fim de semana sem dia 25)")
        return 0

    rc = 0
    with tempfile.TemporaryDirectory(prefix="sunset-compare-") as tmp:
        for nome, day_top, caption, pasta in products:
            titulo = caption.split("*")[1]
            cells, faltam, fallback, top_used = make_grid_cells(
                day_top, today, tmp, 1 if test else RETRIES)
            if top_used != day_top:
                caption += (f"\n_linha de cima: {top_used:%d/%m} — "
                            f"{day_top:%d/%m} sem fotos no Drive_")
                st = send_text(TEST_JID, prefix + f"ℹ️ {titulo}: {day_top:%d/%m} sem as fotos "
                               f"no Drive — linha de cima = {top_used:%d/%m}.")
                print(f"{nome}: baseline {day_top:%d/%m} -> {top_used:%d/%m}, aviso HTTP {st}")
                day_top = top_used
            if fallback:
                st = send_text(TEST_JID, prefix + f"ℹ️ {titulo}: sem {LUM_FOLDER} para "
                               f"{', '.join(fallback)} — usei a janela entre parenteses.")
                print(f"{nome}: fallback de janela em {', '.join(fallback)}, aviso HTTP {st}")
            if faltam:
                st = send_text(chat, prefix + f"⚠️ {titulo} não saiu — "
                               f"faltando no Drive: {', '.join(faltam)}.")
                print(f"{nome}: fotos faltando ({', '.join(faltam)}), aviso enviado, HTTP {st}")
                rc = 1
                continue
            if day_top < MOSAIC_SINCE <= today:
                caption += (f"\n_linha de cima ({day_top:%d/%m}): enquadramento antigo — "
                            f"as lentes foram reposicionadas em {MOSAIC_SINCE:%d/%m}_")
            out = os.path.join(tmp, f"{nome}.jpg")
            try:
                ok = montage_grid(cells, (day_top, today), out)
            except Exception as e:
                print(f"{nome}: erro na montagem: {e}", file=sys.stderr)
                ok = False
            if not ok:
                st = send_text(chat, prefix + f"⚠️ {titulo} falhou na montagem.")
                print(f"{nome}: montagem falhou, aviso enviado, HTTP {st}")
                rc = 1
                continue
            try:  # cópia de inspeção, sobrescrita a cada envio
                import shutil
                shutil.copyfile(out, f"/tmp/ultima-{nome}.jpg")
            except OSError:
                pass
            r = None if no_archive else subprocess.run(
                ["rclone", "copyto", out, f"{REMOTE}/{pasta}/{today:%Y-%m-%d}.jpg"],
                capture_output=True, timeout=180)
            if r is not None and r.returncode != 0:
                print(f"rclone copyto {pasta} falhou: "
                      + r.stderr.decode(errors="replace")[-200:], file=sys.stderr)
            st = send_image(chat, out, prefix + caption)
            print(f"{nome}: {day_top:%d/%m} vs {today:%d/%m} "
                  f"(mosaico 2x2) enviada, HTTP {st} -> {chat}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
