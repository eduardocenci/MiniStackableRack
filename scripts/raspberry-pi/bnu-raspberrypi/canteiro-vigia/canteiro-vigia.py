#!/usr/bin/env python3
"""canteiro-vigia — vigia de fim de semana da câmera do canteiro (ARA).

Sábado e domingo (America/Sao_Paulo), qualquer PESSOA ou VEÍCULO que o Frigate
(bnu LXC 105) rastreia na câmera `canteiro` vira UM aviso no grupo WhatsApp
"Casa Céu Azul": a foto do momento + uma linha dizendo se algum aparelho entrou
na rede Wi-Fi do canteiro no minuto seguinte à detecção (pessoa amiga — quem tem
a senha do Wi-Fi pode estar lá) ou se NENHUM entrou (severidade maior, 🔴).

Como sabe do minuto: o roteador Starlink responde por gRPC (`wifi_get_clients`)
com `associatedTimeS` de cada cliente — há quantos segundos o aparelho está
associado ao Wi-Fi. Daí sai o instante exato em que cada um entrou, sem depender
da varredura ARP de 5 min do netoverview (que fica como fallback, ±5 min). O
roteador é LAN-only; chega até ele pelo relay socat `router-proxy` do ara Pi
(`ara-raspberrypi2:9000` → `192.168.1.1:9000`, mesmo desenho do starlink-proxy).

Fluxo (loop de POLL_S segundos):
  1. GET Frigate /api/events (câmera canteiro, rótulos LABELS) — eventos novos.
  2. Evento em sáb/dom abre um EPISÓDIO (t0 = start_time do primeiro evento);
     detecções seguintes dentro de EPISODIO_MIN entram no mesmo episódio e não
     geram mensagem — um aviso por episódio, sucinto.
  3. Em t0 + JANELA_S: veredito pela rede (roteador → netoverview → sem leitura),
     foto (snapshot do evento com a caixa do objeto → frame da gravação → latest.jpg)
     e envio via WAHA sendImage com legenda de 2 linhas.
  4. Silêncio: depois de um aviso 🟢 (amigo) não repete outro 🟢 por
     SILENCIO_AMIGO_MIN; um 🔴 sempre sai. Tetos MAX_HORA / MAX_DIA (além deles só
     registra; uma nota de pausa vai ao TEST_JID).

MODE (env): log = decide e registra, não envia · shadow = envia ao TEST_JID
(Casa SmokeTests) com a marca SOMBRA · live = envia ao GROUP_JID (Casa Céu Azul).
Cada degrau só com OK do Eduardo. Os destinos vêm só do env — nenhuma leitura
do WhatsApp, nenhum outro chat.

CLI:
  canteiro-vigia.py                 loop (container)
  canteiro-vigia.py --selftest      Frigate, roteador, netoverview, WAHA — sem enviar
  canteiro-vigia.py --veredito [t0] imprime o veredito da rede para t0 (epoch; padrão agora)
  canteiro-vigia.py --test-alert [JID]  último evento qualificável (qualquer dia da
                                    semana) pelo fluxo completo, "[TESTE]", ao JID
                                    (padrão TEST_JID — nunca a família sem querer)

Roda no bnu-raspberrypi, container `canteiro-vigia` (docker/canteiro-vigia/).
Estado em STATE_DIR: state.json (eventos vistos, episódio aberto, contadores —
um restart nunca re-avisa), vigia.jsonl (uma linha por episódio decidido),
heartbeat.json.
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
from datetime import datetime, timedelta
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

DIAS = {int(x) for x in E("DIAS", "5,6").split(",") if x.strip()}   # weekday(): 5 = sáb, 6 = dom
JANELA_S = int(E("JANELA_S", "60"))             # "o minuto seguinte"
PRE_S = int(E("PRE_S", "120"))                  # entrou pouco ANTES da câmera ver = também conta
EPISODIO_MIN = int(E("EPISODIO_MIN", "15"))     # detecções a menos de X min do episódio aberto não avisam de novo
SILENCIO_AMIGO_MIN = int(E("SILENCIO_AMIGO_MIN", "60"))
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
    """Clientes Wi-Fi do roteador Starlink (gRPC via relay). Levanta exceção se não responder."""
    host = ROUTER_GRPC
    out = subprocess.run(
        ["grpcurl", "-plaintext", "-max-time", "10", "-d", '{"wifi_get_clients":{}}',
         host, "SpaceX.API.Device.Device/Handle"],
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
    """Foto do episódio: snapshot do evento com a caixa → frame full-res da gravação → latest.jpg."""
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


# ── Rede do canteiro ──────────────────────────────────────────────────────────
def _fixed(name, mac):
    n = (name or "").lower()
    if any(f in n for f in FIXED_NAMES):
        return True
    parts = (mac or "").lower().split(":")
    return len(parts) == 6 and ":".join(parts[:3]) in FIXED_OUIS


def _nto_names():
    """ip → apelido do netoverview (quem já tem nome bonito: 'iPhone (Jorge)')."""
    try:
        devs = http_json(f"{ARA_NTO_URL}/api/devices", timeout=10).get("devices", [])
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for d in devs:
        nick = d.get("nickname") or ""
        if d.get("ip") and nick:
            out[d["ip"]] = nick
    return out


def veredito_roteador(t0, agora=None):
    """Classifica os clientes Wi-Fi contra t0 pelo associatedTimeS do roteador."""
    agora = agora or time.time()
    nomes = _nto_names()
    conectou, ja, depois = [], [], []
    vistos = set()
    for c in router_clients():
        if (c.get("role") or "") != "CLIENT":
            continue
        name, mac, ip = c.get("name") or "", c.get("macAddress") or "", c.get("ipAddress") or ""
        if _fixed(name, mac):
            continue
        if ip:
            try:
                if ipaddress.ip_address(ip) not in LAN_CIDR:
                    continue
            except ValueError:
                pass
        rotulo = nomes.get(ip) or (name if name.lower() not in ("", "unknown") else "") \
            or f"aparelho {ip or mac[:8] or '?'}"
        if rotulo in vistos and ip:
            rotulo = f"{rotulo} (.{ip.rsplit('.', 1)[-1]})"   # dois "iPhone" sem apelido: distingue pelo IP
        vistos.add(rotulo)
        assoc_s = c.get("associatedTimeS")
        if assoc_s is None:
            ja.append((rotulo, None))        # associado, sem tempo — na rede, não dá o instante
            continue
        assoc = agora - float(assoc_s)
        if assoc < t0 - PRE_S:
            ja.append((rotulo, assoc))
        elif assoc <= t0 + JANELA_S:
            conectou.append((rotulo, assoc))
        else:
            depois.append((rotulo, assoc))
    return {"fonte": "roteador", "conectou": conectou, "ja": ja, "depois": depois}


def veredito_netoverview(t0, agora=None):
    """Fallback ±5 min: /api/presence do netoverview do ara Pi (varredura ARP)."""
    agora = agora or time.time()
    t_from = datetime.fromtimestamp(t0 - PRE_S, TZ).astimezone(ZoneInfo("UTC"))
    t_to = datetime.fromtimestamp(agora, TZ).astimezone(ZoneInfo("UTC"))
    q = urllib.parse.urlencode({"from": t_from.isoformat(), "to": t_to.isoformat()})
    data = http_json(f"{ARA_NTO_URL}/api/presence?{q}", timeout=30)
    conectou, ja, depois = [], [], []
    for d in data.get("devices", []):
        if _fixed(d.get("hostname") or d.get("display_name") or "", d.get("mac") or ""):
            continue
        try:
            if ipaddress.ip_address(d.get("ip") or "") not in LAN_CIDR:
                continue
        except ValueError:
            continue
        rotulo = d.get("nickname") or d.get("display_name") or f"aparelho {d.get('ip')}"
        first = d.get("first_seen_window_utc")
        try:
            first_ts = datetime.fromisoformat(first).timestamp() if first else None
        except ValueError:
            first_ts = None
        if d.get("present_at_window_start"):
            ja.append((rotulo, None))
        elif first_ts is not None and first_ts <= t0 + JANELA_S + 300:
            conectou.append((rotulo, first_ts))
        else:
            depois.append((rotulo, first_ts))
    return {"fonte": "netoverview", "conectou": conectou, "ja": ja, "depois": depois}


def veredito(t0):
    try:
        return veredito_roteador(t0)
    except Exception as ex:  # noqa: BLE001
        log(f"roteador sem resposta ({ex}) — tentando netoverview")
    try:
        return veredito_netoverview(t0)
    except Exception as ex:  # noqa: BLE001
        log(f"netoverview sem resposta ({ex}) — rede sem leitura")
    return {"fonte": None, "conectou": [], "ja": [], "depois": []}


def amigo(v):
    return bool(v["conectou"] or v["ja"] or v["depois"])


# ── Mensagem ──────────────────────────────────────────────────────────────────
def _lista(itens, com_hora):
    out = []
    for nome, ts in itens:
        out.append(f"{nome} ({hhmm(ts)})" if (com_hora and ts) else nome)
    return ", ".join(out)


def mensagem(t0, label, n, v, prefixo=""):
    d = datetime.fromtimestamp(t0, TZ)
    tipo = "pessoa" if label == "person" else f"veículo ({ROTULO.get(label, label)})"
    if n > 1:
        tipo += f" ×{n}"
    sev = "🟢" if amigo(v) else "🔴"
    linhas = [f"{prefixo}{sev} *Canteiro · {DIA[d.weekday()]} {d:%d/%m %H:%M}* — {tipo}"]
    if amigo(v):
        partes = []
        if v["conectou"]:
            partes.append("📱 Entrou na rede: " + _lista(v["conectou"], True))
        if v["depois"]:
            partes.append("📱 Entrou na rede depois: " + _lista(v["depois"], True))
        if v["ja"]:
            partes.append("📱 Já na rede: " + _lista(v["ja"], False))
        linhas.append(" · ".join(partes))
    else:
        linhas.append("📵 Nenhum aparelho entrou na rede do canteiro no minuto seguinte."
                      + ("" if v["fonte"] else " Rede sem leitura."))
    if v["fonte"] == "netoverview":
        linhas[-1] += " (±5 min)"
    if LIVE_URL:
        linhas.append(f"🔗 {LIVE_URL}")
    return "\n".join(linhas), sev


# ── Estado ────────────────────────────────────────────────────────────────────
def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"seen": {}, "episodio": None, "envios": [], "ultimo_amigo": 0, "pausa_avisada": None}


def save_state(st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(STATE)


def jsonl(rec):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with JSONL.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def heartbeat(ok, err=None):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(json.dumps({"ts": time.time(), "ok": ok, "mode": MODE, "erro": err}))
    except OSError:
        pass


# ── Decisão e envio ───────────────────────────────────────────────────────────
def fim_de_semana(ts):
    return datetime.fromtimestamp(ts, TZ).weekday() in DIAS


def teto_atingido(st, agora):
    hora = [t for t in st["envios"] if t > agora - 3600]
    inicio_dia = datetime.fromtimestamp(agora, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    dia = [t for t in st["envios"] if t >= inicio_dia]
    if len(hora) >= MAX_HORA:
        return f"{MAX_HORA}/hora"
    if len(dia) >= MAX_DIA:
        return f"{MAX_DIA}/dia"
    return None


def entregar(texto, jpg, destino_real):
    """Envia conforme o MODE; devolve o destino usado ('log' = nada enviado)."""
    if MODE == "log":
        return "log"
    if MODE == "shadow":
        if not TEST_JID:
            raise RuntimeError("TEST_JID vazio em MODE=shadow")
        send(TEST_JID, f"👁️ *SOMBRA* (iria ao Casa Céu Azul)\n{texto}", jpg)
        return "teste"
    if not destino_real:
        raise RuntimeError("GROUP_JID vazio em MODE=live")
    send(destino_real, texto, jpg)
    return "grupo"


def decidir_episodio(st, ep, agora):
    """t0 + JANELA_S chegou: veredito, foto, silêncio/tetos, envio. Marca o episódio como decidido."""
    t0 = ep["t0"]
    v = veredito(t0)
    texto, sev = mensagem(t0, ep["label"], ep["n"], v)
    motivo = None
    if sev == "🟢" and st.get("ultimo_amigo", 0) and t0 - st["ultimo_amigo"] < SILENCIO_AMIGO_MIN * 60:
        motivo = f"silêncio amigo ({SILENCIO_AMIGO_MIN} min)"
    teto = teto_atingido(st, agora)
    if teto and not motivo:
        motivo = f"teto {teto}"
        if st.get("pausa_avisada") != datetime.fromtimestamp(agora, TZ).strftime("%Y-%m-%d") and TEST_JID \
                and MODE != "log":
            try:
                send(TEST_JID, f"⏸️ canteiro-vigia: teto de {teto} atingido — só registrando até liberar.", None)
            except Exception as ex:  # noqa: BLE001
                log(f"nota de pausa falhou: {ex}")
            st["pausa_avisada"] = datetime.fromtimestamp(agora, TZ).strftime("%Y-%m-%d")

    destino = "nao-enviado"
    jpg = fonte_foto = None
    if motivo is None:
        jpg, fonte_foto = foto(ep["eid"], t0)
        destino = entregar(texto, jpg, GROUP_JID)
        if destino != "log":
            st["envios"].append(agora)
            st["envios"] = st["envios"][-200:]
        if sev == "🟢":
            st["ultimo_amigo"] = t0
    ep["decidido"] = agora
    ep["sev"] = sev
    rec = {"ts": agora, "t0": t0, "eid": ep["eid"], "label": ep["label"], "n": ep["n"], "sev": sev,
           "fonte": v["fonte"], "conectou": [x[0] for x in v["conectou"]], "ja": [x[0] for x in v["ja"]],
           "depois": [x[0] for x in v["depois"]], "foto": fonte_foto, "destino": destino,
           "motivo": motivo, "mode": MODE, "texto": texto}
    jsonl(rec)
    log(f"episódio {hhmm(t0)} {ep['label']} ×{ep['n']} → {sev} fonte={v['fonte']} "
        f"destino={destino}{' (' + motivo + ')' if motivo else ''}")


def one_round(st, agora=None, first=False):
    agora = agora or time.time()
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
            # primeira rodada sem estado: o que já estava no Frigate é passado, não aviso
            continue
        if not fim_de_semana(t_ini):
            continue
        if ep and t_ini <= ep["fim"] + EPISODIO_MIN * 60:
            ep["n"] += 1
            ep["eids"].append(eid)
            ep["fim"] = max(ep["fim"], t_fim)
            log(f"evento {eid} ({e['label']}) entra no episódio das {hhmm(ep['t0'])} (×{ep['n']})")
            continue
        st["episodio"] = {"t0": t_ini, "eid": eid, "label": e["label"], "n": 1, "eids": [eid],
                          "fim": t_fim, "decidido": None, "sev": None}
        log(f"episódio aberto {hhmm(t_ini)} {e['label']} ({eid}) — veredito em {JANELA_S}s")
    ep = st.get("episodio")
    if ep and not ep["decidido"] and agora >= ep["t0"] + JANELA_S:
        decidir_episodio(st, ep, agora)
    if ep and ep["decidido"] and agora > ep["fim"] + EPISODIO_MIN * 60:
        log(f"episódio das {hhmm(ep['t0'])} encerrado (×{ep['n']})")
        st["episodio"] = None


# ── CLI ───────────────────────────────────────────────────────────────────────
def selftest():
    ok = True
    try:
        ver = http_bytes(f"{FRIGATE_URL}/api/version", timeout=10).decode().strip()   # texto puro ("0.17.1-")
        evs = frigate_events(time.time() - 86400)
        print(f"frigate   OK  {ver}  eventos 24h ({FRIGATE_CAMERA}, {','.join(LABELS)}): {len(evs)}")
    except Exception as ex:  # noqa: BLE001
        ok = False
        print(f"frigate   FALHA  {ex}")
    try:
        cl = router_clients()
        pessoas = [c for c in cl if (c.get("role") or "") == "CLIENT"
                   and not _fixed(c.get("name") or "", c.get("macAddress") or "")]
        print(f"roteador  OK  {ROUTER_GRPC}: {len(cl)} entradas, {len(pessoas)} não-fixas")
        for c in pessoas:
            a = c.get("associatedTimeS")
            print(f"            {c.get('name') or '?':24} {c.get('ipAddress') or '-':15} "
                  f"associado há {int(a) if a is not None else '?'} s")
    except Exception as ex:  # noqa: BLE001
        ok = False
        print(f"roteador  FALHA  {ex}")
    try:
        devs = http_json(f"{ARA_NTO_URL}/api/devices", timeout=10).get("devices", [])
        print(f"netoverview OK  {len(devs)} devices, {sum(1 for d in devs if d.get('online'))} online")
    except Exception as ex:  # noqa: BLE001
        ok = False
        print(f"netoverview FALHA  {ex}")
    try:
        print(f"waha      {'OK ' if WAHA_KEY else 'SEM CHAVE'} sessão {WAHA_SESSION}: {waha_session_status()}")
    except Exception as ex:  # noqa: BLE001
        ok = False
        print(f"waha      FALHA  {ex}")
    print(f"mode={MODE} group={'ok' if GROUP_JID else 'VAZIO'} test={'ok' if TEST_JID else 'VAZIO'} "
          f"dias={sorted(DIAS)} janela={JANELA_S}s pre={PRE_S}s episodio={EPISODIO_MIN}min "
          f"silencio={SILENCIO_AMIGO_MIN}min tetos={MAX_HORA}/h {MAX_DIA}/dia")
    return 0 if ok else 1


def cli_veredito(arg):
    t0 = float(arg) if arg else time.time()
    v = veredito(t0)
    texto, sev = mensagem(t0, "person", 1, v)
    print(f"t0={datetime.fromtimestamp(t0, TZ):%d/%m %H:%M:%S} fonte={v['fonte']} sev={sev}")
    for k in ("conectou", "ja", "depois"):
        for nome, ts in v[k]:
            print(f"  {k:9} {nome}  {hhmm(ts) if ts else '-'}")
    print("---\n" + texto)
    return 0


def test_alert(chat):
    evs = frigate_events(time.time() - 7 * 86400)
    if not evs:
        print("nenhum evento qualificável nos últimos 7 dias")
        return 1
    e = evs[-1]
    t0 = float(e["start_time"])
    v = veredito(t0)
    texto, sev = mensagem(t0, e["label"], 1, v, prefixo="[TESTE] ")
    jpg, fonte_foto = foto(e["id"], t0)
    print(f"evento {e['id']} ({e['label']}, {datetime.fromtimestamp(t0, TZ):%d/%m %H:%M}) "
          f"foto={fonte_foto} fonte={v['fonte']} → {chat}\n{texto}")
    st = send(chat, texto, jpg)
    print(f"WAHA HTTP {st}")
    return 0


def main():
    args = sys.argv[1:]
    if args[:1] == ["--selftest"]:
        return selftest()
    if args[:1] == ["--veredito"]:
        return cli_veredito(args[1] if len(args) > 1 else "")
    if args[:1] == ["--test-alert"]:
        chat = args[1] if len(args) > 1 else TEST_JID
        if not chat:
            sys.exit("--test-alert: informe o JID ou configure TEST_JID")
        return test_alert(chat)
    if MODE not in ("log", "shadow", "live"):
        sys.exit(f"MODE inválido: {MODE}")
    st = load_state()
    first = not st["seen"] and not st.get("episodio")
    log(f"canteiro-vigia MODE={MODE} dias={sorted(DIAS)} janela={JANELA_S}s episodio={EPISODIO_MIN}min "
        f"silencio={SILENCIO_AMIGO_MIN}min frigate={FRIGATE_URL} roteador={ROUTER_GRPC}")
    while True:
        try:
            one_round(st, first=first)
            first = False
            save_state(st)
            heartbeat(True)
        except Exception as ex:  # noqa: BLE001
            log(f"rodada falhou: {type(ex).__name__}: {ex}")
            heartbeat(False, f"{type(ex).__name__}: {str(ex)[:300]}")
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main() or 0)
