#!/usr/bin/env python3
"""
Ronda da Casa — proactive scene check against ground-truth references
Deploy to: /config/scripts/frigate_scene_check.py

Usage: python3 frigate_scene_check.py [night|rain|away|manual]   # run a check profile
       python3 frigate_scene_check.py capture [cam ...]     # snapshot current scene as ground truth
       python3 frigate_scene_check.py selftest [cam]        # box+gate+draw one cam → SmokeTests only

On a positive verdict the pipeline locates each finding (grounding call), re-checks it with a
focused crop compare (the bbox verification gate — rejects weather/lighting false positives the
whole-scene verdict lets through) and draws the surviving box on the alert image. Grounding and
gate use the same OpenAI-primary / Ollama-fallback policy as the verdict; image ops use ffmpeg
(no Pillow dependency) and fail open.

Unlike frigate_digest.py (event-driven), this script is proactive: it fetches a CURRENT
snapshot per camera (Frigate /api/<cam>/latest.jpg — no event needed), compares it against
an "all clear" reference image with the VLM, and notifies WhatsApp ONLY when a checklist
item is off (clothes on the varal, gates/doors open, objects left outside).

Reads all config from /config/secrets.yaml; profiles from /config/frigate_scene_check_profiles.yaml.
Logs to /config/frigate_scene_check.log.

Shared helpers (load_secrets, property context, baseline ladder, call_ollama with the
num_predict retry ladder, call_openai, debug senders, daemonize/lock boilerplate) are
COPIED from frigate_digest.py rather than imported: that module configures root logging
onto its own log file at import time. If a third consumer appears, extract a shared
vlm.py next to waha.py.

SECURITY CONSTRAINT (same as frigate_digest.py / frigate_whatsapp.py):
  Sends to exactly ONE destination: secrets["whatsapp_group_jid"]
  (plus secrets["whatsapp_smoketest_jid"] for debug). Zero read calls to the WhatsApp API.
"""

import base64
import datetime
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import traceback

import requests
import yaml

from waha import WahaClient  # shared WAHA (WhatsApp HTTP API) client, /config/scripts/waha.py

# ── Constants ─────────────────────────────────────────────────────────────────
HA_URL           = "http://localhost:8123"
OLLAMA_HOST      = "10.1.1.50"  # bnu-proxmox LAN IP — socat proxy forwards to ply-desktop:11434
OLLAMA_PORT      = 11434
OLLAMA_MODEL     = "qwen3-vl:8b"
OPENAI_MODEL     = "gpt-4.1-mini"  # vision-capable, NON-reasoning — immune to the thinking runaway
# Exemplar gate only (see GATE_EXEMPLAR_PROMPT): discriminating a dark closed door from a
# dark open doorway is beyond 4.1-mini — benched 2026-07-15 on real fundos_overview frames:
# mini 4/18 vs full 15/18 (all 12 closed-door FPs refuted, clean open-door TP confirmed 3/3).
# Only fires on region-anchored findings that have exemplars (~a few calls/night).
OPENAI_GATE_MODEL = "gpt-4.1"
# Which backend answers the verdict first; the other is the fallback. Unlike the digest (rich
# narrative, runs on every Frigate event → local GPU to avoid cloud cost), this is a BINARY
# verdict at low frequency (~5 runs/night). qwen3-vl's thinking phase can't be reliably disabled
# for the multi-check comparison prompt — measured 2026-07-06 it stochastically runs away (empty
# done_reason="length") even with /no_think at num_predict=2048 — so gpt-4.1-mini (non-reasoning,
# ~1-2 s, reliable, ~$1/month at this cadence) is primary. Flip to "ollama" to prefer the local
# GPU (accepting the runaway → slow OpenAI fallback). Ollama is always the fallback when primary.
LLM_PRIMARY      = "openai"   # "openai" | "ollama"
LOG_FILE         = "/config/frigate_scene_check.log"
LOCK_FILE        = "/config/frigate_scene_check.lock"    # separate from frigate_digest.lock —
                                                         # both pipelines may legitimately run at once
STATE_FILE       = "/config/frigate_scene_check.state.json"  # per-finding suppression memory
PROFILES_FILE    = "/config/frigate_scene_check_profiles.yaml"
PROMPT_FILE      = "/config/frigate_scene_check_prompt.txt"  # user-editable, falls back to DEFAULT_PROMPT
PROPERTY_CTX     = "/config/frigate_property_context.txt"
BASELINES_DIR    = "/config/frigate_scene_baselines"     # scene-check ground truth ("all clear")
DIGEST_BASELINES_DIR = "/config/frigate_baselines"       # digest baselines double as fallback anchors
EXEMPLARS_DIR    = "/config/frigate_scene_exemplars"     # ground-truth STATE exemplars for the
                                                         # exemplar gate (see load_state_exemplars)
SNAPSHOT_H       = 720   # latest.jpg height; bump per camera only if a check underperforms
DAY_START        = 6     # 06:00 — matches frigate_digest.py's day/night split
DAY_END          = 20    # 20:00
CONFIRM_DELAY_S  = 20    # wait before the 2-of-2 confirmation snapshot (kills one-frame glitches)
MAX_ALERT_IMAGES = 4     # cap on snapshot images attached to a family-group alert
LLM_TIMEOUT      = 600   # seconds — hard per-attempt wall clock (SIGALRM)
# Ollama is the FALLBACK here (see LLM_PRIMARY) precisely because its thinking phase is
# unreliable for this task. Prepending Qwen3's `/no_think` soft-switch (OLLAMA_NO_THINK) cuts
# thinking ~10× vs full strength, but does NOT eliminate it: thinking length is stochastic and
# scales with prompt size (a multi-check camera runs longer), so it still overflows num_predict
# at random and returns an EMPTY done_reason="length" response. num_predict=2048 + the ×1.5 ladder
# (2048→3072→4608) reduces how often that happens; when it still fails, the caller falls back to
# the primary/other backend. (The Ollama `think:false` PARAM is deliberately NOT used — on this
# model it INCREASED thinking vs /no_think.)
OLLAMA_NO_THINK     = "/no_think"
OLLAMA_NUM_CTX      = 16384
OLLAMA_NUM_PREDICT  = 2048
OLLAMA_MAX_ATTEMPTS = 3
SEND_ATTEMPTS    = 3     # WhatsApp send retries

# ── Bounding-box verification gate ────────────────────────────────────────────
# On a positive verdict, locate the finding (grounding call) and re-check it with a
# focused crop compare before alerting; the surviving box is drawn on the alert image.
# Same OpenAI-primary / Ollama-fallback policy as the verdict (no model mixing).
ENABLE_BBOX_GATE = True
BBOX_PAD_FRAC    = 0.45  # crop padding around the located box (fraction of box size)
REGION_PAD_FRAC  = 0.15  # smaller pad for configured region anchors (already generous)

# ── Region anchors ─────────────────────────────────────────────────────────────
# A check may pin WHERE its object lives per camera (`regions:` in the profiles file,
# 0-1000 normalized [x0,y0,x1,y1]). Anchored checks get the region drawn as a colored
# marker box on the REFERENCE image sent to the verdict (the prompt tells the model to
# look only there), and the region replaces the model-grounding bbox in the verification
# gate — added after a night run flagged "portão aberto" while looking at a WINDOW: the
# whole-scene verdict, the grounding call and the gate all examined the wrong object.
REGION_COLORS = [("green", "VERDE"), ("blue", "AZUL"),
                 ("yellow", "AMARELO"), ("magenta", "MAGENTA")]
FFMPEG           = "/usr/bin/ffmpeg"  # present in the HA core container
                                      # (frigate_whatsapp.py shells out to it in prod);
                                      # every ffmpeg op fails OPEN so a missing binary
                                      # degrades to "send without box", never a crash

