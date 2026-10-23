#!/usr/bin/env python3
"""canteiro-vigia — vigia da câmera do canteiro (ARA) quando a obra está vazia.

Enquanto o WiFi do canteiro não tem NENHUM aparelho de pessoa (só os fixos:
roteador, câmera, Pi), o vigia está LIGADO: qualquer pessoa ou veículo que o
Frigate (bnu LXC 105) rastreia na câmera `canteiro` vira UMA mensagem no grupo
WhatsApp "Casa Céu Azul" — a foto do momento + uma linha dizendo se algum
aparelho conhecido entrou no WiFi no minuto seguinte (🟢, quem tem a senha do
WiFi pode estar lá) ou se nenhum entrou (🔴, "deve ser alguém estranho").
Assim que um aparelho de pessoa aparece no WiFi o vigia DESLIGA; ele religa
ARMAR_APOS_MIN depois de o WiFi ficar vazio de novo. Na prática: liga na sexta
quando a equipe vai embora, desliga na segunda quando ela chega — e cobre
feriados e as noites de semana em que ninguém dormiu no canteiro (decisão
Eduardo 22/10/2026; a 1ª iteração, só sáb/dom, durou um dia).

Como sabe do minuto: o roteador Starlink responde por gRPC (`wifi_get_clients`)
com `associatedTimeS` de cada cliente — há quantos segundos o aparelho está
associado ao WiFi. Daí sai o instante exato em que cada um entrou, sem depender
da varredura ARP de 5 min do netoverview (que fica como fallback, ±5 min). O
roteador é LAN-only; chega até ele pelo relay socat `router-proxy` do ara Pi
(`ara-raspberrypi2:9000` → `192.168.1.1:9000`, mesmo desenho do starlink-proxy).

Fluxo (loop de POLL_S segundos):
  1. A cada REDE_POLL_S: quem está no WiFi → estado ligado/desligado.
  2. GET Frigate /api/events (câmera canteiro, rótulos LABELS) — eventos novos.
     Com o vigia ligado, o primeiro evento abre um EPISÓDIO (t0 = start_time);
     detecções seguintes dentro de EPISODIO_MIN entram no mesmo episódio e não
     geram mensagem — com UMA exceção: episódio aberto por VEÍCULO + pessoa
     detectada depois = segunda mensagem, com a foto da pessoa (foto do carro e
     da pessoa na sequência).
  3. Em t0 + JANELA_S: veredito pelo WiFi (roteador → netoverview → sem leitura),
     foto (snapshot do evento com a caixa do objeto → frame da gravação →
     latest.jpg) e envio via WAHA sendImage com legenda de 2 linhas. 🟢 também
     desliga o vigia (alguém da equipe chegou).
  4. Tetos MAX_HORA / MAX_DIA (além deles só registra; uma nota de pausa vai ao
     TEST_JID).

MODE (env): log = decide e registra, não envia · shadow = envia ao TEST_JID
(Casa SmokeTests) com a marca SOMBRA · live = envia ao GROUP_JID (Casa Céu Azul).
Mudanças de estado (ligou/desligou) vão a AVISAR_ESTADO: teste (SmokeTests,
padrão) · grupo · nao. Os destinos vêm só do env — nenhuma leitura do WhatsApp.

CLI:
  canteiro-vigia.py                 loop (container)
  canteiro-vigia.py --selftest      Frigate, roteador, netoverview, WAHA — sem enviar
  canteiro-vigia.py --status        estado do vigia + quem está no WiFi + episódio aberto
  canteiro-vigia.py --veredito [t0] imprime o veredito do WiFi para t0 (epoch; padrão agora)
  canteiro-vigia.py --test-alert [JID]  último evento qualificável pelo fluxo completo,
                                    "[TESTE]", ao JID (padrão TEST_JID), ignorando o estado

Roda no bnu-raspberrypi, container `canteiro-vigia` (docker/canteiro-vigia/).
Estado em STATE_DIR: state.json (eventos vistos, episódio aberto, ligado/
desligado, contadores — um restart nunca re-avisa), vigia.jsonl (uma linha por
mensagem decidida e por mudança de estado), heartbeat.json.
"""
import base64
import ipaddress
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")


