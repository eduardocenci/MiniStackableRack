#!/usr/bin/env python3
"""canteiro-sunset-compare — "Dia de Trabalho": grade diária do pôr do sol da obra ARA.

Toda segunda–sexta às 20:10 America/Sao_Paulo (container canteiro-sunset-compare
no bnu-raspberrypi, supercronic com TZ=America/Sao_Paulo — ver
docker/canteiro-jobs/; o "dia" é calculado aqui com ZoneInfo, nunca com a
hora local) baixa do Google Drive as fotos do pôr do sol nas TRÊS posições
da lente PT e monta a grade 2×3 (layout decidido por Eduardo 31/08/2026):

  linha 1 (dia anterior):  posicao3 | posicao1 | posicao2
  linha 2 (dia corrente):  posicao3 | posicao1 | posicao2

Colunas em ordem esquerda→centro→direita da obra (pos3 = rua/estoque,
pos1 = guarda/fundação, pos2 = bancada/pátio), 800 px por célula →
2400×900. Dia anterior = ontem; segunda compara com SEXTA (exceção única
31/08/2026: usa domingo 30/08, primeiro dia com as três posições). Fotos
vêm do timelapse do ara Pi (upload às 20:00 — daí a folga de 10 min +
retries). A grade é arquivada em `DiaDeTrabalho/YYYY-MM-DD.jpg` no topo do
Timelapse (movido de posicao1/DiaDeTrabalho em 31/08/2026) e enviada no
WhatsApp via WAHA `sendImage` (base64 — funciona neste Core build, mesmo
padrão do canteiro-watchdog).

A câmera grava data/hora dentro de cada frame, então a própria grade
carrega os carimbos dos dois dias em cada célula.

Config: ~/canteiro-jobs/env/canteiro-sunset-compare.env (env_file do
compose) — WAHA_URL, WAHA_KEY, WAHA_SESSION, GROUP_JID, TEST_JID,
RCLONE_REMOTE (default ceuazul:Timelapse; remote no rclone.conf de
~eduardocenci montado no container, token da mesma conta Google do upload
do ara Pi).

Teste manual (vai ao TEST_JID — grupo Casa SmokeTests):
  docker exec canteiro-sunset-compare python3 /app/canteiro-sunset-compare.py --test
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
    """Baixa o pôr do sol de `pos` no dia `day`; retorna caminho local ou None."""
    sub = os.path.join(dest_dir, f"{pos}-{day:%Y%m%d}")
    os.makedirs(sub, exist_ok=True)
    pat = day.strftime("%Y-%m-%d") + "_*.jpg"
    for i in range(retries):
        r = subprocess.run(["rclone", "copy", f"{REMOTE}/{pos}/por-do-sol",
                            sub, "--include", pat],
                           capture_output=True, timeout=180)
        if r.returncode != 0:
            print(f"rclone copy {pos} rc={r.returncode}: "
                  f"{r.stderr.decode(errors='replace')[-300:]}", file=sys.stderr)
        found = sorted(f for f in os.listdir(sub)
                       if f.startswith(day.strftime("%Y-%m-%d")))
        if found:
            return os.path.join(sub, found[-1])
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


def main():
    test = len(sys.argv) > 1 and sys.argv[1] == "--test"
    chat = (sys.argv[2] if len(sys.argv) > 2 else TEST_JID) if test else GROUP_JID
    prefix = "[TESTE] " if test else ""
    now = datetime.now(TZ)
    if not test and now.weekday() >= 5:  # recuperações de fim de semana não enviam
        print("fim de semana, sem comparacao")
        return 0
    today = now.date()
    yesterday = previous_workday(today)

    with tempfile.TemporaryDirectory(prefix="sunset-compare-") as tmp:
        cells, faltam = [], []
        for day, retries in ((yesterday, 1), (today, 1 if test else RETRIES)):
            for pos in POSITIONS:
                f = rclone_fetch(pos, day, tmp, retries)
                if f:
                    cells.append(f)
                else:
                    faltam.append(f"{pos} {day:%d/%m}")
        if faltam:
            st = send_text(chat, prefix + "⚠️ Grade do Dia de Trabalho não saiu hoje — "
                           f"faltando no Drive: {', '.join(faltam)}.")
            print(f"fotos faltando ({', '.join(faltam)}), aviso enviado, HTTP {st}")
            return 1
        out = os.path.join(tmp, "grade.jpg")
        if not montage_grid(cells, out):
            st = send_text(chat, prefix + "⚠️ Grade do Dia de Trabalho falhou na montagem (ffmpeg).")
            print(f"montagem falhou, aviso enviado, HTTP {st}")
            return 1
        try:  # cópia de inspeção, sobrescrita a cada envio
            import shutil
            shutil.copyfile(out, "/tmp/ultima-comparacao.jpg")
        except OSError:
            pass
        # arquivo permanente da grade no Drive (topo do Timelapse)
        r = subprocess.run(
            ["rclone", "copyto", out,
             f"{REMOTE}/DiaDeTrabalho/{today:%Y-%m-%d}.jpg"],
            capture_output=True, timeout=180)
        if r.returncode != 0:
            print("rclone copyto DiaDeTrabalho falhou: "
                  + r.stderr.decode(errors="replace")[-200:], file=sys.stderr)
        caption = prefix + f"🌇 *Dia de Trabalho ({today:%d/%m})*"
        st = send_image(chat, out, caption)
        print(f"grade {yesterday:%d/%m} vs {today:%d/%m} enviada, HTTP {st} -> {chat}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