_handler = logging.FileHandler(LOG_FILE)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [scene_check] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger(__name__)


# ── Secrets / HA state (from frigate_digest.py:118) ───────────────────────────
def load_secrets() -> dict:
    with open("/config/secrets.yaml") as f:
        return yaml.safe_load(f)


def get_ha_state(entity_id: str, token: str) -> str:
    try:
        r = requests.get(f"{HA_URL}/api/states/{entity_id}",
                         headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if r.ok:
            return r.json().get("state", "")
    except requests.RequestException as exc:
        log.warning("get_ha_state(%s) failed: %s", entity_id, exc)
    return ""


# ── Property context (from frigate_digest.py:386) ─────────────────────────────
def load_property_context(path: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """Returns (raw_text, camera_to_location, zone_to_label)."""
    camera_to_location: dict[str, str] = {}
    zone_to_label: dict[str, str] = {}
    if not os.path.exists(path):
        log.info("Property context file not found: %s — using camera names", path)
        return "", camera_to_location, zone_to_label

    with open(path, encoding="utf-8") as f:
        raw = f.read()

    section = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        low = stripped.lower()
        if stripped.startswith("[") and stripped.endswith("]"):
            if low.startswith("[câmera") or low.startswith("[camera"):
                section = "cameras"
            elif low.startswith("[zona") or low.startswith("[zone"):
                section = "zones"
            else:
                section = ""
            continue
        if section and ":" in stripped:
            key, _, val = stripped.partition(":")
            key, val = key.strip(), val.strip()
            if key and val:
                if section == "cameras":
                    camera_to_location[key] = val
                else:
                    zone_to_label[key] = val

    clean_lines = [l for l in raw.splitlines() if not l.strip().startswith("#")]
    context = "\n".join(clean_lines).strip()
    return context, camera_to_location, zone_to_label


def humanize_cam(cam: str, camera_to_location: dict[str, str]) -> str:
    """Human location label for a camera (frente_principal → Frente Principal).
    Keeps camera tech names out of messages."""
    label = camera_to_location.get(cam)
    if label:
        return label
    return cam.replace("_", " ").strip().title()


# ── Profiles config ───────────────────────────────────────────────────────────
def load_profiles(path: str = PROFILES_FILE) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg.get("checks") or not cfg.get("profiles"):
        raise ValueError(f"{path} must define 'checks' and 'profiles'")
    return cfg


def resolve_profile(cfg: dict, profile: str) -> tuple[dict[str, list[dict]], dict]:
    """Returns (camera → [check dicts with 'id'], profile options).
    Each camera gets only the checks that list it."""
    prof = cfg["profiles"].get(profile)
    if prof is None:
        raise ValueError(f"Unknown profile '{profile}' in {PROFILES_FILE}")
    cam_checks: dict[str, list[dict]] = {}
    for check_id in prof.get("checks", []):
        check = cfg["checks"].get(check_id)
        if not check:
            log.warning("Profile %s references unknown check '%s' — skipping", profile, check_id)
            continue
        for cam in check.get("cameras", []):
            cam_checks.setdefault(cam, []).append({**check, "id": check_id})
    return cam_checks, prof


def all_configured_cameras(cfg: dict) -> list[str]:
    cams: list[str] = []
    for check in cfg["checks"].values():
        for cam in check.get("cameras", []):
            if cam not in cams:
                cams.append(cam)
    return cams


# ── Snapshots and references ──────────────────────────────────────────────────
def fetch_latest(base: str, camera: str, h: int = SNAPSHOT_H) -> bytes | None:
    """Current on-demand snapshot — no Frigate event required."""
    try:
        r = requests.get(f"{base}/api/{camera}/latest.jpg", params={"h": h}, timeout=30)
        r.raise_for_status()
        return r.content
    except requests.RequestException as exc:
        log.warning("fetch_latest(%s) failed: %s", camera, exc)
        return None


def load_reference(camera: str, is_night: bool) -> tuple[bytes, str] | None:
    """Ground-truth image for a camera: scene baselines first, digest baselines as
    fallback (they are 'nothing happening' scenes, which usually also means gates
    closed / varal empty). Day/night ladder as in frigate_digest.py:449."""
    suffix = "_night.jpg" if is_night else "_day.jpg"
    for directory in (BASELINES_DIR, DIGEST_BASELINES_DIR):
        for name in (f"{camera}{suffix}", f"{camera}.jpg"):
            path = os.path.join(directory, name)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    return f.read(), path
    return None


def load_state_exemplars(camera: str, check_id: str, is_night: bool) -> tuple[bytes, bytes] | None:
    """(normal_bytes, anomaly_bytes) ground-truth STATE pair for a camera+check, or None.

    Full frames in EXEMPLARS_DIR named {camera}__{check}__normal*.jpg / __anomaly*.jpg —
    e.g. fundos_overview__portoes__anomaly_day.jpg is a REAL capture of the laundry door
    open (2026-07-15). The gate crops them with the same region box as the current
    snapshot, so they must come from the same camera framing (latest.jpg geometry).
    Day/night ladder with cross-fallback: a day exemplar still anchors a night gate —
    GATE_EXEMPLAR_PROMPT compares STRUCTURAL state, not lighting. Both states must
    resolve, else None (a lone exemplar can't anchor the A/B comparison)."""
    suffix = "_night" if is_night else "_day"
    other = "_day" if is_night else "_night"
    pair: list[bytes] = []
    for state in ("normal", "anomaly"):
        for suf in (suffix, other, ""):
            path = os.path.join(EXEMPLARS_DIR, f"{camera}__{check_id}__{state}{suf}.jpg")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    pair.append(f.read())
                break
        else:
            return None
    return pair[0], pair[1]


# ── Prompt ────────────────────────────────────────────────────────────────────
DEFAULT_PROMPT = """\
Você compara duas imagens da MESMA câmera de segurança residencial. A PRIMEIRA imagem é a
REFERÊNCIA ("tudo certo": tudo guardado e fechado). A SEGUNDA imagem é a cena ATUAL.
Local da câmera: __LOCATION__.
__TIME_NOTE__
Verifique APENAS os itens desta lista — nada além dela:
__CHECKS__

REGRAS:
- Compare a cena ATUAL com a REFERÊNCIA; reporte apenas diferenças que correspondam aos
  itens da lista acima.
- Compare a PRESENÇA de objetos, não a APARÊNCIA deles. Um objeto que aparece nas DUAS
  imagens NÃO é um achado, mesmo que pareça diferente por causa da luz ou da umidade.
- Ignore por completo diferenças de: iluminação, brilho, sombras, reflexos, CHÃO MOLHADO vs
  SECO, chuva, poças, vento na vegetação, qualidade de imagem, carimbo de hora/data e
  pequenas mudanças de ângulo ou cor. Nada disso é uma anomalia.
- Objetos permanentes que já aparecem na REFERÊNCIA (vasos, plantas, móveis de jardim,
  decoração fixa) NUNCA são achados — nem quando ressaltam mais sob luz ou piso seco.
- Ignore pessoas e animais: a ronda procura OBJETOS e ESTADOS (portas, portões, roupas,
  objetos deixados), não movimento.
- Reporte um item SOMENTE se tiver certeza visual clara na imagem ATUAL. Na dúvida, NÃO reporte.
- Responda em português do Brasil.
__REFERENCES__
FORMATO DA RESPOSTA (obrigatório):
- Primeira linha: exatamente "ALERTA: SIM" ou "ALERTA: NAO".
- Se SIM: uma linha por achado, no formato "- [id] descrição curta e factual".
- O [id] deve ser EXATAMENTE um dos ids entre colchetes da lista acima — NÃO invente ids.
  Ex.: uma bicicleta, cadeira, brinquedo ou ferramenta deixada é "[objetos]", nunca "[bicicleta]".
- Se NAO: nenhuma linha adicional.
"""


def load_prompt_template() -> str:
    """User-editable template (digest convention); embedded default otherwise."""
    try:
        if os.path.exists(PROMPT_FILE):
            with open(PROMPT_FILE, encoding="utf-8") as f:
                text = f.read()
            if text.strip():
                return text
    except OSError as exc:
        log.warning("Cannot read %s (%s) — using embedded prompt", PROMPT_FILE, exc)
    return DEFAULT_PROMPT


def build_prompt(template: str, location: str, checks: list[dict],
                 is_night: bool, property_context: str) -> str:
    lines = []
    for c in checks:
        instr = " ".join(str(c.get("instruction", c.get("label", c["id"]))).split())
        anchor = (f"(verifique APENAS a região marcada em {c['_color_pt']} "
                  f"na imagem de REFERÊNCIA) " if c.get("_color_pt") else "")
        lines.append(f"- [{c['id']}] {anchor}{instr}")
    if any(c.get("_color_pt") for c in checks):
        # Injected into __CHECKS__ so the user-editable template needs no new placeholder.
        lines.append(
            "- ATENÇÃO: as caixas coloridas desenhadas na imagem de REFERÊNCIA são apenas "
            "MARCADORES da região a verificar — elas NÃO existem na cena real e NUNCA são "
            "um achado. Examine a MESMA região na imagem ATUAL (que não tem caixa).")
    checks_txt = "\n".join(lines)
    if is_night:
        time_note = ("As imagens são noturnas (infravermelho, preto e branco); a referência pode "
                     "ter sido capturada em condição de luz diferente — ignore diferenças de cor e brilho.")
    else:
        time_note = "As imagens são diurnas."
    references = f"\nCONTEXTO DA PROPRIEDADE:\n{property_context}\n" if property_context else ""
    return (template
            .replace("__LOCATION__", location)
            .replace("__TIME_NOTE__", time_note)
            .replace("__CHECKS__", checks_txt)
            .replace("__REFERENCES__", references))


# ── Verdict parsing ───────────────────────────────────────────────────────────
# Tolerant gate: optional markdown bold, any of :-–— separators (mirrors digest's _REL_GATE_RE).
_ALERT_GATE_RE = re.compile(
    r"^\s*\**\s*ALERTA\s*\**\s*[:\-–—]?\s*\**\s*(SIM|N[ÃA]O)\b",
    re.IGNORECASE | re.MULTILINE,
)
_FINDING_RE = re.compile(r"^\s*[-•*]\s*\[\s*(\w+)\s*\]\s*(.+?)\s*$", re.MULTILINE)
UNKNOWN_CHECK_ID = "outros"  # fallback bucket for a finding whose [id] isn't in the checklist


def parse_verdict(text: str, allowed_ids: set[str]) -> list[tuple[str, str]] | None:
    """Returns [(check_id, description)] — empty list means a clean 'ALERTA: NAO'.
    Returns None only when there is no usable structure (no gate line, or 'ALERTA: SIM'
    with no itemised finding at all) — the caller treats None as NO finding plus a debug
    notice, since for an 'is anything off' check a false silence beats crying wolf.

    The model often labels a real finding with an invented id (e.g. '[bicicleta]' or the
    camera name '[frente_principal]') instead of the checklist id. Dropping those would
    silently swallow genuine findings AND bypass the 2-of-2 confirmation that exists to
    weed out false positives — so an unrecognised id is remapped to the camera's check
    (its single id when unambiguous, else a generic bucket) rather than discarded."""
    m = _ALERT_GATE_RE.search(text or "")
    if not m:
        return None
    if m.group(1).upper().startswith("N"):
        return []
    raw = _FINDING_RE.findall(text)
    if not raw:
        return None  # SIM with no itemised finding line — unusable
    fallback = next(iter(allowed_ids)) if len(allowed_ids) == 1 else UNKNOWN_CHECK_ID
    return [(cid.lower() if cid.lower() in allowed_ids else fallback, desc.strip())
            for cid, desc in raw]


# ── Ollama (from frigate_digest.py:905 — retry ladder intact) ─────────────────
class _OllamaTimeout(Exception):
    pass


def call_ollama(prompt: str, images: list[bytes]) -> tuple[str | None, dict]:
    """Returns (verdict_text_or_None, stats). Retries with a ×1.5 larger num_predict each
    time the model returns an EMPTY response cut off by length (done_reason="length")."""
    img_payload = [base64.b64encode(img).decode() for img in images] if images else None
    # Disable the thinking phase for this binary verdict (see OLLAMA_NO_THINK note above).
    no_think_prompt = f"{OLLAMA_NO_THINK}\n{prompt}"
    stats: dict = {"n_images": len(images), "elapsed_s": 0.0, "timed_out": False,
                   "error": None, "attempts": 0, "done_reason": None, "backend": "ollama"}

    def _alarm_handler(signum, frame):
        raise _OllamaTimeout()

    num_predict = OLLAMA_NUM_PREDICT
    for attempt in range(1, OLLAMA_MAX_ATTEMPTS + 1):
        stats["attempts"] = attempt
        payload: dict = {
            "model":   OLLAMA_MODEL,
            "prompt":  no_think_prompt,
            "stream":  False,
            "options": {"temperature": 0.3, "num_predict": num_predict,
                        "num_ctx": max(OLLAMA_NUM_CTX, num_predict + 8192)},
        }
        if img_payload:
            payload["images"] = img_payload

        old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(LLM_TIMEOUT)  # hard per-attempt wall-clock deadline
        t0 = time.monotonic()
        try:
            log.info("Calling Ollama (%d images, attempt %d/%d, num_predict=%d)...",
                     len(images), attempt, OLLAMA_MAX_ATTEMPTS, num_predict)
            r = requests.post(
                f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/generate",
                json=payload,
                # No requests timeout — SIGALRM is the authoritative deadline
            )
            r.raise_for_status()
            data        = r.json()
            response    = data.get("response", "").strip()
            done_reason = data.get("done_reason")
            stats["elapsed_s"] += time.monotonic() - t0
            stats["done_reason"] = done_reason
            log.info("Ollama response (%d chars, done=%s): %s...",
                     len(response), done_reason, response[:120])
            if response:
                return response, stats
            if done_reason == "length" and attempt < OLLAMA_MAX_ATTEMPTS:
                num_predict = int(num_predict * 1.5)
                log.warning("Ollama empty (done=length) — retrying with num_predict=%d", num_predict)
                continue
            return None, stats
        except _OllamaTimeout:
            stats["elapsed_s"] += time.monotonic() - t0
            stats["timed_out"] = True
            log.error("Ollama call timed out after %ds", LLM_TIMEOUT)
            return None, stats
        except requests.RequestException as exc:
            stats["elapsed_s"] += time.monotonic() - t0
            stats["error"] = str(exc)
            log.error("Ollama call failed: %s", exc)
            return None, stats
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

    return None, stats


# ── OpenAI fallback (from frigate_digest.py:998) ──────────────────────────────
def call_openai(prompt: str, images: list[bytes], api_key: str,
                detail: str = "low", model: str = OPENAI_MODEL) -> str | None:
    content: list[dict] = [{"type": "text", "text": prompt}]
    for img_bytes in images:
        b64 = base64.b64encode(img_bytes).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": detail},
        })
    payload = {
        "model":       model,
        "messages":    [{"role": "user", "content": content}],
        "max_tokens":  400,
        "temperature": 0.3,
    }
    try:
        log.info("Calling OpenAI (%d images)...", len(images))
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        verdict = (data["choices"][0]["message"]["content"] or "").strip()
        log.info("OpenAI response (%d chars): %s...", len(verdict), verdict[:120])
        return verdict or None
    except requests.RequestException as exc:
        log.error("OpenAI call failed: %s", exc)
        return None


