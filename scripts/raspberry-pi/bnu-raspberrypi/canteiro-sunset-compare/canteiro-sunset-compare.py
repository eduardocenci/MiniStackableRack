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

A câmera grava data/hora dentro de cada frame, então a própria grade
carrega os carimbos dos dois dias em cada célula.

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
    if today == date(2026, 9, 25):   # 1º mês: sem imagens de 26/08 (Eduardo: usar 31/08)
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
            if not montage_grid(cells, out):
                st = send_text(chat, prefix + f"⚠️ {titulo} falhou na montagem (ffmpeg).")
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
            print(f"{nome}: {day_top:%d/%m} vs {today:%d/%m} enviada, HTTP {st} -> {chat}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
