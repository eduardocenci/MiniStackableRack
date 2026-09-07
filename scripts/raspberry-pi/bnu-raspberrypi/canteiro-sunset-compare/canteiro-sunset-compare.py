#!/usr/bin/env python3
"""canteiro-sunset-compare — grades do pôr do sol da obra ARA (WhatsApp).

Todo dia às 20:10 America/Sao_Paulo (container canteiro-sunset-compare no
bnu-raspberrypi, supercronic com TZ=America/Sao_Paulo — ver
docker/canteiro-jobs/; o "dia" é calculado aqui com ZoneInfo, nunca com a
hora local) o script decide o que emitir — cada produto é uma grade 2×3
(linha 1 = baseline, linha 2 = hoje; colunas posicao3 | posicao1 |
posicao2 = esquerda→centro→direita da obra; 800 px/célula → 2400×900):

  🌇 Dia de Trabalho (seg–sex)    baseline = ontem; segunda usa SEXTA
                                  (exceção 31/08/2026: domingo 30/08)
  🏗️ Semana de Trabalho (sexta)   baseline = sexta anterior; legenda mostra
                                  seg–sex da semana (exceção 04/09/2026:
                                  domingo 30/08)
  📆 Mês de Trabalho (dia 25,     baseline = dia 26 do mês anterior —
     qualquer dia da semana)      janela de medição da empreiteira 26→25
                                  (exceção 25/09/2026: 31/08, decisão
                                  Eduardo — não há imagens de 26/08)

Fotos vêm do timelapse do ara Pi (upload às 20:00 — daí a folga de 10 min
+ retries; cache local reusa downloads entre produtos da mesma execução).
Cada grade é arquivada no topo do Timelapse em
`<DiaDeTrabalho|SemanaDeTrabalho|MesDeTrabalho>/YYYY-MM-DD.jpg` e enviada
no WhatsApp via WAHA `sendImage` (base64 — funciona neste Core build,
mesmo padrão do canteiro-watchdog).

Desde 07/09/2026 a grade é ALINHADA por software: mede-se o deslocamento
do pilar do galpão na pos1 de cada dia (pilar_align, mesma medição da
re-âncora do ara) e desloca+recorta as 3 colunas daquele dia por esse
offset, zerando o tremor entre dias (pos2/pos3 herdam o offset da guarda).
Sem numpy/pillow ou sem as referências do Drive, cai no modo antigo
(ffmpeg, sem alinhar). Como o recorte come a faixa inferior onde a câmera
grava o carimbo, cada célula recebe uma etiqueta DD/MM desenhada.

Config: ~/canteiro-jobs/env/canteiro-sunset-compare.env (env_file do
compose) — WAHA_URL, WAHA_KEY, WAHA_SESSION, GROUP_JID, TEST_JID,
RCLONE_REMOTE (default ceuazul:Timelapse; remote no rclone.conf de
~eduardocenci montado no container, token da mesma conta Google do upload
do ara Pi).

Teste manual (vai ao TEST_JID — grupo Casa SmokeTests):
  docker exec canteiro-sunset-compare python3 /app/canteiro-sunset-compare.py --test [all|daily|semana|mes] [chatId]
  (sem filtro: testa os produtos que valeriam hoje; com filtro, força-os)
"""
import base64
import glob
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pilar_align   # alinhamento por software (mede o pilar, desloca+recorta)

TZ = ZoneInfo("America/Sao_Paulo")

WAHA_URL     = os.environ.get("WAHA_URL", "http://10.1.1.126:3000")
WAHA_KEY     = os.environ["WAHA_KEY"]
WAHA_SESSION = os.environ.get("WAHA_SESSION", "default")
GROUP_JID    = os.environ["GROUP_JID"]
TEST_JID     = os.environ.get("TEST_JID", GROUP_JID)
REMOTE       = os.environ.get("RCLONE_REMOTE", "ceuazul:Timelapse")
POSITIONS    = ["posicao3", "posicao1", "posicao2"]   # ordem das colunas
CELL_W       = 800
RETRIES      = 8      # fotos de hoje: espera o upload das 20:00 até ~20:35
RETRY_WAIT_S = 180