# ── Shared LLM dispatch (OpenAI primary, Ollama fallback — no model mixing) ────
def ask_llm(prompt: str, images: list[bytes], secrets: dict,
            detail: str = "low", openai_model: str = OPENAI_MODEL) -> tuple[str | None, str]:
    """One VLM pass with the configured primary backend, the other as fallback.
    Returns (text_or_None, backend_label). Used by the verdict, the grounding call
    and the gate so all three follow the same LLM_PRIMARY policy. `openai_model`
    lets the exemplar gate upgrade to OPENAI_GATE_MODEL without touching the rest."""
    api_key = secrets.get("openai_api_key", "")

    def _openai():
        return (call_openai(prompt, images, api_key, detail, openai_model), "openai") if api_key else (None, "openai")

    def _ollama():
        text, _stats = call_ollama(prompt, images)
        return text, "ollama"

    primary, fallback = (_openai, _ollama) if LLM_PRIMARY == "openai" else (_ollama, _openai)
    text, backend = primary()
    if text is None:
        text, backend = fallback()
    return text, backend


def analyze_camera(camera: str, reference: bytes, current: bytes, checks: list[dict],
                   prompt_template: str, is_night: bool, property_context: str,
                   location: str, secrets: dict) -> tuple[list[tuple[str, str]] | None, str]:
    """One VLM pass: reference + current → parsed findings.
    Returns (findings_or_None, backend_label). None = analysis failed/unparseable."""
    prompt = build_prompt(prompt_template, location, checks, is_night, property_context)
    allowed = {c["id"] for c in checks}
    images = [reference, current]  # order matters: prompt says FIRST=reference, SECOND=current
    text, backend = ask_llm(prompt, images, secrets)
    if text is None:
        log.error("Both LLM backends failed for %s (openai_key=%s)",
                  camera, bool(secrets.get("openai_api_key")))
        return None, backend
    findings = parse_verdict(text, allowed)
    if findings is None:
        log.warning("Unparseable verdict for %s (%s): %r", camera, backend, (text or "")[:200])
    return findings, backend


