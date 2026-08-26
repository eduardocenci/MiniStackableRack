#!/usr/bin/env python3
"""canteiro-watchdog — avisa no WhatsApp quando o canteiro (ARA) cai/volta.

Roda no bnu-raspberrypi por systemd timer (a cada 60 s):
  1. Cacheia um frame da câmera (go2rtc local, produtor já ativo pela tela
     de parede) em STATE_DIR/lastframe.jpg — vira a "última imagem antes da
     queda".
  2. Testa o relay do canteiro (TCP ara-raspberrypi:8554 pela tailnet).
  3. Máquina de estados com debounce: FAILS_TO_ALERT falhas seguidas
     (~3 min) → alerta de QUEDA no grupo WhatsApp (via WAHA, imagem com o
     último frame + caption); primeira volta → mensagem de RECUPERAÇÃO com
     a duração da queda.

Config: /etc/canteiro-watchdog.env (EnvironmentFile do systemd) —
WAHA_URL, WAHA_KEY, WAHA_SESSION, GROUP_JID, ARA_HOST, ARA_PORT.
Teste manual: canteiro-watchdog.py --test-alert [chatId]  (envia um alerta
de exemplo ao chatId — por padrão o grupo configurado; use o grupo
SmokeTests para não incomodar a família).
"""
import base64
import json
import os
import socket
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")
STATE_DIR = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/canteiro-watchdog"))
STATE = STATE_DIR / "state.json"
FRAME = STATE_DIR / "lastframe.jpg"
GO2RTC_FRAME = "http://127.0.0.1:1984/api/frame.jpeg?src=canteiro"

WAHA_URL = os.environ.get("WAHA_URL", "http://10.1.1.126:3000")
WAHA_KEY = os.environ["WAHA_KEY"]
WAHA_SESSION = os.environ.get("WAHA_SESSION", "default")
GROUP_JID = os.environ["GROUP_JID"]
ARA_HOST = os.environ.get("ARA_HOST", "100.66.255.82")
ARA_PORT = int(os.environ.get("ARA_PORT", "8554"))
FAILS_TO_ALERT = int(os.environ.get("FAILS_TO_ALERT", "3"))


def now():
    return datetime.now(TZ)


def hhmm(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M")


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"status": "up", "fails": 0, "down_since": None, "alerted": False}


def save_state(st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(STATE)


def cache_frame():
    try:
        req = urllib.request.Request(GO2RTC_FRAME)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
        if len(data) > 20000:  # descarta frames vazios/quebrados
            tmp = FRAME.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(FRAME)
    except Exception:
        pass  # canteiro fora → mantém o último frame bom


def relay_up():
    try:
        with socket.create_connection((ARA_HOST, ARA_PORT), timeout=5):
            return True
    except OSError:
        return False


def waha_post(endpoint, payload):
    req = urllib.request.Request(
        f"{WAHA_URL}/api/{endpoint}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Api-Key": WAHA_KEY},
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.status


def send_text(chat, text):
    return waha_post("sendText", {"session": WAHA_SESSION, "chatId": chat, "text": text})


def send_image(chat, caption):
    if not FRAME.exists():
        return send_text(chat, caption + "\n(sem imagem em cache)")
    b64 = base64.b64encode(FRAME.read_bytes()).decode()
    return waha_post("sendImage", {
        "session": WAHA_SESSION, "chatId": chat,
        "file": {"mimetype": "image/jpeg", "filename": "canteiro.jpg", "data": b64},
        "caption": caption,
    })


def down_caption(since_ts):
    return ("📡 *Canteiro fora do ar* desde " + hhmm(since_ts) +
            " — Starlink ou energia do barracão.\n"
            "Acima, a última imagem antes da queda.\n"
            "_Aviso automático; nova mensagem quando voltar._")


def up_text(since_ts):
    mins = int((now().timestamp() - since_ts) / 60)
    return ("✅ *Canteiro de volta ao ar* — ficou ~" + str(mins) +
            " min offline (" + hhmm(since_ts) + "–" + now().strftime("%H:%M") + ").")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--test-alert":
        chat = sys.argv[2] if len(sys.argv) > 2 else GROUP_JID
        cache_frame()
        st = send_image(chat, "[TESTE] " + down_caption(now().timestamp()))
        print("test alert sent, HTTP", st, "->", chat)
        return

    st = load_state()
    up = relay_up()

    if up:
        cache_frame()
        if st["status"] == "down":
            if st.get("alerted"):
                try:
                    send_text(GROUP_JID, up_text(st["down_since"]))
                except Exception as e:
                    print("recovery send failed:", e)
            st = {"status": "up", "fails": 0, "down_since": None, "alerted": False}
        else:
            st["fails"] = 0
    else:
        st["fails"] = st.get("fails", 0) + 1
        if st["status"] == "up" and st["fails"] >= FAILS_TO_ALERT:
            st["status"] = "down"
            st["down_since"] = now().timestamp() - st["fails"] * 60
            try:
                send_image(GROUP_JID, down_caption(st["down_since"]))
                st["alerted"] = True
            except Exception as e:
                print("down alert failed (retry next tick):", e)
                st["status"] = "up"      # re-tenta o alerta no próximo tick
                st["fails"] = FAILS_TO_ALERT
    save_state(st)


if __name__ == "__main__":
    main()