def E(name, default=""):
    return os.environ.get(name, default).strip()


MODE = E("MODE", "log")
WAHA_URL = E("WAHA_URL", "http://10.1.1.126:3000").rstrip("/")
WAHA_KEY = E("WAHA_KEY")
WAHA_SESSION = E("WAHA_SESSION", "default")
GROUP_JID = E("GROUP_JID")                      # "Casa Céu Azul" (ARA_ALERTAS_GROUP_JID)
TEST_JID = E("TEST_JID")                        # Casa SmokeTests
LIVE_URL = E("LIVE_URL")                        # opcional: link do /live no fim da legenda
AVISAR_ESTADO = E("AVISAR_ESTADO", "teste")     # ligou/desligou → teste | grupo | nao

FRIGATE_URL = E("FRIGATE_URL", "http://bnu-frigate:5000").rstrip("/")
FRIGATE_CAMERA = E("FRIGATE_CAMERA", "canteiro")
LABELS = [x.strip() for x in E("LABELS", "person,car,bus,motorcycle,bicycle").split(",") if x.strip()]

ROUTER_GRPC = E("ROUTER_GRPC", "ara-raspberrypi2:9000")   # relay socat → roteador Starlink
ARA_NTO_URL = E("ARA_NTO_URL", "http://ara-raspberrypi2:5000").rstrip("/")
LAN_CIDR = ipaddress.ip_network(E("LAN_CIDR", "192.168.1.0/24"))
# o roteador redige o MAC dos clientes até o OUI (54:ba:d9:XX:XX:XX) — por isso
# os fixos são reconhecidos por OUI e por nome, não por MAC completo
FIXED_OUIS = {x.strip().lower() for x in E("FIXED_OUIS", "74:24:9f,54:ba:d9,88:a2:9e").split(",") if x.strip()}
FIXED_NAMES = [x.strip().lower() for x in E("FIXED_NAMES", "controller,im9-m,ara-raspberrypi").split(",") if x.strip()]

ARMAR_APOS_MIN = int(E("ARMAR_APOS_MIN", "5"))  # WiFi vazio há X min → vigia ligado (celular que só piscou não liga)
REDE_POLL_S = int(E("REDE_POLL_S", "30"))       # cadência da leitura do WiFi para ligar/desligar
FORCAR_ARMADO = E("FORCAR_ARMADO", "0") == "1"  # teste: ligado mesmo com gente no WiFi
JANELA_S = int(E("JANELA_S", "60"))             # "o minuto seguinte"
PRE_S = int(E("PRE_S", "120"))                  # entrou pouco ANTES de a câmera ver = também conta
EPISODIO_MIN = int(E("EPISODIO_MIN", "15"))     # detecções a menos de X min do episódio aberto não avisam de novo
MAX_HORA = int(E("MAX_HORA", "6"))
MAX_DIA = int(E("MAX_DIA", "24"))
POLL_S = int(E("POLL_S", "10"))
LOOKBACK_S = int(E("LOOKBACK_S", "900"))        # janela do /api/events a cada rodada

STATE_DIR = Path(E("STATE_DIR", "/var/lib/canteiro-vigia"))
STATE = STATE_DIR / "state.json"
JSONL = STATE_DIR / "vigia.jsonl"
HEARTBEAT = STATE_DIR / "heartbeat.json"

ROTULO = {"person": "pessoa", "car": "carro", "bus": "caminhão/ônibus",
          "motorcycle": "moto", "bicycle": "bicicleta"}
DIA = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def log(msg):
    print(f"{datetime.now(TZ):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def hhmm(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M")


def quando(ts):
    d = datetime.fromtimestamp(ts, TZ)
    return f"{DIA[d.weekday()]} {d:%d/%m %H:%M}"


# ── HTTP / gRPC helpers ───────────────────────────────────────────────────────
def http_json(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def http_bytes(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def waha_post(endpoint, payload, timeout=60):
    req = urllib.request.Request(
        f"{WAHA_URL}/api/{endpoint}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Api-Key": WAHA_KEY}, method="POST")
    last = None
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as ex:
            last = f"HTTP {ex.code}: {ex.read()[:160]!r}"
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            last = f"{type(ex).__name__}: {ex}"
        log(f"WAHA {endpoint} tentativa {i + 1}/3 falhou: {last}")
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"WAHA {endpoint}: {last}")