# ── Bounding box: locate a finding, then use the box as a false-positive gate ──
# The box does double duty: (1) it crops a tight region so a SECOND, focused compare
# can reject the weather/lighting false positives the whole-scene verdict lets through
# (e.g. permanent planters that "appear" when wet pavement dries out), and (2) it is
# drawn on the alert image. All image ops use ffmpeg (no Pillow dependency) and fail
# OPEN — a missed real alert is worse than one false positive slipping through.
BBOX_PROMPT = """\
Esta é a imagem ATUAL de uma câmera de segurança residencial (pode ser visão noturna P&B).
Um sistema detectou esta anomalia na cena:
  "__DESC__"
Localize na imagem a região exata dessa anomalia e devolva a caixa delimitadora.
Responda SOMENTE com JSON válido, sem texto extra:
{"present": true, "box_2d": [x_min, y_min, x_max, y_max]}
- Coordenadas NORMALIZADAS de 0 a 1000, origem no canto SUPERIOR ESQUERDO
  (x=0 esquerda, x=1000 direita; y=0 topo, y=1000 base).
- Se a anomalia não estiver visível, responda {"present": false, "box_2d": []}."""

GATE_PROMPT = """\
Você recebe DOIS recortes da MESMA região de uma câmera de segurança FIXA (mesmo enquadramento).
O PRIMEIRO recorte é a REFERÊNCIA (cena normal, "tudo certo"). O SEGUNDO é a cena ATUAL.
Um sistema automático SUSPEITOU desta anomalia nesta região:
  "__DESC__"
Sua tarefa é REFUTAR esse alerta. O padrão é "confirmado": false — só confirme se for inequívoco.
REGRAS:
- Responda "confirmado": true SOMENTE se houver na ATUAL um objeto físico CLARO e INEQUÍVOCO
  que NÃO existe na REFERÊNCIA.
- Um objeto ou estrutura que aparece nos DOIS recortes NÃO é novo, mesmo que a aparência mude
  por causa da luz ou da umidade → confirmado:false.
- Chão/piso vazio, textura da superfície, iluminação, brilho, sombra, reflexo, chão molhado vs
  seco, poças, ruído de imagem e carimbo de hora NÃO são objetos → confirmado:false.
- Na MENOR dúvida, responda confirmado:false.
Responda SOMENTE com JSON válido: {"confirmado": true, "motivo": "curto e factual"}"""

# State-aware variant for REGION-ANCHORED findings (portões abertos, roupas no varal):
# the generic gate above only confirms NEW OBJECTS, so it would auto-refute a genuinely
# open gate — the gate exists in both crops, only its STATE changed. Anchored regions
# frame exactly the object being checked, so here the question is whether the SUSPECTED
# CHANGE is visible, keeping the same refute-by-default posture for light/weather.
GATE_STATE_PROMPT = """\
Você recebe DOIS recortes da MESMA região de uma câmera de segurança FIXA (mesmo enquadramento),
mostrando exatamente o objeto verificado (ex.: um portão, uma porta, o varal).
O PRIMEIRO recorte é a REFERÊNCIA (estado normal, "tudo certo": portões/portas fechados,
varal vazio). O SEGUNDO é a cena ATUAL.
Um sistema automático SUSPEITOU desta mudança nesta região:
  "__DESC__"
Sua tarefa é REFUTAR esse alerta. O padrão é "confirmado": false — só confirme se for inequívoco.
REGRAS:
- Responda "confirmado": true SOMENTE se a mudança suspeitada estiver CLARA e INEQUÍVOCA na
  ATUAL em comparação com a REFERÊNCIA (ex.: portão visivelmente aberto/entreaberto que na
  referência aparece fechado; roupas/tecidos pendurados onde a referência mostra varal vazio).
- Diferenças de iluminação, brilho, sombra, reflexo, molhado vs seco, ruído de imagem e
  carimbo de hora NÃO são mudanças → confirmado:false.
- Se o objeto aparenta o MESMO estado nos dois recortes → confirmado:false.
- Na MENOR dúvida, responda confirmado:false.
Responda SOMENTE com JSON válido: {"confirmado": true, "motivo": "curto e factual"}"""

# Exemplar variant: when a REAL capture of both states exists (load_state_exemplars), the
# gate stops asking "did it change vs the reference?" and instead classifies the current
# crop against two ground-truth anchors of THIS object on THIS camera. Added 2026-07-15
# after the laundry-door incident: the day baseline itself had the door OPEN, so both the
# verdict and the ref-vs-current gates were comparing open-vs-open and suppressed a REAL
# open door; conversely, night IR makes the closed door read as an opening (chronic FPs).
# The A/B-classification framing survived both failure modes in the bench (15/18 with
# OPENAI_GATE_MODEL; all 12 closed-door FPs refuted). __CUES__ receives the per-camera
# `state_cues` text from the profiles file ("" when absent).
GATE_EXEMPLAR_PROMPT = """\
Você recebe TRÊS recortes da MESMA região de uma câmera de segurança FIXA (mesmo
enquadramento), mostrando o MESMO objeto verificado:
1) ESTADO NORMAL — registro real do objeto em estado normal ("tudo certo": porta/portão
   fechado, varal vazio).
2) ESTADO ANORMAL — registro real e CONFIRMADO da anomalia verificada neste mesmo objeto.
3) ATUAL — a cena agora.
Um sistema automático SUSPEITOU desta mudança na cena ATUAL:
  "__DESC__"
__CUES__
Sua tarefa: decidir se, na imagem ATUAL, o objeto está no estado da imagem 1 (NORMAL) ou
da imagem 2 (ANORMAL). O padrão é "confirmado": false — só confirme se a ATUAL mostrar
claramente o mesmo estado da imagem 2 (ANORMAL).
REGRAS:
- Compare o ESTADO físico do objeto (as pistas estruturais), nunca iluminação, cor,
  sombra, nitidez ou horário — os três recortes podem ser de momentos e condições de luz
  diferentes (dia colorido vs noite infravermelho P&B).
- Objetos na frente (varais, roupas, móveis) podem ocultar parte da cena — julgue pelo
  que estiver visível.
- Na MENOR dúvida, responda confirmado:false.
Responda SOMENTE com JSON válido: {"confirmado": true, "motivo": "curto e factual"}"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from the JPEG SOF marker — avoids a Pillow dependency."""
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if 0xD0 <= marker <= 0xD9:            # RSTn / SOI / EOI: no length field
            i += 2
            continue
        # SOF0..SOF15 carry the frame dimensions; skip DHT(C4)/JPG(C8)/DAC(CC).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = (data[i + 5] << 8) | data[i + 6]
            w = (data[i + 7] << 8) | data[i + 8]
            return w, h
        seg = (data[i + 2] << 8) | data[i + 3]
        i += 2 + seg
    return None


