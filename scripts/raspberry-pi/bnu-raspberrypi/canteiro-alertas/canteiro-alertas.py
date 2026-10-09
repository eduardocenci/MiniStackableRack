#!/usr/bin/env python3
"""canteiro-alertas — alertas de caminhão da câmera do canteiro no WhatsApp.

home-ara decisão 0010, fase 2 (decisão Eduardo 09/10/2026: "Implement for 'Casa Céu Azul'").

Loop a cada POLL_S segundos:
  Frigate (bnu LXC 105) /api/events da câmera `canteiro`, rótulos de veículo →
  evento com ≥ MIN_DUR_S → snapshot (o mesmo ?quality=80 dos pacotes do diário) →
  OpenAI Decisions API (perguntas V2 do home-ara, `detail=auto`) →
  canteiro.alertas.Motor (episódios, horário silencioso, tetos, estado persistido) →
  WAHA sendImage.

A lógica é o pacote `canteiro/` do home-ara (submódulo homes/ara), copiado para a
imagem canteiro-jobs — este arquivo só faz a E/S.

MODE (env):
  log     decide e grava em alertas.jsonl, NÃO envia nada (padrão — primeiro deploy)
  shadow  envia ao TEST_JID (Casa SmokeTests) com a marca "SOMBRA"
  live    envia ao GROUP_JID ("Casa Céu Azul")
O aviso de pausa por teto vai sempre ao TEST_JID. Os destinos vêm só do env —
nunca de conteúdo. Falha da OpenAI = sem alerta (só log), nunca o Frigate cru.

Uso:
  canteiro-alertas.py                 loop
  canteiro-alertas.py --once          uma rodada e sai
  canteiro-alertas.py --selftest      1 chamada mínima à Decisions API (chave + IP) e sai
  canteiro-alertas.py --test-alert JID  envia um alerta de exemplo com o snapshot do
                                        último evento de veículo ao JID dado (use o SmokeTests)
"""
import base64
import datetime as dt
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))   # /app/canteiro (cópia do home-ara)

from canteiro.alertas import Classif, Config, Motor  # noqa: E402
from canteiro.decisions import (MAX_SIDE, BudgetExceeded, Client, build_payload,  # noqa: E402
                                data_url, estimate_image_tokens, estimate_text_tokens,
                                prepare_image, usd)
from canteiro.questions import contexto, question_set  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
E = os.environ.get
MODE = E("MODE", "log")
WAHA_URL = E("WAHA_URL", "http://10.1.1.126:3000")
WAHA_KEY = E("WAHA_KEY", "")
WAHA_SESSION = E("WAHA_SESSION", "default")
GROUP_JID = E("GROUP_JID", "")                 # "Casa Céu Azul" (ARA_ALERTAS_GROUP_JID)
TEST_JID = E("TEST_JID", "")                   # Casa SmokeTests
OPENAI_API_KEY = E("OPENAI_API_KEY", "")       # a mesma chave do bnu HA (allowlist de IP do bnu)
FRIGATE_URL = E("FRIGATE_URL", "http://bnu-frigate:5000")
FRIGATE_CAMERA = E("FRIGATE_CAMERA", "canteiro")
LABELS = E("LABELS", "car,bus,motorcycle")
STATE_DIR = Path(E("STATE_DIR", "/var/lib/canteiro-alertas"))
POLL_S = int(E("POLL_S", "15"))
STALE_S = int(E("STALE_S", "1800"))            # evento mais velho que isso na 1ª vez = só marca visto
BUDGET_USD_DAY = float(E("BUDGET_USD_DAY", "0.30"))
VERSION = E("QUESTIONS", "v2")

CFG = Config(thr=float(E("THR", "0.85")), min_dur_s=int(E("MIN_DUR_S", "10")),
             pour_alerts=E("POUR_ALERTS", "0") == "1")
