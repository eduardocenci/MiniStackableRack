#!/usr/bin/env python3
"""canteiro-sunset-compare — comparação diária do pôr do sol da obra ARA.

Toda segunda–sexta às 20:10 America/Sao_Paulo (container canteiro-sunset-compare
no bnu-raspberrypi, supercronic com TZ=America/Sao_Paulo — ver
docker/canteiro-jobs/; até 2026-08-29 era um systemd timer. O "dia" é
calculado aqui com ZoneInfo, nunca com a hora local) baixa do Google
Drive as fotos de `posicao1/por-do-sol/` do ÚLTIMO DIA ÚTIL (segunda
compara com sexta; demais dias, com ontem) e de HOJE (timelapse do
ara Pi, upload às 20:00 — daí a folga de 10 min + retries), empilha
verticalmente com ffmpeg vstack (ontem no TOPO, hoje EMBAIXO, largura
1600 px), arquiva a montagem em `posicao1/DiaDeTrabalho/YYYY-MM-DD.jpg`
no Drive e manda no WhatsApp via WAHA `sendImage` (base64 — funciona neste
Core build, mesmo padrão do canteiro-watchdog).

A câmera grava data/hora dentro de cada frame, então a própria montagem
carrega os carimbos dos dois dias.

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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")

WAHA_URL     = os.environ.get("WAHA_URL", "http://10.1.1.126:3000")
WAHA_KEY     = os.environ["WAHA_KEY"]
WAHA_SESSION = os.environ.get("WAHA_SESSION", "default")
GROUP_JID    = os.environ["GROUP_JID"]
TEST_JID     = os.environ.get("TEST_JID", GROUP_JID)
REMOTE       = os.environ.get("RCLONE_REMOTE", "ceuazul:Timelapse")
FOLDER       = "posicao1/por-do-sol"
RETRIES      = 8      # foto de hoje: espera o upload das 20:00 até ~20:35
RETRY_WAIT_S = 180


def rclone_fetch(day, dest_dir, retries=1):
    """Baixa a foto do pôr do sol de `day`; retorna o caminho local ou None."""
    pat = day.strftime("%Y-%m-%d") + "_*.jpg"
    for i in range(retries):
        r = subprocess.run(["rclone", "copy", f"{REMOTE}/{FOLDER}", dest_dir,
                           "--include", pat], capture_output=True, timeout=180)
        if r.returncode != 0:
            print(f"rclone copy rc={r.returncode}: {r.stderr.decode(errors='replace')[-300:]}",
                  file=sys.stderr)
        found = sorted(f for f in os.listdir(dest_dir)
                       if f.startswith(day.strftime("%Y-%m-%d")))
        if found:
            return os.path.join(dest_dir, found[-1])
        if i + 1 < retries:
            print(f"foto de {day:%d/%m} ainda nao esta no Drive; aguardando {RETRY_WAIT_S}s")
            time.sleep(RETRY_WAIT_S)
    return None


def montage(top, bottom, out):
    """Empilha top sobre bottom (largura 1600), JPEG final em `out`."""
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", top, "-i", bottom,
         "-filter_complex",
         "[0]scale=1600:-2[a];[1]scale=1600:-2[b];[a][b]vstack=inputs=2",
         "-frames:v", "1", "-q:v", "3", "-y", out],
        timeout=120)
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


def main():
    test = len(sys.argv) > 1 and sys.argv[1] == "--test"
    chat = (sys.argv[2] if len(sys.argv) > 2 else TEST_JID) if test else GROUP_JID
    prefix = "[TESTE] " if test else ""
    now = datetime.now(TZ)
    if not test and now.weekday() >= 5:  # Persistent pode recuperar no fim de semana
        print("fim de semana, sem comparacao")
        return 0
    today = now.date()
    # segunda compara com o último dia útil (sexta); demais dias, com ontem
    yesterday = today - timedelta(days=3 if now.weekday() == 0 else 1)

    with tempfile.TemporaryDirectory(prefix="sunset-compare-") as tmp:
        f_yest = rclone_fetch(yesterday, tmp)
        f_today = rclone_fetch(today, tmp, retries=1 if test else RETRIES)
        if not f_yest or not f_today:
            faltam = [d.strftime("%d/%m") for d, f in
                      ((yesterday, f_yest), (today, f_today)) if not f]
            st = send_text(chat, prefix + "⚠️ Comparação do pôr do sol não saiu hoje — "
                           f"faltando no Drive a foto de: {', '.join(faltam)}.")
            print(f"fotos faltando ({', '.join(faltam)}), aviso enviado, HTTP {st}")
            return 1
        out = os.path.join(tmp, "comparacao.jpg")
        if not montage(f_yest, f_today, out):
            st = send_text(chat, prefix + "⚠️ Comparação do pôr do sol falhou na montagem (ffmpeg).")
            print(f"montagem falhou, aviso enviado, HTTP {st}")
            return 1
        try:  # cópia de inspeção, sobrescrita a cada envio
            import shutil
            shutil.copyfile(out, "/tmp/ultima-comparacao.jpg")
        except OSError:
            pass
        # arquivo permanente da montagem no Drive (posicao1/DiaDeTrabalho/)
        r = subprocess.run(
            ["rclone", "copyto", out,
             f"{REMOTE}/posicao1/DiaDeTrabalho/{today:%Y-%m-%d}.jpg"],
            capture_output=True, timeout=180)
        if r.returncode != 0:
            print("rclone copyto DiaDeTrabalho falhou: "
                  + r.stderr.decode(errors="replace")[-200:], file=sys.stderr)
        caption = prefix + f"🌇 *Dia de Trabalho ({today:%d/%m})*"
        st = send_image(chat, out, caption)
        print(f"comparacao {yesterday:%d/%m} vs {today:%d/%m} enviada, HTTP {st} -> {chat}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
