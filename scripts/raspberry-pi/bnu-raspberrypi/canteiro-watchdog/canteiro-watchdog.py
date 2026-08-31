#!/usr/bin/env python3
"""canteiro-watchdog — avisa no WhatsApp quando o canteiro (ARA) cai/volta.

Roda no bnu-raspberrypi no container canteiro-watchdog — loop de 60 s, ver
docker/canteiro-jobs/ (até 2026-08-29 era um systemd timer):
  1. Cacheia um frame da câmera (go2rtc local, produtor mantido ativo pelo
     Frigate e pelo canteiro-hls) em STATE_DIR/lastframe.jpg — vira a
     "última imagem antes da queda".
  2. Testa o relay do canteiro (TCP ara-raspberrypi:8554 pela tailnet).
  3. Máquina de estados com debounce: FAILS_TO_ALERT falhas seguidas
     (~3 min) → alerta de QUEDA no grupo WhatsApp (via WAHA, imagem com o
     último frame + caption); primeira volta → mensagem de RECUPERAÇÃO com
     a duração da queda.
  4. AUTO-HEAL do /live (2026-08-31): com o relay EM PÉ, detecta o "wedge"
     do muxer HLS — go2rtc sobrevive às reconexões do produtor via Starlink
     mas o restream fica com timestamps que o mediamtx não engole; o muxer
     entra em crash-loop (`muxer error: sample timestamp is impossible to
     handle`) e NUNCA se recupera sozinho (incidente 29→31/08: 1658 crashes,
     /live morto 2 dias com o Frigate normal). Remédio: reiniciar go2rtc e
     DEPOIS canteiro-hls via socket do Docker (só o canteiro-hls não
     destrava — provado 2x). Debounce de HLS_FAILS_TO_HEAL ticks, no máximo
     MAX_HEAL_ATTEMPTS reinícios por episódio, depois escala no WhatsApp.
     Nota de heal vai a HEAL_JID (SmokeTests; vazio = só stdout — a família
     não vê nada disso).

Config: ~/canteiro-jobs/env/canteiro-watchdog.env (env_file do compose) —
WAHA_URL, WAHA_KEY, WAHA_SESSION, GROUP_JID, ARA_HOST, ARA_PORT e, para o
auto-heal, HEAL_JID (+ opcionais HLS_URL, HLS_FAILS_TO_HEAL,
MAX_HEAL_ATTEMPTS, HEAL_CONTAINERS, DOCKER_SOCK).
Teste manual: canteiro-watchdog.py --test-alert [chatId]  (envia um alerta
de exemplo ao chatId — por padrão o grupo configurado; use o grupo
SmokeTests para não incomodar a família).
Heal manual: canteiro-watchdog.py --heal-now  (reinicia go2rtc +
canteiro-hls agora, nota em HEAL_JID — o remédio do runbook em um comando).
"""
import base64
import http.client
import json
import os
import socket
import sys
import time
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

# ── auto-heal do /live ──────────────────────────────────────────────────────
HLS_URL = os.environ.get("HLS_URL", "http://127.0.0.1:8888/canteiro/index.m3u8?cookieCheck=1")
HEAL_JID = os.environ.get("HEAL_JID", "")  # vazio = heal sem WhatsApp (só stdout)
HLS_FAILS_TO_HEAL = int(os.environ.get("HLS_FAILS_TO_HEAL", "2"))
MAX_HEAL_ATTEMPTS = int(os.environ.get("MAX_HEAL_ATTEMPTS", "2"))
HEAL_CONTAINERS = os.environ.get("HEAL_CONTAINERS", "go2rtc,canteiro-hls")  # a ordem importa
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
# só a linha ERR do mediamtx (a INF "session closed: muxer instance crashed"
# não conta — 1 crash com 2 sessões geraria 3 matches e falso positivo)
CRASH_MARKER = b"muxer instance crashed: muxer error"


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


class _DockerSock(http.client.HTTPConnection):
    """HTTP do stdlib sobre o unix socket do Docker (sem docker-cli na imagem)."""
    def __init__(self):
        super().__init__("docker")

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(60)  # docker restart bloqueia até o container voltar
        s.connect(DOCKER_SOCK)
        self.sock = s