STATE = STATE_DIR / "state.json"
JSONL = STATE_DIR / "alertas.jsonl"
HEARTBEAT = STATE_DIR / "heartbeat.json"
QS = question_set("veiculos", VERSION)
CTX = contexto("veiculos", VERSION)


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def http_json(url: str, timeout: int = 30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def http_bytes(url: str, timeout: int = 30) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------- WhatsApp (WAHA)
def waha_post(endpoint: str, payload: dict, timeout: int = 60) -> int:
    req = urllib.request.Request(f"{WAHA_URL}/api/{endpoint}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "X-Api-Key": WAHA_KEY})
    last = None
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"WAHA {endpoint}: {last}")


def send(chat: str, text: str, jpg: bytes | None) -> int:
    if jpg:
        return waha_post("sendImage", {"session": WAHA_SESSION, "chatId": chat, "caption": text,
                                       "file": {"mimetype": "image/jpeg", "filename": "canteiro.jpg",
                                                "data": base64.b64encode(jpg).decode()}})
    return waha_post("sendText", {"session": WAHA_SESSION, "chatId": chat, "text": text})


def deliver(m, jpg: bytes | None) -> str:
    """Envia uma Mensagem do motor conforme o MODE; devolve o destino usado (ou 'log')."""
    if MODE == "log":
        return "log"
    if m.destino == "teste" or MODE == "shadow":
        chat = TEST_JID
        text = m.texto if m.destino == "teste" else f"👁️ *SOMBRA* (iria ao Casa Céu Azul)\n{m.texto}"
    else:
        chat, text = GROUP_JID, m.texto
    if not chat:
        raise RuntimeError(f"destino vazio para {m.tipo} (MODE={MODE})")
    send(chat, text, jpg)
    return "teste" if chat == TEST_JID else "grupo"


# ---------------------------------------------------------------- estado
def load_state() -> dict | None:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def save_state(motor: Motor) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(motor.estado(), ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE)


def jsonl(rec: dict) -> None:
    with JSONL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- classificação
class Classifier:
    """Decisions API com teto diário; cache em STATE_DIR/cache (trilha de auditoria)."""

    def __init__(self):
        self.day = None
        self.client = None

    def _client(self) -> Client:
        d = dt.datetime.now(TZ).date()
        if d != self.day:
            self.day, self.client = d, Client(STATE_DIR / "cache", budget_usd=BUDGET_USD_DAY,
                                              api_key=OPENAI_API_KEY)
        return self.client

    def __call__(self, raw: bytes) -> tuple[Classif | None, str | None, dict]:
        img, size = prepare_image(raw, MAX_SIDE)
        payload = build_payload(QS, [data_url(img)], CTX, "auto")
        est = usd(estimate_image_tokens(size, "auto") + estimate_text_tokens(QS, CTX))
        try:
            dec = self._client().decide(payload, est)
        except BudgetExceeded as e:
            return None, f"teto diário: {e}", {}
        except Exception as e:  # noqa: BLE001 — HTTPError, rede, retries esgotados
            return None, f"{type(e).__name__}: {str(e)[:200]}", {}
        c = Classif.from_answers(dec.answers)
        meta = {"tokens": dec.input_tokens, "cached": dec.cached, "latency_s": dec.latency_s}
        return c, (None if c else "recusa em 'veiculo'"), meta


# ---------------------------------------------------------------- loop
def frigate_events(after: float) -> list[dict]:
    q = urllib.parse.urlencode({"camera": FRIGATE_CAMERA, "labels": LABELS, "after": int(after),
                                "limit": 200})
    return http_json(f"{FRIGATE_URL}/api/events?{q}")


def snapshot(eid: str) -> bytes | None:
    try:
        return http_bytes(f"{FRIGATE_URL}/api/events/{eid}/snapshot.jpg?quality=80")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None          # ainda sem snapshot — tenta na próxima rodada
        raise


def one_round(motor: Motor, classify: Classifier, first: bool) -> bool:
    now = time.time()
    changed = False
    for m in motor.tick(now):
        dest = deliver(m, None)
        jsonl({"ts": now, "tipo": m.tipo, "destino": dest, "texto": m.texto})
        changed = True
    for e in sorted(frigate_events(now - 7200), key=lambda e: e["start_time"]):
        eid, start = e["id"], float(e["start_time"])
        last = float(e.get("end_time") or now)
        if motor.visto(eid):
            motor.atualizar(eid, last)
            continue
        if last - start < CFG.min_dur_s:
            continue
        if first and now - start > STALE_S:      # backlog da partida: não alerta o passado
            motor.ignorar(eid, now, "backlog da partida (> STALE_S)")
            changed = True
            continue
        raw = snapshot(eid)
        if raw is None:
            continue
        c, err, meta = classify(raw)
        box = (e.get("data") or {}).get("box")
        msgs = motor.processar(eid, start, last, box, c, now)
        rec = {"ts": now, "id": eid, "inicio": start, "dur_s": round(last - start), "label": e.get("label"),
               "p_caminhao": round(c.p_truck, 3) if c else None, "classe": c.truck_class if c else None,
               "p_entrega": c.p_entrega if c else None, "p_concretagem": c.p_concretagem if c else None,
               "erro": err, **meta, "mensagens": []}
        for m in msgs:
            try:
                dest = deliver(m, raw if m.evento == eid else None)
            except Exception as ex:  # noqa: BLE001
                dest = f"FALHOU: {ex}"
                log(f"envio falhou ({m.tipo}): {ex}")
            rec["mensagens"].append({"tipo": m.tipo, "destino": dest, "texto": m.texto})
            log(f"{m.tipo} → {dest}: {m.texto.splitlines()[0]}")
        jsonl(rec)
        changed = True
    return changed


def heartbeat(ok: bool, err: str | None = None) -> None:
    HEARTBEAT.write_text(json.dumps({"ts": time.time(), "ok": ok, "mode": MODE, "erro": err}),
                         encoding="utf-8")


def selftest() -> int:
    payload = {"model": "gpt-6-luna", "input": "O céu é azul?",
               "questions": [{"type": "predicate", "name": "ok", "instructions": "A frase é uma pergunta?"}]}
    req = urllib.request.Request("https://api.openai.com/v1/decisions", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {OPENAI_API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print("selftest ok:", r.status, json.load(r).get("answers"))
            return 0
    except urllib.error.HTTPError as e:
        print("selftest FALHOU:", e.code, e.read()[:300].decode(errors="replace"))
        return 1


def test_alert(chat: str) -> int:
    evs = frigate_events(time.time() - 7 * 86400)
    evs = [e for e in evs if e.get("has_snapshot", True)]
    if not evs:
        print("nenhum evento de veículo nos últimos 7 dias")
        return 1
    e = max(evs, key=lambda e: e["start_time"])
    raw = snapshot(e["id"])
    t = dt.datetime.fromtimestamp(e["start_time"], TZ)
    send(chat, f"🧪 *Teste do canteiro-alertas* — snapshot do evento {t:%d/%m %H:%M}\n"
               "_Alerta automático da câmera · mensagem de teste_", raw)
    print("enviado a", chat)
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if "--test-alert" in sys.argv:
        i = sys.argv.index("--test-alert")
        if len(sys.argv) <= i + 1:
            sys.exit("--test-alert exige o JID explícito (use o SmokeTests)")
        return test_alert(sys.argv[i + 1])
    if MODE not in ("log", "shadow", "live"):
        sys.exit(f"MODE inválido: {MODE}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    motor = Motor(CFG, load_state())
    classify = Classifier()
    log(f"canteiro-alertas MODE={MODE} limiar={CFG.thr} gatilho={CFG.min_dur_s}s perguntas={VERSION} "
        f"concretagem={'on' if CFG.pour_alerts else 'off'}")
    first = True
    while True:
        try:
            if one_round(motor, classify, first):
                save_state(motor)
            heartbeat(True)
            first = False
        except Exception as ex:  # noqa: BLE001 — Frigate fora, rede: tenta de novo na próxima
            log(f"rodada falhou: {ex}")
            traceback.print_exc()
            heartbeat(False, str(ex)[:300])
        if "--once" in sys.argv:
            return 0
        time.sleep(POLL_S)


if __name__ == "__main__":
    raise SystemExit(main())
