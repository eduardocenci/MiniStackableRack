#!/usr/bin/env python3
"""canteiro-presenca — relatório diário (20:00) de presença na obra ARA.

Pergunta ao netoverview do ara-raspberrypi quem esteve na rede Starlink do
canteiro hoje (GET /api/presence, janela 00:00 → agora, America/Sao_Paulo)
e manda o resumo no grupo WhatsApp via WAHA. Aparelhos fixos (Pi, câmera
Intelbras, roteador Starlink) ficam de fora — o que sobra são celulares e
computadores de pessoas, então a contagem aproxima "quantas pessoas
passaram pela obra hoje".

Roda no bnu-raspberrypi (que enxerga o WAHA na LAN) por systemd timer
(canteiro-presenca.timer, OnCalendar 20:00 America/Sao_Paulo).

Config: /etc/canteiro-presenca.env (EnvironmentFile do systemd) —
WAHA_URL, WAHA_KEY, WAHA_SESSION, GROUP_JID, TEST_JID, ARA_NTO_URL,
EXCLUDE_MACS, EXCLUDE_HOSTNAMES.
Teste manual: canteiro-presenca.py --test [chatId]  (por padrão manda ao
TEST_JID — grupo Casa SmokeTests — para não incomodar a família).
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")

WAHA_URL     = os.environ.get("WAHA_URL", "http://10.1.1.126:3000")
WAHA_KEY     = os.environ["WAHA_KEY"]
WAHA_SESSION = os.environ.get("WAHA_SESSION", "default")
GROUP_JID    = os.environ["GROUP_JID"]
TEST_JID     = os.environ.get("TEST_JID", GROUP_JID)
ARA_NTO_URL  = os.environ.get("ARA_NTO_URL", "http://ara-raspberrypi:5000")
EXCLUDE_MACS = {m.strip().lower() for m in os.environ.get("EXCLUDE_MACS", "").split(",") if m.strip()}
EXCLUDE_HOSTNAMES = [h.strip().lower() for h in
                     os.environ.get("EXCLUDE_HOSTNAMES", "ara-raspberrypi").split(",") if h.strip()]

WEEKDAYS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def fetch_presence(t_from, t_to):
    q = urllib.parse.urlencode({"from": t_from.isoformat(), "to": t_to.isoformat()})
    with urllib.request.urlopen(f"{ARA_NTO_URL}/api/presence?{q}", timeout=30) as r:
        return json.load(r)


def is_fixed(dev):
    mac = (dev.get("mac") or "").lower()
    if mac and mac in EXCLUDE_MACS:
        return True
    host = (dev.get("hostname") or "").lower()
    return any(x in host for x in EXCLUDE_HOSTNAMES)


def label(dev):
    # display_name já resolve nickname → hostname → vendor → IP; só o
    # fallback de IP é trocado por sufixo de MAC (celular com MAC aleatório
    # não tem nome — dê um apelido no netoverview para ele aparecer bonito)
    disp = dev.get("display_name") or ""
    if not disp or disp == dev.get("ip"):
        mac = dev.get("mac") or ""
        return f"aparelho …{mac[-8:]}" if mac else (dev.get("ip") or "?")
    return disp


def hhmm(iso):
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%H:%M")


def build_report(now):
    t_from = datetime.combine(now.date(), dtime(0, 0), tzinfo=TZ).astimezone(timezone.utc)
    data = fetch_presence(t_from, now.astimezone(timezone.utc))
    people = [d for d in data["devices"] if not is_fixed(d)]

    date_str = now.strftime("%d/%m") + f" ({WEEKDAYS[now.weekday()]})"
    lines = [f"👷 *Obra ARA — presença de {date_str}*", ""]
    if not people:
        lines.append("Nenhum aparelho de pessoa apareceu na rede do canteiro hoje.")
    elif len(people) == 1:
        lines.append("*1* aparelho de pessoa passou pela rede do canteiro:")
    else:
        lines.append(f"*{len(people)}* aparelhos de pessoas passaram pela rede do canteiro:")
    for d in people:
        start = "00:00" if d.get("present_at_window_start") else hhmm(d["first_seen_window_utc"])
        end = "agora" if d.get("online_now") else hhmm(d["last_seen_window_utc"])
        lines.append(f"• {label(d)} — {start} às {end}")
    lines += ["", "_Fixos excluídos (Pi, câmera, roteador) · 1 aparelho ≈ 1 pessoa_",
              "_Relatório automático diário das 20:00 · netoverview ara_"]
    return "\n".join(lines)


def send_text(chat, text):
    req = urllib.request.Request(
        f"{WAHA_URL}/api/sendText",
        data=json.dumps({"session": WAHA_SESSION, "chatId": chat, "text": text}).encode(),
        headers={"Content-Type": "application/json", "X-Api-Key": WAHA_KEY},
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.status


def main():
    now = datetime.now(TZ)
    test = len(sys.argv) > 1 and sys.argv[1] == "--test"
    chat = (sys.argv[2] if len(sys.argv) > 2 else TEST_JID) if test else GROUP_JID
    text = build_report(now)
    if test:
        text = "[TESTE] " + text
    st = send_text(chat, text)
    print(f"presence report sent, HTTP {st} -> {chat}")


if __name__ == "__main__":
    main()