def waha_session_status():
    req = urllib.request.Request(f"{WAHA_URL}/api/sessions/{WAHA_SESSION}",
                                 headers={"X-Api-Key": WAHA_KEY})
    with urllib.request.urlopen(req, timeout=10) as r:
        return (json.load(r) or {}).get("status", "")


def send(chat, text, jpg):
    """sendImage com legenda (ou sendText sem foto) — sempre a UM chat vindo do env."""
    if jpg:
        return waha_post("sendImage", {
            "session": WAHA_SESSION, "chatId": chat, "caption": text,
            "file": {"mimetype": "image/jpeg", "filename": "canteiro.jpg",
                     "data": base64.b64encode(jpg).decode()}})
    return waha_post("sendText", {"session": WAHA_SESSION, "chatId": chat, "text": text})


def router_clients():
    """Clientes WiFi do roteador Starlink (gRPC via relay). Levanta exceção se não responder."""
    out = subprocess.run(
        ["grpcurl", "-plaintext", "-max-time", "10", "-d", '{"wifi_get_clients":{}}',
         ROUTER_GRPC, "SpaceX.API.Device.Device/Handle"],
        check=True, capture_output=True, text=True, timeout=20).stdout
    return json.loads(out)["wifiGetClients"]["clients"]


# ── Frigate ───────────────────────────────────────────────────────────────────
def frigate_events(after):
    q = urllib.parse.urlencode({"cameras": FRIGATE_CAMERA, "labels": ",".join(LABELS),
                                "after": int(after), "limit": 100})
    evs = http_json(f"{FRIGATE_URL}/api/events?{q}")
    return sorted((e for e in evs if e.get("camera") == FRIGATE_CAMERA and e.get("label") in LABELS),
                  key=lambda e: e["start_time"])


def foto(eid, t0):
    """Foto do evento: snapshot com a caixa do objeto → frame full-res da gravação → latest.jpg."""
    for url, nome in (
        (f"{FRIGATE_URL}/api/events/{eid}/snapshot.jpg?bbox=1&quality=85", "snapshot"),
        (f"{FRIGATE_URL}/api/{FRIGATE_CAMERA}/recordings/{t0 + 2:.3f}/snapshot.jpg", "gravação"),
        (f"{FRIGATE_URL}/api/{FRIGATE_CAMERA}/latest.jpg?h=720&quality=85", "latest"),
    ):
        try:
            raw = http_bytes(url)
            if raw and raw[:2] == b"\xff\xd8":
                return raw, nome
        except Exception as ex:  # noqa: BLE001 — a foto é best-effort, a mensagem sai sem ela
            log(f"foto {nome} falhou: {ex}")
    return None, "sem foto"


# ── WiFi do canteiro ──────────────────────────────────────────────────────────
def _fixed(name, mac):
    n = (name or "").lower()
    if any(f in n for f in FIXED_NAMES):
        return True
    parts = (mac or "").lower().split(":")
    return len(parts) == 6 and ":".join(parts[:3]) in FIXED_OUIS


def _na_lan(ip):
    try:
        return ipaddress.ip_address(ip or "") in LAN_CIDR
    except ValueError:
        return False


_NOMES = {"ts": 0.0, "map": {}}


def _nto_names(agora):
    """ip → apelido do netoverview (quem já tem nome bonito: 'iPhone (Jorge)'); cache de 5 min."""
    if agora - _NOMES["ts"] < 300:
        return _NOMES["map"]
    try:
        devs = http_json(f"{ARA_NTO_URL}/api/devices", timeout=10).get("devices", [])
        _NOMES["map"] = {d["ip"]: d["nickname"] for d in devs if d.get("ip") and d.get("nickname")}
        _NOMES["ts"] = agora
    except Exception:  # noqa: BLE001
        pass
    return _NOMES["map"]