def _ffmpeg_pipe(vf_args: list[str], data: bytes, timeout: int = 30) -> bytes | None:
    """Run ffmpeg reading a JPEG on stdin, return the JPEG on stdout. None on any
    failure (fail-open: a missing/broken ffmpeg must never crash the ronda)."""
    try:
        p = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", "-",
                            *vf_args, "-f", "mjpeg", "-"],
                           input=data, capture_output=True, timeout=timeout)
        if p.returncode == 0 and p.stdout:
            return p.stdout
        log.warning("ffmpeg rc=%s: %s", p.returncode, p.stderr.decode("utf-8", "replace")[:200])
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ffmpeg unusable (%s) — image op skipped", exc)
    return None


def _box_to_px(box: list[float], w: int, h: int) -> tuple[float, float, float, float]:
    """0-1000 normalized [x0,y0,x1,y1] → pixel (x0,y0,x1,y1), clamped and ordered."""
    xs = sorted((max(0.0, min(box[0] / 1000 * w, w)), max(0.0, min(box[2] / 1000 * w, w))))
    ys = sorted((max(0.0, min(box[1] / 1000 * h, h)), max(0.0, min(box[3] / 1000 * h, h))))
    return xs[0], ys[0], xs[1], ys[1]


def _crop(img: bytes, box: list[float], pad: float = BBOX_PAD_FRAC) -> bytes | None:
    """Crop a padded region around `box`; each image uses its OWN dimensions so a
    reference at a different resolution still crops the same physical spot."""
    wh = jpeg_size(img)
    if not wh:
        return None
    w, h = wh
    x0, y0, x1, y1 = _box_to_px(box, w, h)
    bw, bh = x1 - x0, y1 - y0
    if bw < 2 or bh < 2:
        return None
    cx0 = int(max(0.0, x0 - bw * pad)); cy0 = int(max(0.0, y0 - bh * pad))
    cx1 = int(min(float(w), x1 + bw * pad)); cy1 = int(min(float(h), y1 + bh * pad))
    if cx1 - cx0 < 2 or cy1 - cy0 < 2:
        return None
    return _ffmpeg_pipe(["-vf", f"crop={cx1 - cx0}:{cy1 - cy0}:{cx0}:{cy0}"], img)


def parse_bbox(text: str) -> list[float] | None:
    """0-1000 box from a grounding reply; None if absent or present:false."""
    m = _JSON_RE.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if obj.get("present") is False:
        return None
    box = obj.get("box_2d") or obj.get("box") or obj.get("bbox")
    if not (isinstance(box, list) and len(box) == 4):
        return None
    try:
        vals = [float(v) for v in box]
    except (TypeError, ValueError):
        return None
    if max(vals) <= 1.5:            # model answered in 0-1 fractions
        vals = [v * 1000 for v in vals]
    return vals


def request_bbox(current: bytes, description: str, secrets: dict) -> list[float] | None:
    text, backend = ask_llm(BBOX_PROMPT.replace("__DESC__", description), [current],
                            secrets, detail="high")
    box = parse_bbox(text or "")
    log.info("bbox(%s): %s", backend, box)
    return box


def parse_gate(text: str) -> bool | None:
    m = _JSON_RE.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    val = obj.get("confirmado")
    return val if isinstance(val, bool) else None


def gate_finding(reference: bytes, current: bytes, box: list[float],
                 description: str, secrets: dict,
                 pad: float = BBOX_PAD_FRAC,
                 state_check: bool = False,
                 exemplars: tuple[bytes, bytes] | None = None,
                 cues: str = "") -> tuple[bool, str]:
    """Focused crop compare: is the finding REAL, ignoring weather/light? Returns
    (keep, note). Fails OPEN (keep=True) on any infra hiccup. `state_check=True`
    (region-anchored findings) judges a STATE change of the framed object instead
    of new-object presence — the object itself appears in both crops.

    `exemplars=(normal, anomaly)` upgrades the state gate to an A/B classification
    against real captures of both states (GATE_EXEMPLAR_PROMPT + OPENAI_GATE_MODEL);
    `cues` is the per-camera state_cues text injected into that prompt. Falls back
    to the ref-vs-current gate when an exemplar crop fails."""
    cur_c = _crop(current, box, pad)
    if exemplars and cur_c:
        norm_c = _crop(exemplars[0], box, pad)
        anom_c = _crop(exemplars[1], box, pad)
        if norm_c and anom_c:
            prompt = (GATE_EXEMPLAR_PROMPT
                      .replace("__DESC__", description)
                      .replace("__CUES__", cues.strip()))
            text, backend = ask_llm(prompt, [norm_c, anom_c, cur_c], secrets,
                                    detail="high", openai_model=OPENAI_GATE_MODEL)
            real = parse_gate(text or "")
            if real is None:
                return True, f"gate exemplar inconclusivo ({backend}) → mantido"
            return real, f"{'confirmado' if real else 'falso positivo'} (exemplar, {backend})"
    ref_c = _crop(reference, box, pad)
    if not ref_c or not cur_c:
        return True, "crop indisponível → mantido"
    prompt = GATE_STATE_PROMPT if state_check else GATE_PROMPT
    text, backend = ask_llm(prompt.replace("__DESC__", description),
                            [ref_c, cur_c], secrets, detail="high")
    real = parse_gate(text or "")
    if real is None:
        return True, f"gate inconclusivo ({backend}) → mantido"
    return real, f"{'confirmado' if real else 'falso positivo'} ({backend})"