def docker_api(method, path):
    conn = _DockerSock()
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def hls_ok():
    try:
        with urllib.request.urlopen(urllib.request.Request(HLS_URL), timeout=10) as r:
            return r.status == 200 and r.read(2048).lstrip().startswith(b"#EXTM3U")
    except Exception:
        return False


def hls_crashes(since_ts):
    """Crashes ERR do muxer no log do canteiro-hls desde since_ts (unix)."""
    try:
        status, body = docker_api(
            "GET", f"/containers/canteiro-hls/logs?stdout=1&stderr=1&since={int(since_ts)}")
        return body.count(CRASH_MARKER) if status == 200 else 0
    except Exception:
        return 0  # sem socket/permissão → sinal fica só com o hls_ok()


def heal():
    """Reinicia os containers do remédio, na ordem (go2rtc ANTES do hls)."""
    for i, name in enumerate(n.strip() for n in HEAL_CONTAINERS.split(",") if n.strip()):
        if i:
            time.sleep(3)  # go2rtc precisa estar de pé antes do hls rediscar
        status, body = docker_api("POST", f"/containers/{name}/restart?t=5")
        if status != 204:
            raise RuntimeError(f"docker restart {name}: HTTP {status} "
                               + body[:200].decode(errors="replace"))


def heal_note(text):
    print(text)
    if HEAL_JID:
        try:
            send_text(HEAL_JID, text)
        except Exception as e:
            print("heal note send failed:", e)


def check_hls(st):
    """Wedge do /live: relay OK mas muxer HLS morto/em crash-loop → heal."""
    now_ts = now().timestamp()
    # a janela de crashes nunca olha para trás de um heal (o log do container
    # sobrevive ao restart — sem isso os crashes antigos re-disparariam)
    crashing = hls_crashes(max(now_ts - 90, st.get("last_heal", 0) + 1)) >= 3
    if hls_ok() and not crashing:
        if st.get("heal_attempts"):
            heal_note("✅ */live recuperado* — auto-heal do canteiro concluído.")
        st["hls_fails"] = 0
        st["heal_attempts"] = 0
        st["escalated"] = False
        return
    st["hls_fails"] = st.get("hls_fails", 0) + 1
    if st["hls_fails"] < HLS_FAILS_TO_HEAL:
        return
    if st.get("heal_attempts", 0) >= MAX_HEAL_ATTEMPTS:
        if not st.get("escalated"):
            heal_note("🚨 */live continua travado* após "
                      + str(MAX_HEAL_ATTEMPTS) + " reinícios automáticos de "
                      + HEAL_CONTAINERS + " — precisa de intervenção manual "
                      "(runbook canteiro-jobs).")
            st["escalated"] = True
        return
    st["heal_attempts"] = st.get("heal_attempts", 0) + 1
    st["hls_fails"] = 0
    st["last_heal"] = now_ts
    try:
        heal()
        heal_note("🔧 *Auto-heal do /live* — muxer HLS travado com o relay OK; "
                  "reiniciei " + HEAL_CONTAINERS.replace(",", " + ")
                  + " (tentativa " + str(st["heal_attempts"]) + "/"
                  + str(MAX_HEAL_ATTEMPTS) + ").")
    except Exception as e:
        heal_note("🚨 *Auto-heal do /live FALHOU* (tentativa "
                  + str(st["heal_attempts"]) + "): " + str(e))


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

    if len(sys.argv) > 1 and sys.argv[1] == "--heal-now":
        st = load_state()
        st["last_heal"] = now().timestamp()
        st["hls_fails"] = 0
        heal()
        save_state(st)
        heal_note("🔧 *Heal manual do /live* (--heal-now) — "
                  + HEAL_CONTAINERS.replace(",", " + ") + " reiniciados.")
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
            st.update({"status": "up", "fails": 0, "down_since": None, "alerted": False})
        else:
            st["fails"] = 0
        # wedge do /live só se avalia com o relay em pé (relay fora = queda
        # normal; os contadores de heal congelam até o canteiro voltar)
        check_hls(st)
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
