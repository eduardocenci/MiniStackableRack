#!/usr/bin/env python3
"""canteiro-presenca — relatório diário (20:00) de presença na obra ARA.

Pergunta ao netoverview do ara-raspberrypi2 quem esteve na rede Starlink do
canteiro hoje (GET /api/presence, janela 00:00 → agora, America/Sao_Paulo)
e manda o resumo no grupo WhatsApp via WAHA. Aparelhos fixos (Pi, câmera
Intelbras, roteador Starlink) ficam de fora — o que sobra são celulares e
computadores de pessoas, então a contagem aproxima "quantas pessoas
passaram pela obra hoje".

Desde 11/10/2026 o relatório traz também um bloco "fora do expediente": quem
esteve na rede na última noite completa (padrão 20:00 → 05:00, a mesma janela
estrita do Diário de Obra) — há gente dormindo no canteiro desde a semana de
05/10 e queremos saber quantos. Por aparelho, só "já na rede às 20:00" ou
"chegou às HH:MM" — até quando ficou não dá para afirmar (ver night_lines).
A noite que começa às 20:00 de hoje só sai no relatório de amanhã; a linha
"na rede entre 19:00 e 20:00" é a prévia dela.

Roda no bnu-raspberrypi (que enxerga o WAHA na LAN) no container
canteiro-presenca — supercronic 20:00 America/Sao_Paulo, ver
docker/canteiro-jobs/ (até 2026-08-29 era um systemd timer).

Config: ~/canteiro-jobs/env/canteiro-presenca.env (env_file do compose) —
WAHA_URL, WAHA_KEY, WAHA_SESSION, GROUP_JID, TEST_JID, ARA_NTO_URL,
EXCLUDE_MACS, EXCLUDE_HOSTNAMES, LAN_CIDR, NIGHT_START_H, NIGHT_END_H.
Teste manual: canteiro-presenca.py --test [chatId]  (por padrão manda ao
TEST_JID — grupo Casa SmokeTests — para não incomodar a família).
Prévia sem enviar nada: canteiro-presenca.py --dry-run  (só imprime).
"""
import ipaddress
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")

WAHA_URL     = os.environ.get("WAHA_URL", "http://10.1.1.126:3000")
WAHA_KEY     = os.environ.get("WAHA_KEY", "")    # obrigatórios só para enviar
WAHA_SESSION = os.environ.get("WAHA_SESSION", "default")
GROUP_JID    = os.environ.get("GROUP_JID", "")   # (--dry-run roda sem eles)
TEST_JID     = os.environ.get("TEST_JID", GROUP_JID)
ARA_NTO_URL  = os.environ.get("ARA_NTO_URL", "http://ara-raspberrypi2:5000")
EXCLUDE_MACS = {m.strip().lower() for m in os.environ.get("EXCLUDE_MACS", "").split(",") if m.strip()}
EXCLUDE_HOSTNAMES = [h.strip().lower() for h in
                     os.environ.get("EXCLUDE_HOSTNAMES", "ara-raspberrypi").split(",") if h.strip()]
LAN_CIDR = ipaddress.ip_network(os.environ.get("LAN_CIDR", "192.168.1.0/24"))
# fora do expediente: janela noturna estrita (horas BRT), a mesma do Diário de
# Obra (canteiro-diario prev_night_20_05) — a equipe chega 06:00–07:00 e sai
# até ~17:00, então 20:00 → 05:00 não pega quem só saiu tarde ou chegou cedo
NIGHT_START_H = int(os.environ.get("NIGHT_START_H", "20"))
NIGHT_END_H   = int(os.environ.get("NIGHT_END_H", "5"))

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


def in_lan(dev):
    # fora da LAN do canteiro nunca é pessoa (ex.: 172.17.0.x da bridge Docker
    # do próprio Pi, que já poluiu o relatório em 31/08/2026) — defesa em
    # profundidade além do filtro de interfaces virtuais no scanner netoverview
    try:
        return ipaddress.ip_address(dev.get("ip") or "") in LAN_CIDR
    except ValueError:
        return False


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


def people_between(t_from, t_to):
    data = fetch_presence(t_from.astimezone(timezone.utc), t_to.astimezone(timezone.utc))
    return [d for d in data["devices"] if in_lan(d) and not is_fixed(d)]


def last_night(now):
    # a última noite JÁ ENCERRADA: às 20:00 é a de ontem→hoje; rodando de
    # madrugada (teste manual antes das NIGHT_END_H), a de anteontem→ontem
    end = datetime.combine(now.date(), dtime(NIGHT_END_H, 0), tzinfo=TZ)
    if now < end:
        end -= timedelta(days=1)
    start = datetime.combine(end.date(), dtime(NIGHT_START_H, 0), tzinfo=TZ)
    if start >= end:
        start -= timedelta(days=1)
    return start, end


def night_lines(now):
    # Só "já estava às 20:00" × "chegou às HH:MM": celular dormindo derruba e
    # religa o Wi-Fi a noite toda (noite 08→09/10: 6, 9 e 11 transições em 3
    # dos 4 aparelhos, todos já na rede às 20:00), e o last_seen da API vem
    # grampeado na borda da janela — nenhum dos dois diz até quando ficou.
    start, end = last_night(now)
    night = people_between(start, end)
    if start.month == end.month:
        span = f"{start:%d}→{end:%d/%m}"
    else:
        span = f"{start:%d/%m}→{end:%d/%m}"
    lines = [f"🌙 *Fora do expediente — noite de {span}, {start:%H:%M} às {end:%H:%M}*"]
    if not night:
        lines.append("Nenhum aparelho de pessoa na rede do canteiro.")
    else:
        noun = "aparelho de pessoa ficou" if len(night) == 1 else "aparelhos de pessoas ficaram"
        lines.append(f"*{len(night)}* {noun} na rede do canteiro:")
        for d in night:
            if d.get("present_at_window_start"):
                when = f"já na rede às {start:%H:%M}"
            else:
                when = f"chegou às {hhmm(d['first_seen_window_utc'])}"
            lines.append(f"• {label(d)} — {when}")
    # prévia da noite que começa hoje: quem apareceu na última hora (não o
    # online_now do instante, que perde o celular que cochilou bem às 20:00)
    late = people_between(now - timedelta(hours=1), now)
    lines.append(f"Na rede entre {now - timedelta(hours=1):%H:%M} e {now:%H:%M} de hoje: "
                 f"*{len(late)}* — prévia da noite de hoje")
    return lines


def build_report(now):
    t_from = datetime.combine(now.date(), dtime(0, 0), tzinfo=TZ)
    people = people_between(t_from, now)

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
    try:   # o bloco novo não pode derrubar o relatório do dia
        lines += [""] + night_lines(now)
    except Exception as e:  # noqa: BLE001
        lines += ["", f"🌙 _Fora do expediente indisponível hoje ({type(e).__name__})_"]
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
    if len(sys.argv) > 1 and sys.argv[1] == "--dry-run":
        print(build_report(now))
        return
    test = len(sys.argv) > 1 and sys.argv[1] == "--test"
    chat = (sys.argv[2] if len(sys.argv) > 2 else TEST_JID) if test else GROUP_JID
    if not WAHA_KEY or not chat:
        sys.exit("WAHA_KEY/GROUP_JID ausentes no env — nada enviado")
    text = build_report(now)
    if test:
        text = "[TESTE] " + text
    st = send_text(chat, text)
    print(f"presence report sent, HTTP {st} -> {chat}")


if __name__ == "__main__":
    main()