def draw_boxes(img: bytes, boxes: list[list[float]]) -> bytes:
    """Draw a red rectangle for each 0-1000 box; returns the original on any failure."""
    wh = jpeg_size(img)
    if not wh or not boxes:
        return img
    w, h = wh
    thick = max(3, w // 300)
    filters = []
    for box in boxes:
        x0, y0, x1, y1 = _box_to_px(box, w, h)
        filters.append(f"drawbox=x={int(x0)}:y={int(y0)}:w={int(x1 - x0)}:h={int(y1 - y0)}"
                       f":color=red@1.0:thickness={thick}")
    return _ffmpeg_pipe(["-vf", ",".join(filters), "-q:v", "3"], img) or img


def draw_region_markers(img: bytes, regions: list[tuple[list[float], str]]) -> bytes:
    """Colored marker boxes (region anchors) on a COPY of the reference sent to the
    verdict VLM. `regions` is [(0-1000 box, ffmpeg color)]. Fails OPEN: on any ffmpeg
    problem the plain reference is used and the prompt anchor note still applies."""
    wh = jpeg_size(img)
    if not wh or not regions:
        return img
    w, h = wh
    thick = max(4, w // 200)
    filters = []
    for box, color in regions:
        x0, y0, x1, y1 = _box_to_px(box, w, h)
        filters.append(f"drawbox=x={int(x0)}:y={int(y0)}:w={int(x1 - x0)}:h={int(y1 - y0)}"
                       f":color={color}@1.0:thickness={thick}")
    return _ffmpeg_pipe(["-vf", ",".join(filters), "-q:v", "3"], img) or img


# ── Suppression state ─────────────────────────────────────────────────────────
def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict) and isinstance(state.get("findings"), dict):
            return state
    except (OSError, ValueError):
        pass
    return {"findings": {}}


def save_state(state: dict) -> None:
    """Atomic write; never raises — suppression is best-effort."""
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        log.warning("Cannot save state file: %s", exc)


def is_suppressed(state: dict, key: str, profile: str, suppress_hours: float) -> bool:
    if suppress_hours <= 0:
        return False
    entry = state["findings"].get(key) or {}
    last = (entry.get("last_notified") or {}).get(profile, 0)
    return bool(last) and (time.time() - last) < suppress_hours * 3600


# ── WhatsApp output ───────────────────────────────────────────────────────────
PROFILE_HEADERS = {
    "night":  "🌙 *Ronda noturna — {t}*",
    "rain":   "🌧️ *Chuva prevista para a próxima hora — {t}*",
    "away":   "🚶 *Saída de casa — {t}*",
    "manual": "🔍 *Ronda manual — {t}*",
}


def format_message(findings_by_cam: dict[str, list[tuple[str, str]]], profile: str,
                   cam_to_location: dict[str, str]) -> str:
    now_t = datetime.datetime.now().strftime("%H:%M")
    header = PROFILE_HEADERS.get(profile, PROFILE_HEADERS["manual"]).format(t=now_t)
    lines = [header, "Encontrei coisas fora do lugar:", ""]
    for cam, findings in findings_by_cam.items():
        lines.append(f"📍 *{humanize_cam(cam, cam_to_location)}*")
        for _cid, desc in findings:
            d = desc.rstrip(".")
            lines.append(f"• {d[:1].upper()}{d[1:]}")
        lines.append("")
    if profile == "rain":
        lines.append("_Chuva se aproximando — vale recolher._")
    elif profile == "away":
        lines.append("_Todos saíram de casa — vale conferir._")
    return "\n".join(lines).strip()


def send_findings(findings_by_cam: dict[str, list[tuple[str, str]]],
                  snapshots: dict[str, bytes],
                  boxes_by_cam: dict[str, dict[str, list[float]]],
                  profile: str, cam_to_location: dict[str, str], secrets: dict) -> bool:
    jid = secrets["whatsapp_group_jid"]
    client = WahaClient(secrets, attempts=SEND_ATTEMPTS)
    text = format_message(findings_by_cam, profile, cam_to_location)
    ok, detail = client.send_text(jid, text)
    if not ok:
        log.error("WhatsApp send failed: %s", detail)
        return False
    for cam in list(findings_by_cam)[:MAX_ALERT_IMAGES]:
        img = snapshots.get(cam)
        if not img:
            continue
        # Draw the located box for each finding still being alerted on this camera.
        cam_boxes = [b for b in ((boxes_by_cam.get(cam) or {}).get(cid)
                                 for cid, _ in findings_by_cam[cam]) if b]
        if cam_boxes:
            img = draw_boxes(img, cam_boxes)
        b64 = base64.b64encode(img).decode()
        client.send_image_b64(jid, b64, f"ronda_{cam}.jpg",
                              caption=humanize_cam(cam, cam_to_location))
    return True


# ── Debug channel (from frigate_digest.py:1140, no email fallback) ────────────
def send_debug_whatsapp(text: str, secrets: dict) -> None:
    jid = secrets.get("whatsapp_smoketest_jid", "")
    if not (jid and "@g.us" in jid):
        log.warning("[DEBUG] whatsapp_smoketest_jid missing/invalid — debug message dropped")
        return
    client = WahaClient(secrets, attempts=SEND_ATTEMPTS)
    ok, detail = client.send_text(jid, text, timeout=15)
    if not ok:
        log.warning("[DEBUG] debug send failed: %s", detail)


def send_debug_images(images: list[tuple[bytes, str]], secrets: dict) -> None:
    """Best-effort (caption per image); debug must never break the run."""
    jid = secrets.get("whatsapp_smoketest_jid", "")
    if not (jid and "@g.us" in jid) or not images:
        return
    client = WahaClient(secrets, attempts=2)
    for i, (img, caption) in enumerate(images, 1):
        try:
            b64 = base64.b64encode(img).decode()
            client.send_image_b64(jid, b64, f"ronda_debug_{i}.jpg", caption=caption)
        except Exception as exc:  # noqa: BLE001
            log.warning("[DEBUG] image %d send failed: %s", i, exc)


# ── Capture mode ──────────────────────────────────────────────────────────────
def capture_baselines(cameras: list[str], base: str, is_night: bool,
                      secrets: dict, debug: bool) -> None:
    """Snapshot the CURRENT scene as the new ground truth. Run this with the house in
    'all clear' state (varal empty, gates closed, nothing left outside)."""
    os.makedirs(BASELINES_DIR, exist_ok=True)
    suffix = "_night.jpg" if is_night else "_day.jpg"
    captured: list[tuple[bytes, str]] = []
    failed: list[str] = []
    for cam in cameras:
        img = fetch_latest(base, cam)
        if not img:
            failed.append(cam)
            continue
        path = os.path.join(BASELINES_DIR, f"{cam}{suffix}")
        if os.path.exists(path):
            try:
                os.replace(path, path + ".bak")  # keep one generation of the old reference
            except OSError as exc:
                log.warning("Cannot back up %s: %s", path, exc)
        with open(path, "wb") as f:
            f.write(img)
        log.info("Baseline captured: %s (%d bytes)", path, len(img))
        captured.append((img, f"📸 Referência {'noturna' if is_night else 'diurna'}: {cam}"))
    log.info("Capture done: %d ok, %d failed (%s)", len(captured), len(failed), ", ".join(failed))
    if debug:
        send_debug_whatsapp(
            f"📸 *Ronda — referência capturada* ({'noite' if is_night else 'dia'})\n"
            f"{len(captured)} câmeras ok"
            + (f", falharam: {', '.join(failed)}" if failed else "")
            + "\nConfira se está tudo no lugar nas imagens a seguir.",
            secrets,
        )
        send_debug_images(captured, secrets)


# ── Selftest (safe diagnostic — SmokeTests channel only) ──────────────────────
def selftest(cam: str, secrets: dict) -> None:
    """Run the box+gate+draw pipeline for ONE camera against the live scene and report
    to the SmokeTests channel ONLY (never the family group). Validates ffmpeg crop/draw
    and the gate end-to-end in the real runtime. Invoke: ...py selftest <cam>."""
    now = datetime.datetime.now()
    is_night = not (DAY_START <= now.hour < DAY_END)
    cam_checks, _ = resolve_profile(load_profiles(), "manual")
    checks = cam_checks.get(cam)
    if not checks:
        send_debug_whatsapp(f"🧪 selftest: '{cam}' sem checagens configuradas.", secrets)
        return
    prompt_template = load_prompt_template()
    property_context, cam_to_location, _ = load_property_context(PROPERTY_CTX)
    location = humanize_cam(cam, cam_to_location)
    base = f"http://{secrets['frigate_host']}:{secrets['frigate_port']}"
    current = fetch_latest(base, cam)
    ref = load_reference(cam, is_night)
    if not current or not ref:
        send_debug_whatsapp(f"🧪 selftest {cam}: sem snapshot ou referência.", secrets)
        return
    ref_img, _ref_path = ref

    # Region anchors — same wiring as _run_inner, so the selftest validates them too.
    region_by_check: dict[str, list[float]] = {}
    cues_by_check: dict[str, str] = {}
    markers: list[tuple[list[float], str]] = []
    for c in checks:
        rbox = (c.get("regions") or {}).get(cam)
        if isinstance(rbox, list) and len(rbox) == 4:
            color, color_pt = REGION_COLORS[len(markers) % len(REGION_COLORS)]
            region_by_check[c["id"]] = [float(v) for v in rbox]
            cues_by_check[c["id"]] = str((c.get("state_cues") or {}).get(cam, ""))
            markers.append(([float(v) for v in rbox], color))
            c["_color_pt"] = color_pt
    ref_for_verdict = draw_region_markers(ref_img, markers) if markers else ref_img

    findings, backend = analyze_camera(cam, ref_for_verdict, current, checks, prompt_template,
                                       is_night, property_context, location, secrets)
    header = ("verdict (%s): " % backend) + (
        ", ".join(f"[{c}] {d}" for c, d in findings) if findings else "NAO")
    lines = [f"🧪 *Selftest bbox — {location}*", header]
    if markers:
        lines.append(f"🎯 regiões configuradas: "
                     + ", ".join(f"[{cid}]" for cid in region_by_check))
    boxes: list[list[float]] = []
    crops: list[tuple[bytes, str]] = []
    if markers:
        crops.append((ref_for_verdict, "ref anotada (marcadores de região)"))
    for cid, desc in (findings or []):
        region = region_by_check.get(cid)
        if region:
            box = region
            rc, cc = _crop(ref_img, box, REGION_PAD_FRAC), _crop(current, box, REGION_PAD_FRAC)
            exemplars = load_state_exemplars(cam, cid, is_night)
            keep, note = gate_finding(ref_img, current, box, desc, secrets,
                                      pad=REGION_PAD_FRAC, state_check=True,
                                      exemplars=exemplars, cues=cues_by_check.get(cid, ""))
            note = f"região configurada{' + exemplar' if exemplars else ''}: {note}"
        else:
            box = request_bbox(current, desc, secrets)
            if box is None:
                lines.append(f"• [{cid}] sem bbox → manteria (sem verificação)")
                continue
            rc, cc = _crop(ref_img, box), _crop(current, box)
            keep, note = gate_finding(ref_img, current, box, desc, secrets)
        lines.append(f"• [{cid}] box={[round(v) for v in box]} → {note}")
        if keep:
            boxes.append(box)
        if rc:
            crops.append((rc, f"ref crop [{cid}]"))
        if cc:
            crops.append((cc, f"atual crop [{cid}]"))
    if not findings:
        lines.append("_Nada a verificar — cena limpa._")
    # ffmpeg health probe: crop + draw a demo box so the real runtime is validated
    # end-to-end even on a clean scene (draw_boxes returns the SAME object on failure).
    demo_box = [350.0, 300.0, 650.0, 550.0]
    crop_ok = _crop(current, demo_box) is not None
    demo_img = draw_boxes(current, [demo_box])
    draw_ok = demo_img is not current
    lines.append(f"🔧 ffmpeg: crop={'ok' if crop_ok else 'FALHOU'}, "
                 f"draw={'ok' if draw_ok else 'FALHOU'}")
    if boxes:
        imgs = [(draw_boxes(current, boxes), "cena atual + caixa(s) sobreviventes")] + crops
    else:
        imgs = [(demo_img, "🔧 caixa DEMO (sem achado) — valida render ffmpeg")]
    send_debug_whatsapp("\n".join(lines), secrets)
    send_debug_images(imgs, secrets)


# ── Check run ─────────────────────────────────────────────────────────────────
def _run_inner(profile: str, secrets: dict, debug: bool) -> None:
    now = datetime.datetime.now()
    is_night = not (DAY_START <= now.hour < DAY_END)
    cfg = load_profiles()
    cam_checks, prof_opts = resolve_profile(cfg, profile)
    suppress_hours   = float(prof_opts.get("suppress_hours", 0))
    confirm_positive = bool(prof_opts.get("confirm_positives", False))
    prompt_template  = load_prompt_template()
    property_context, cam_to_location, _zones = load_property_context(PROPERTY_CTX)
    base = f"http://{secrets['frigate_host']}:{secrets['frigate_port']}"

    findings_by_cam: dict[str, list[tuple[str, str]]] = {}
    snapshots: dict[str, bytes] = {}
    boxes_by_cam: dict[str, dict[str, list[float]]] = {}   # cam → {check_id: box 0-1000}
    checked_keys: set[str] = set()          # (cam:check) pairs actually analyzed this run
    debug_lines: list[str] = []
    debug_pairs: list[tuple[bytes, str]] = []

    for cam, checks in cam_checks.items():
        location = humanize_cam(cam, cam_to_location)
        current = fetch_latest(base, cam)
        if not current:
            debug_lines.append(f"{cam} → ⚠️ snapshot falhou")
            continue
        ref = load_reference(cam, is_night)
        if not ref:
            # No anchor → skip: running the VLM without a reference is the main
            # false-positive source. Capture baselines to enable this camera.
            log.info("No reference for %s — skipping (capture baselines first)", cam)
            debug_lines.append(f"{cam} → ⚠️ sem referência (capture primeiro)")
            continue
        ref_img, ref_path = ref

        # Region anchors for this camera: stamp each anchored check with its marker
        # color (used by build_prompt) and draw the markers on a COPY of the reference
        # for the verdict call. The CLEAN ref_img keeps serving the crop-compare gate.
        region_by_check: dict[str, list[float]] = {}
        cues_by_check: dict[str, str] = {}
        markers: list[tuple[list[float], str]] = []
        for c in checks:
            c.pop("_color_pt", None)
            box = (c.get("regions") or {}).get(cam)
            if isinstance(box, list) and len(box) == 4:
                color, color_pt = REGION_COLORS[len(markers) % len(REGION_COLORS)]
                region_by_check[c["id"]] = [float(v) for v in box]
                cues_by_check[c["id"]] = str((c.get("state_cues") or {}).get(cam, ""))
                markers.append(([float(v) for v in box], color))
                c["_color_pt"] = color_pt
        ref_for_verdict = draw_region_markers(ref_img, markers) if markers else ref_img

        t0 = time.monotonic()
        findings, backend = analyze_camera(cam, ref_for_verdict, current, checks,
                                           prompt_template, is_night, property_context,
                                           location, secrets)
        elapsed = time.monotonic() - t0
        if findings is None:
            debug_lines.append(f"{cam} → ⚠️ análise falhou ({backend}, {elapsed:.0f}s)")
            continue

        for c in checks:
            checked_keys.add(f"{cam}:{c['id']}")

        if findings and debug:
            # Show the debug channel exactly what the model saw on a positive.
            debug_pairs.append((ref_for_verdict, f"ref: {cam} ({os.path.basename(ref_path)})"))
            debug_pairs.append((current, f"atual (1ª): {cam}"))

        if findings and confirm_positive:
            # 2-of-2 confirmation on a FRESH snapshot — kills one-frame glitches
            # (headlight glare, insect on lens). Costs an extra call only on positives.
            time.sleep(CONFIRM_DELAY_S)
            second = fetch_latest(base, cam)
            if second:
                findings2, backend2 = analyze_camera(cam, ref_for_verdict, second, checks,
                                                     prompt_template, is_night,
                                                     property_context, location, secrets)
                confirmed_ids = {cid for cid, _ in (findings2 or [])}
                dropped = [cid for cid, _ in findings if cid not in confirmed_ids]
                if dropped:
                    log.info("%s: findings not confirmed on 2nd pass: %s", cam, dropped)
                findings = [(cid, desc) for cid, desc in findings if cid in confirmed_ids]
                current = second  # alert with the freshest image
                backend = f"{backend}+{backend2}"

        # Bounding-box verification gate: locate each finding, then run a focused crop
        # compare to reject weather/lighting false positives the whole-scene verdict let
        # through (e.g. permanent planters that "appear" once wet pavement dries out).
        # The surviving boxes are drawn on the alert image. Fails OPEN on infra hiccups.
        if findings and ENABLE_BBOX_GATE:
            kept: list[tuple[str, str]] = []
            for cid, desc in findings:
                region = region_by_check.get(cid)
                if region:
                    # Configured anchor is authoritative — no model grounding, so the
                    # gate is guaranteed to examine the right object (not, say, a
                    # window across the yard). Tighter pad: the region is generous.
                    keep, note = gate_finding(ref_img, current, region, desc, secrets,
                                              pad=REGION_PAD_FRAC, state_check=True,
                                              exemplars=load_state_exemplars(cam, cid, is_night),
                                              cues=cues_by_check.get(cid, ""))
                    note = f"região configurada: {note}"
                    box = region
                else:
                    box = request_bbox(current, desc, secrets)
                    if box is None:
                        kept.append((cid, desc))      # no localization → keep, don't draw
                        debug_lines.append(f"    ↳ [{cid}] sem bbox → mantido")
                        continue
                    keep, note = gate_finding(ref_img, current, box, desc, secrets)
                if keep:
                    kept.append((cid, desc))
                    boxes_by_cam.setdefault(cam, {})[cid] = box
                    debug_lines.append(f"    ↳ [{cid}] {note}")
                else:
                    log.info("%s: [%s] SUPPRESSED by bbox gate — %s", cam, cid, note)
                    debug_lines.append(f"    ↳ [{cid}] 🚫 {note}")
            findings = kept

        verdict = ", ".join(cid for cid, _ in findings) if findings else "NAO"
        debug_lines.append(f"{cam} → {verdict} ({backend}, {elapsed:.0f}s)")
        if findings:
            findings_by_cam[cam] = findings
            snapshots[cam] = current

    # Suppression: drop findings already notified for this profile within the window
    state = load_state()
    found_keys = {f"{cam}:{cid}" for cam, fs in findings_by_cam.items() for cid, _ in fs}
    to_send: dict[str, list[tuple[str, str]]] = {}
    for cam, findings in findings_by_cam.items():
        kept = [(cid, desc) for cid, desc in findings
                if not is_suppressed(state, f"{cam}:{cid}", profile, suppress_hours)]
        kept_ids = {cid for cid, _ in kept}
        for cid, _desc in findings:
            if cid not in kept_ids:
                log.info("Suppressed %s:%s (%s, %.0fh window)", cam, cid, profile, suppress_hours)
        if kept:
            to_send[cam] = kept

    # Resolution reset: keys checked but NOT found are cleared, so clothes taken in
    # and hung out again re-alert correctly.
    for key in checked_keys - found_keys:
        state["findings"].pop(key, None)

    sent = False
    if to_send:
        sent = send_findings(to_send, snapshots, boxes_by_cam, profile, cam_to_location, secrets)
        if sent:
            for cam, findings in to_send.items():
                for cid, desc in findings:
                    entry = state["findings"].setdefault(f"{cam}:{cid}",
                                                         {"last_notified": {}, "text": ""})
                    # Stamp only after a successful send — a WAHA outage re-alerts next run.
                    entry["last_notified"][profile] = time.time()
                    entry["text"] = desc
        else:
            log.error("Findings NOT delivered — suppression state left unstamped")
    save_state(state)

    n_checked = len({k.split(":")[0] for k in checked_keys})
    log.info("Run done: %d cameras checked, %d with findings, %d sent (suppressed the rest)",
             n_checked, len(findings_by_cam), len(to_send) if sent else 0)
    if debug:
        summary = "\n".join(debug_lines) or "nenhuma câmera configurada"
        if to_send and sent:
            head = f"📤 *Ronda {profile} — alerta enviado* ({len(to_send)} câmeras)"
        elif findings_by_cam:
            head = f"🔇 *Ronda {profile} — achados suprimidos* (já notificados)"
        else:
            head = f"🟢 *Ronda {profile} ok* — nada fora do lugar ({n_checked} câmeras)"
        send_debug_whatsapp(f"{head}\n\n{summary}", secrets)
        send_debug_images(debug_pairs, secrets)


def run(profile: str) -> None:
    """Wrapper: reads the debug flag up front and reports hard failures to the
    SmokeTests channel — infra failures must never alert the family group."""
    log.info("=== Scene check starting (profile=%s) ===", profile)
    secrets = load_secrets()
    ha_token = secrets.get("boiler_ha_token", "")
    debug = get_ha_state("input_boolean.frigate_scene_check_debug", ha_token) == "on"
    try:
        if profile == "capture":
            cams = sys.argv[2:] or all_configured_cameras(load_profiles())
            now = datetime.datetime.now()
            base = f"http://{secrets['frigate_host']}:{secrets['frigate_port']}"
            capture_baselines(cams, base, not (DAY_START <= now.hour < DAY_END),
                              secrets, debug)
        else:
            _run_inner(profile, secrets, debug)
    except Exception as exc:
        log.exception("Scene check FAILED")
        if debug:
            tb = traceback.format_exc().strip().splitlines()
            where = tb[-2].strip() if len(tb) >= 2 else ""
            send_debug_whatsapp(
                f"❌ *Ronda FALHOU* ({profile})\n"
                f"{type(exc).__name__}: {str(exc)[:200]}\n"
                f"`{where[:160]}`",
                secrets,
            )
        raise


# ── Launcher / lock (from frigate_digest.py:1342) ─────────────────────────────
def _lock_is_held() -> bool:
    """Return True only if a live process holds the lock; removes stale locks."""
    if not os.path.exists(LOCK_FILE):
        return False
    try:
        with open(LOCK_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # signal 0 = existence check, no signal sent
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        log.info("Removing stale lock (process gone or unreadable)")
        try:
            os.unlink(LOCK_FILE)
        except OSError:
            pass
        return False


def main() -> None:
    arg = (sys.argv[1].strip() if len(sys.argv) > 1 else "manual").lower()

    if arg == "selftest":
        # Safe diagnostic: reports to SmokeTests only. Runs inline (fast, a few calls).
        secrets = load_secrets()
        cam = sys.argv[2].strip() if len(sys.argv) > 2 else "frente_garagem"
        log.info("=== Selftest starting (cam=%s) ===", cam)
        selftest(cam, secrets)
        return

    profile = arg if arg in ("night", "rain", "away", "manual", "capture") else "manual"

    if profile == "capture":
        # Capture is fast (a handful of snapshot GETs) — run inline so the button
        # press completes synchronously, well inside HA's 60 s shell_command budget.
        run("capture")
        return

    if not os.environ.get("_SCENE_CHECK_WORKER"):
        # Launcher: spawn an independent worker and exit immediately. HA's shell_command
        # sees the launcher exit in < 0.1 s, so its 60-second timeout never fires.
        env = os.environ.copy()
        env["_SCENE_CHECK_WORKER"] = "1"
        subprocess.Popen(
            [sys.executable] + sys.argv,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        sys.exit(0)

    if _lock_is_held():
        log.info("Scene check already running — skipping trigger")
        sys.exit(0)
    lock_written = False
    try:
        with open(LOCK_FILE, "w") as _lf:
            _lf.write(str(os.getpid()))
        lock_written = True
    except OSError as exc:
        log.warning("Cannot create lock file (%s) — proceeding without lock", exc)
    try:
        run(profile)
    finally:
        if lock_written:
            try:
                os.unlink(LOCK_FILE)
            except OSError:
                pass


if __name__ == "__main__":
    main()