def pessoas_roteador(agora):
    """[(rótulo, instante em que associou | None)] dos aparelhos de pessoas no WiFi, pelo roteador."""
    nomes = _nto_names(agora)
    out, vistos = [], set()
    for c in router_clients():
        if (c.get("role") or "") != "CLIENT":
            continue
        name, mac, ip = c.get("name") or "", c.get("macAddress") or "", c.get("ipAddress") or ""
        if _fixed(name, mac) or (ip and not _na_lan(ip)):
            continue
        rotulo = nomes.get(ip) or (name if name.lower() not in ("", "unknown") else "") \
            or f"aparelho {ip or mac[:8] or '?'}"
        if rotulo in vistos and ip:
            rotulo = f"{rotulo} (.{ip.rsplit('.', 1)[-1]})"   # dois "iPhone" sem apelido: distingue pelo IP
        vistos.add(rotulo)
        assoc_s = c.get("associatedTimeS")
        out.append((rotulo, agora - float(assoc_s) if assoc_s is not None else None))
    return out


def pessoas_netoverview():
    """Fallback ±5 min: aparelhos de pessoas online na última varredura ARP do netoverview."""
    out = []
    for d in http_json(f"{ARA_NTO_URL}/api/devices", timeout=15).get("devices", []):
        if not d.get("online") or not _na_lan(d.get("ip")):
            continue
        if _fixed(d.get("hostname") or d.get("display_name") or "", d.get("mac") or ""):
            continue
        out.append((d.get("nickname") or d.get("display_name") or f"aparelho {d.get('ip')}", None))
    return out


def rede_agora(agora):
    """(pessoas, fonte): quem está no WiFi agora — (None, None) quando nem roteador nem netoverview respondem."""
    try:
        return pessoas_roteador(agora), "roteador"
    except Exception as ex:  # noqa: BLE001
        log(f"roteador sem resposta ({ex}) — tentando netoverview")
    try:
        return pessoas_netoverview(), "netoverview"
    except Exception as ex:  # noqa: BLE001
        log(f"netoverview sem resposta ({ex}) — WiFi sem leitura")
    return None, None


def veredito(t_from, t_to, agora):
    """Classifica quem está no WiFi contra a janela [t_from, t_to] (instante em que associou)."""
    pessoas, fonte = rede_agora(agora)
    v = {"fonte": fonte, "conectou": [], "ja": [], "depois": [], "pessoas": pessoas or []}
    for rotulo, assoc in pessoas or []:
        if assoc is None or assoc < t_from:
            v["ja"].append((rotulo, assoc))
        elif assoc <= t_to:
            v["conectou"].append((rotulo, assoc))
        else:
            v["depois"].append((rotulo, assoc))
    return v


# ── Mensagem ──────────────────────────────────────────────────────────────────
def mensagem(t_ev, label, n, v, apos=None, prefixo=""):
    """(texto, sev). `apos` = (label, t0) do veículo que abriu o episódio, na mensagem da pessoa."""
    tipo = "pessoa" if label == "person" else f"veículo ({ROTULO.get(label, label)})"
    if apos:
        tipo += f" (após {ROTULO.get(apos[0], apos[0])} {hhmm(apos[1])})"
    elif n > 1:
        tipo += f" ×{n}"
    sev = "🟢" if v["pessoas"] else "🔴"
    linhas = [f"{prefixo}{sev} *Canteiro · {quando(t_ev)}* — {tipo}"]
    if sev == "🟢":
        nomes = [f"{r} (entrou {hhmm(a)})" for r, a in v["conectou"] + v["depois"]] + [r for r, _ in v["ja"]]
        linhas.append("📱 Há dispositivos conhecidos no WiFi, então tudo deve estar OK: " + ", ".join(nomes))
    elif v["fonte"]:
        linhas.append("📵 Nenhum aparelho conhecido conectou no WiFi no minuto seguinte, "
                      "logo deve ser alguém estranho !!!")
    else:
        linhas.append("📵 Sem leitura do WiFi do canteiro (roteador e netoverview fora), "
                      "logo pode ser alguém estranho !!!")
    if v["fonte"] == "netoverview":
        linhas[-1] += " (leitura ±5 min)"
    if LIVE_URL:
        linhas.append(f"🔗 {LIVE_URL}")
    return "\n".join(linhas), sev