def rclone_fetch(pos, day, dest_dir, retries=1):
    """Baixa o pôr do sol de `pos` no dia `day`; retorna caminho local ou None.
    Checa o cache local primeiro — vários produtos na mesma execução reusam
    o que já foi baixado."""
    sub = os.path.join(dest_dir, f"{pos}-{day:%Y%m%d}")
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
        r = subprocess.run(["rclone", "copy", f"{REMOTE}/{pos}/por-do-sol",
                            sub, "--include", pat],
                           capture_output=True, timeout=180)
        if r.returncode != 0:
            print(f"rclone copy {pos} rc={r.returncode}: "
                  f"{r.stderr.decode(errors='replace')[-300:]}", file=sys.stderr)
        f = found()
        if f:
            return f
        if i + 1 < retries:
            print(f"{pos} de {day:%d/%m} ainda nao esta no Drive; aguardando {RETRY_WAIT_S}s")
            time.sleep(RETRY_WAIT_S)
    return None


def montage_grid(cells, out):
    """Grade 2x3: `cells` = 6 caminhos na ordem linha1(p3,p1,p2)+linha2(idem)."""
    args = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    for c in cells:
        args += ["-i", c]
    scaled = "".join(f"[{i}]scale={CELL_W}:-2[s{i}];" for i in range(6))
    filt = (scaled
            + "[s0][s1][s2]hstack=inputs=3[r1];"
            + "[s3][s4][s5]hstack=inputs=3[r2];"
            + "[r1][r2]vstack=inputs=2")
    args += ["-filter_complex", filt, "-frames:v", "1", "-q:v", "3", "-y", out]
    r = subprocess.run(args, timeout=120)
    return r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 100_000


PAD_PX       = 28     # folga alem do maior offset, ao recortar
MAX_MARGIN   = 320    # recorte maximo por lado (protege o enquadramento)


def _montage_aligned(cells, row_offs, days, out):
    """Monta a grade 2x3 em PIL alinhando cada linha pelo offset do pilar.
    `row_offs` = [(sx,sy)|None, (sx,sy)|None] (linha de cima, linha de baixo);
    o offset da linha vale para as 3 colunas (p3,p1,p2). Recorta a janela de
    referencia deslocada de (sx,sy) e etiqueta DD/MM. Retorna True/False."""
    from PIL import Image, ImageDraw, ImageFont
    ims = [Image.open(c).convert("RGB") for c in cells]
    W, H = ims[0].size
    shifts = []
    for ri in range(2):
        shifts += [row_offs[ri] if row_offs[ri] else (0, 0)] * 3
    mags = [max(abs(o[0]), abs(o[1])) for o in row_offs if o]
    M = min(MAX_MARGIN, (max(mags) + PAD_PX) if mags else PAD_PX)
    cw, ch = W - 2 * M, H - 2 * M
    tw = CELL_W
    th = round(CELL_W * ch / cw)
    canvas = Image.new("RGB", (tw * 3, th * 2), (18, 18, 18))
    dr = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default(size=30)
    except Exception:
        font = ImageFont.load_default()
    for idx, (im, (sx, sy)) in enumerate(zip(ims, shifts)):
        left = max(0, min(M + sx, W - cw))
        top = max(0, min(M + sy, H - ch))
        tile = im.crop((left, top, left + cw, top + ch)).resize((tw, th))
        r, c = divmod(idx, 3)
        canvas.paste(tile, (c * tw, r * th))
        tag = days[0 if idx < 3 else 1].strftime("%d/%m")
        tx, ty = c * tw + 10, r * th + th - 40
        dr.rectangle([tx - 5, ty - 4, tx + 82, ty + 32], fill=(0, 0, 0))
        dr.text((tx, ty), tag, fill=(255, 220, 0), font=font)
    canvas.save(out, quality=88)
    return os.path.exists(out) and os.path.getsize(out) > 50_000


def build_montage(cells, day_top, day_bottom, ref_dir, out):
    """Grade alinhada quando da (numpy/pillow + refs + medicao valida da
    pos1); senao a grade ffmpeg de sempre. Retorna (ok, alinhada)."""
    if ref_dir and pilar_align.available():
        offs = []
        for idx, day in ((1, day_top), (4, day_bottom)):   # cells[1]/[4] = pos1
            r = None
            try:
                r = pilar_align.measure_valid(cells[idx], ref_dir)
            except Exception as e:
                print(f"align: erro medindo pos1 {day:%d/%m}: {e}", file=sys.stderr)
            o = pilar_align.sane_offset(r)
            offs.append(o)
            if o:
                print(f"align {day:%d/%m}: offset=({o[0]:+d},{o[1]:+d}) "
                      f"psr={r[2]:.0f} ref={r[3]}")
            else:
                print(f"align {day:%d/%m}: sem shift (medicao invalida/implausivel)")
        if any(offs):
            if _montage_aligned(cells, offs, (day_top, day_bottom), out):
                return True, True
            print("align: montagem alinhada falhou, caindo no ffmpeg", file=sys.stderr)
    return montage_grid(cells, out), False


def fetch_refs(dest):
    """Baixa as referencias posicao1-*.jpg do Drive (ceuazul:Timelapse/ref)
    para `dest`; retorna `dest` ou None."""
    os.makedirs(dest, exist_ok=True)
    r = subprocess.run(["rclone", "copy", f"{REMOTE}/ref", dest,
                        "--include", "posicao1-*.jpg"],
                       capture_output=True, timeout=180)
    if r.returncode != 0:
        print("align: rclone das refs falhou: "
              + r.stderr.decode(errors="replace")[-200:], file=sys.stderr)
    if glob.glob(os.path.join(dest, "posicao1-*.jpg")):
        return dest
    return None


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
    if today == date(2026, 8, 31):   # exceção única: sexta 28/08 não tem pos2/pos3
        return date(2026, 8, 30)
    return today - timedelta(days=3 if today.weekday() == 0 else 1)


def week_baseline(today):
    if today == date(2026, 9, 4):    # 1ª semana: sexta 28/08 não tem pos2/pos3
        return date(2026, 8, 30)
    return today - timedelta(days=7)  # sexta anterior


def month_baseline(today):
    # Mês de medição da empreiteira: 26 do mês anterior → 25 do atual.
    if today == date(2026, 9, 25):   # 1º mês: sem imagens de 26/08. Eduardo
        # avaliou 30 e 31/08 (ambas com pos1 deslocada — era pré-reâncora) e
        # escolheu 31/08 como a melhor. Não trocar para 30/08.
        return date(2026, 8, 31)
    prev_last = today.replace(day=1) - timedelta(days=1)
    return prev_last.replace(day=26)


def month_caption_range(today):
    prev_last = today.replace(day=1) - timedelta(days=1)
    return f"26/{prev_last:%m} - 25/{today:%m}"


def make_grid_cells(day_top, day_bottom, tmp, retries_bottom):
    cells, faltam = [], []
    for day, retries in ((day_top, 1), (day_bottom, retries_bottom)):
        for pos in POSITIONS:
            f = rclone_fetch(pos, day, tmp, retries)
            if f:
                cells.append(f)
            else:
                faltam.append(f"{pos} {day:%d/%m}")
    return cells, faltam


def main():
    argv = sys.argv[1:]
    test = bool(argv) and argv[0] == "--test"
    rest = argv[1:] if test else []
    which = rest[0] if rest and rest[0] in ("all", "daily", "semana", "mes") else None
    chat_arg = (rest[1] if len(rest) > 1 else None) if which else (rest[0] if rest else None)
    chat = (chat_arg or TEST_JID) if test else GROUP_JID
    prefix = "[TESTE] " if test else ""
    now = datetime.now(TZ)
    today = now.date()
    wd = now.weekday()

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
        ref_dir = fetch_refs(os.path.join(tmp, "ref")) if pilar_align.available() else None
        if pilar_align.available() and not ref_dir:
            print("align: refs indisponiveis, grades sairao sem alinhamento", file=sys.stderr)
        for nome, day_top, caption, pasta in products:
            titulo = caption.split("*")[1]
            cells, faltam = make_grid_cells(day_top, today, tmp, 1 if test else RETRIES)
            if faltam:
                st = send_text(chat, prefix + f"⚠️ {titulo} não saiu — "
                               f"faltando no Drive: {', '.join(faltam)}.")
                print(f"{nome}: fotos faltando ({', '.join(faltam)}), aviso enviado, HTTP {st}")
                rc = 1
                continue
            out = os.path.join(tmp, f"{nome}.jpg")
            ok, aligned = build_montage(cells, day_top, today, ref_dir, out)
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
            r = subprocess.run(["rclone", "copyto", out,
                                f"{REMOTE}/{pasta}/{today:%Y-%m-%d}.jpg"],
                               capture_output=True, timeout=180)
            if r.returncode != 0:
                print(f"rclone copyto {pasta} falhou: "
                      + r.stderr.decode(errors="replace")[-200:], file=sys.stderr)
            st = send_image(chat, out, prefix + caption)
            print(f"{nome}: {day_top:%d/%m} vs {today:%d/%m} "
                  f"({'alinhada' if aligned else 'sem alinhamento'}) enviada, HTTP {st} -> {chat}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