# ── Estado ────────────────────────────────────────────────────────────────────
def load_state():
    base = {"seen": {}, "episodio": None, "envios": [], "pausa_avisada": None,
            "armado": None, "armado_desde": None, "rede_vazia_desde": None,
            "rede": {"ts": 0, "fonte": None, "pessoas": []}}
    try:
        base.update(json.loads(STATE.read_text()))
    except (OSError, ValueError):
        pass
    return base


def save_state(st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(STATE)


def jsonl(rec):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with JSONL.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def heartbeat(st, ok, err=None):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(json.dumps({"ts": time.time(), "ok": ok, "mode": MODE, "erro": err,
                                         "armado": st.get("armado"), "rede": st.get("rede")}))
    except OSError:
        pass


# ── Ligado / desligado ────────────────────────────────────────────────────────
def armado(st):
    return bool(st.get("armado"))


def avisar_estado(texto):
    chat = {"teste": TEST_JID, "grupo": GROUP_JID if MODE == "live" else TEST_JID}.get(AVISAR_ESTADO, "")
    if not chat or MODE == "log":
        return "log"
    try:
        send(chat, texto, None)
        return "teste" if chat == TEST_JID else "grupo"
    except Exception as ex:  # noqa: BLE001
        log(f"aviso de estado falhou: {ex}")
        return "falhou"


def mudar_estado(st, ligado, agora, motivo):
    st["armado"] = ligado
    st["armado_desde"] = agora
    texto = (f"🔒 *Vigia ligado · {quando(agora)}* — {motivo}" if ligado
             else f"🔓 *Vigia desligado · {quando(agora)}* — {motivo}")
    destino = avisar_estado(texto)
    log(f"{'LIGADO' if ligado else 'DESLIGADO'}: {motivo} (aviso → {destino})")
    jsonl({"tipo": "estado", "ts": agora, "armado": ligado, "motivo": motivo, "destino": destino, "mode": MODE})


def atualizar_armado(st, agora):
    pessoas, fonte = rede_agora(agora)
    if pessoas is None:
        st["rede"] = {"ts": agora, "fonte": None, "pessoas": st["rede"].get("pessoas", [])}
        return
    nomes = [r for r, _ in pessoas]
    st["rede"] = {"ts": agora, "fonte": fonte, "pessoas": nomes}
    if FORCAR_ARMADO:
        if not armado(st):
            mudar_estado(st, True, agora, "FORCAR_ARMADO=1")
        return
    if st["armado"] is None:
        # primeira leitura de todas: assume o estado atual do WiFi, sem aviso
        st["armado"] = not pessoas
        st["armado_desde"] = agora
        st["rede_vazia_desde"] = None if pessoas else agora
        log(f"estado inicial: {'LIGADO' if st['armado'] else 'DESLIGADO'} "
            f"(WiFi: {', '.join(nomes) or 'vazio'}, fonte {fonte})")
        return
    if pessoas:
        st["rede_vazia_desde"] = None
        if armado(st):
            mudar_estado(st, False, agora, f"{nomes[0]} no WiFi")
    else:
        if st["rede_vazia_desde"] is None:
            st["rede_vazia_desde"] = agora
        if not armado(st) and agora - st["rede_vazia_desde"] >= ARMAR_APOS_MIN * 60:
            mudar_estado(st, True, agora, f"WiFi do canteiro vazio há {ARMAR_APOS_MIN} min")


# ── Decisão e envio ───────────────────────────────────────────────────────────
def teto_atingido(st, agora):
    hora = [t for t in st["envios"] if t > agora - 3600]
    inicio_dia = datetime.fromtimestamp(agora, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    dia = [t for t in st["envios"] if t >= inicio_dia]
    if len(hora) >= MAX_HORA:
        return f"{MAX_HORA}/hora"
    if len(dia) >= MAX_DIA:
        return f"{MAX_DIA}/dia"
    return None


def entregar(texto, jpg):
    """Envia conforme o MODE; devolve o destino usado ('log' = nada enviado)."""
    if MODE == "log":
        return "log"
    if MODE == "shadow":
        if not TEST_JID:
            raise RuntimeError("TEST_JID vazio em MODE=shadow")
        send(TEST_JID, f"👁️ *SOMBRA* (iria ao Casa Céu Azul)\n{texto}", jpg)
        return "teste"
    if not GROUP_JID:
        raise RuntimeError("GROUP_JID vazio em MODE=live")
    send(GROUP_JID, texto, jpg)
    return "grupo"


def decidir(st, ep, agora, seguimento=False):
    """Veredito + foto + envio de uma mensagem do episódio (a de abertura ou a da pessoa após veículo)."""
    if seguimento:
        seg = ep["seguimento"]
        t_ev, eid, label, apos = seg["t"], seg["eid"], "person", (ep["label"], ep["t0"])
    else:
        t_ev, eid, label, apos = ep["t0"], ep["eid"], ep["label"], None
    v = veredito(ep["t0"] - PRE_S, t_ev + JANELA_S, agora)
    texto, sev = mensagem(t_ev, label, ep["n"] - (1 if ep.get("seguimento") else 0), v, apos=apos)
    motivo = teto_atingido(st, agora)
    if motivo:
        motivo = f"teto {motivo}"
        hoje = datetime.fromtimestamp(agora, TZ).strftime("%Y-%m-%d")
        if st.get("pausa_avisada") != hoje and TEST_JID and MODE != "log":
            try:
                send(TEST_JID, f"⏸️ canteiro-vigia: {motivo} atingido — só registrando até liberar.", None)
            except Exception as ex:  # noqa: BLE001
                log(f"nota de pausa falhou: {ex}")
            st["pausa_avisada"] = hoje
    destino, jpg, fonte_foto = "nao-enviado", None, None
    if motivo is None:
        jpg, fonte_foto = foto(eid, t_ev)
        destino = entregar(texto, jpg)
        if destino != "log":
            st["envios"] = (st["envios"] + [agora])[-200:]
    if seguimento:
        ep["seguimento"]["decidido"] = agora
    else:
        ep["decidido"] = agora
        ep["sev"] = sev
    if sev == "🟢" and armado(st):
        mudar_estado(st, False, agora, f"{v['pessoas'][0][0]} no WiFi (detecção {hhmm(t_ev)})")
    jsonl({"tipo": "seguimento" if seguimento else "episodio", "ts": agora, "t0": ep["t0"], "t": t_ev,
           "eid": eid, "label": label, "n": ep["n"], "sev": sev, "fonte": v["fonte"],
           "conectou": [r for r, _ in v["conectou"]], "ja": [r for r, _ in v["ja"]],
           "depois": [r for r, _ in v["depois"]], "foto": fonte_foto, "destino": destino,
           "motivo": motivo, "mode": MODE, "texto": texto})
    log(f"{'pessoa após ' + ROTULO.get(ep['label'], ep['label']) if seguimento else 'episódio'} {hhmm(t_ev)} "
        f"{label} ×{ep['n']} → {sev} fonte={v['fonte']} destino={destino}{' (' + motivo + ')' if motivo else ''}")


def one_round(st, agora=None, first=False):
    agora = agora or time.time()
    if st["armado"] is None or agora - st["rede"].get("ts", 0) >= REDE_POLL_S:
        atualizar_armado(st, agora)
    # eventos vistos: só os das últimas 24 h ficam na memória
    st["seen"] = {k: v for k, v in st["seen"].items() if v > agora - 86400}
    for e in frigate_events(agora - LOOKBACK_S):
        eid, t_ini = e["id"], float(e["start_time"])
        t_fim = float(e["end_time"]) if e.get("end_time") else agora
        ep = st.get("episodio")
        if eid in st["seen"]:
            if ep and eid in ep["eids"]:
                ep["fim"] = max(ep["fim"], t_fim)
            continue
        st["seen"][eid] = t_ini
        if first:
            continue   # primeira rodada sem estado: o que já estava no Frigate é passado, não aviso
        if ep and t_ini <= ep["fim"] + EPISODIO_MIN * 60:
            ep["n"] += 1
            ep["eids"].append(eid)
            ep["fim"] = max(ep["fim"], t_fim)
            if e["label"] == "person" and ep["label"] != "person" and not ep.get("seguimento"):
                # carro primeiro, pessoa depois: segunda mensagem com a foto da pessoa
                ep["seguimento"] = {"eid": eid, "t": t_ini, "decidido": None}
                log(f"pessoa {eid} após {ROTULO.get(ep['label'], ep['label'])} das {hhmm(ep['t0'])} — "
                    f"foto da pessoa em {JANELA_S}s")
            else:
                log(f"evento {eid} ({e['label']}) entra no episódio das {hhmm(ep['t0'])} (×{ep['n']})")
            continue
        if not armado(st):
            log(f"evento {eid} ({e['label']}) {hhmm(t_ini)} ignorado — vigia desligado "
                f"(WiFi: {', '.join(st['rede'].get('pessoas') or []) or '?'})")
            continue
        st["episodio"] = {"t0": t_ini, "eid": eid, "label": e["label"], "n": 1, "eids": [eid],
                          "fim": t_fim, "decidido": None, "sev": None, "seguimento": None}
        log(f"episódio aberto {hhmm(t_ini)} {e['label']} ({eid}) — veredito em {JANELA_S}s")
    ep = st.get("episodio")
    if ep and not ep["decidido"] and agora >= ep["t0"] + JANELA_S:
        decidir(st, ep, agora)
    seg = ep and ep.get("seguimento")
    if seg and not seg["decidido"] and agora >= seg["t"] + JANELA_S:
        decidir(st, ep, agora, seguimento=True)
    if ep and ep["decidido"] and (not seg or seg["decidido"]) and agora > ep["fim"] + EPISODIO_MIN * 60:
        log(f"episódio das {hhmm(ep['t0'])} encerrado (×{ep['n']})")
        st["episodio"] = None


# ── CLI ───────────────────────────────────────────────────────────────────────
def selftest():
    ok = True
    agora = time.time()
    try:
        ver = http_bytes(f"{FRIGATE_URL}/api/version", timeout=10).decode().strip()   # texto puro ("0.17.1-")
        evs = frigate_events(agora - 86400)
        print(f"frigate   OK  {ver}  eventos 24h ({FRIGATE_CAMERA}, {','.join(LABELS)}): {len(evs)}")
    except Exception as ex:  # noqa: BLE001
        ok = False
        print(f"frigate   FALHA  {ex}")
    try:
        pessoas = pessoas_roteador(agora)
        print(f"roteador  OK  {ROUTER_GRPC}: {len(pessoas)} aparelhos de pessoas no WiFi"
              f" → vigia {'LIGADO' if not pessoas else 'DESLIGADO'} se fosse a 1ª leitura")
        for r, a in pessoas:
            print(f"            {r:28} entrou {hhmm(a) if a else '?'} ({int(agora - a) if a else '?'} s atrás)")
    except Exception as ex:  # noqa: BLE001
        ok = False
        print(f"roteador  FALHA  {ex}")
    try:
        p = pessoas_netoverview()
        print(f"netoverview OK  {len(p)} aparelhos de pessoas online na última varredura")
    except Exception as ex:  # noqa: BLE001
        ok = False
        print(f"netoverview FALHA  {ex}")
    try:
        print(f"waha      {'OK ' if WAHA_KEY else 'SEM CHAVE'} sessão {WAHA_SESSION}: {waha_session_status()}")
    except Exception as ex:  # noqa: BLE001
        ok = False
        print(f"waha      FALHA  {ex}")
    print(f"mode={MODE} group={'ok' if GROUP_JID else 'VAZIO'} test={'ok' if TEST_JID else 'VAZIO'} "
          f"estado→{AVISAR_ESTADO} armar_apos={ARMAR_APOS_MIN}min janela={JANELA_S}s pre={PRE_S}s "
          f"episodio={EPISODIO_MIN}min tetos={MAX_HORA}/h {MAX_DIA}/dia forcar={int(FORCAR_ARMADO)}")
    return 0 if ok else 1


def status():
    st = load_state()
    a = st.get("armado")
    print(f"vigia: {'LIGADO' if a else ('DESLIGADO' if a is False else 'sem leitura ainda')}"
          f"{' desde ' + quando(st['armado_desde']) if st.get('armado_desde') else ''}")
    r = st.get("rede") or {}
    print(f"WiFi ({r.get('fonte') or '?'}, {quando(r['ts']) if r.get('ts') else 'nunca'}): "
          f"{', '.join(r.get('pessoas') or []) or 'vazio'}"
          f"{' — vazio desde ' + quando(st['rede_vazia_desde']) if st.get('rede_vazia_desde') else ''}")
    ep = st.get("episodio")
    if ep:
        print(f"episódio: {quando(ep['t0'])} {ep['label']} ×{ep['n']} "
              f"{'decidido ' + ep['sev'] if ep['decidido'] else 'aguardando veredito'}"
              f"{' · pessoa após veículo' if ep.get('seguimento') else ''}")
    else:
        print("episódio: nenhum")
    print(f"envios na última hora: {sum(1 for t in st['envios'] if t > time.time() - 3600)} · "
          f"eventos vistos (24h): {len(st['seen'])}")
    return 0


def cli_veredito(arg):
    agora = time.time()
    t0 = float(arg) if arg else agora
    v = veredito(t0 - PRE_S, t0 + JANELA_S, agora)
    texto, sev = mensagem(t0, "person", 1, v)
    print(f"t0={datetime.fromtimestamp(t0, TZ):%d/%m %H:%M:%S} fonte={v['fonte']} sev={sev}")
    for k in ("conectou", "ja", "depois"):
        for nome, ts in v[k]:
            print(f"  {k:9} {nome}  {hhmm(ts) if ts else '-'}")
    print("---\n" + texto)
    return 0


def test_alert(chat):
    agora = time.time()
    evs = frigate_events(agora - 7 * 86400)
    if not evs:
        print("nenhum evento qualificável nos últimos 7 dias")
        return 1
    e = evs[-1]
    t0 = float(e["start_time"])
    v = veredito(t0 - PRE_S, t0 + JANELA_S, agora)
    texto, sev = mensagem(t0, e["label"], 1, v, prefixo="[TESTE] ")
    jpg, fonte_foto = foto(e["id"], t0)
    print(f"evento {e['id']} ({e['label']}, {datetime.fromtimestamp(t0, TZ):%d/%m %H:%M}) "
          f"foto={fonte_foto} fonte={v['fonte']} → {chat}\n{texto}")
    print(f"WAHA HTTP {send(chat, texto, jpg)}")
    return 0


def main():
    args = sys.argv[1:]
    if args[:1] == ["--selftest"]:
        return selftest()
    if args[:1] == ["--status"]:
        return status()
    if args[:1] == ["--veredito"]:
        return cli_veredito(args[1] if len(args) > 1 else "")
    if args[:1] == ["--test-alert"]:
        chat = args[1] if len(args) > 1 else TEST_JID
        if not chat:
            sys.exit("--test-alert: informe o JID ou configure TEST_JID")
        return test_alert(chat)
    if MODE not in ("log", "shadow", "live"):
        sys.exit(f"MODE inválido: {MODE}")
    if AVISAR_ESTADO not in ("teste", "grupo", "nao"):
        sys.exit(f"AVISAR_ESTADO inválido: {AVISAR_ESTADO}")
    st = load_state()
    first = not st["seen"] and not st.get("episodio")
    log(f"canteiro-vigia MODE={MODE} estado→{AVISAR_ESTADO} armar_apos={ARMAR_APOS_MIN}min janela={JANELA_S}s "
        f"episodio={EPISODIO_MIN}min frigate={FRIGATE_URL} roteador={ROUTER_GRPC}")
    while True:
        try:
            one_round(st, first=first)
            first = False
            save_state(st)
            heartbeat(st, True)
        except Exception as ex:  # noqa: BLE001
            log(f"rodada falhou: {type(ex).__name__}: {ex}")
            heartbeat(st, False, f"{type(ex).__name__}: {str(ex)[:300]}")
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main() or 0)
