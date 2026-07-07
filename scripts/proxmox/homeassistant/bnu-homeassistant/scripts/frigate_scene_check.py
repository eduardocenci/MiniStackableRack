#!/usr/bin/env python3
"""
Ronda da Casa — proactive scene check against ground-truth references
Deploy to: /config/scripts/frigate_scene_check.py

Usage: python3 frigate_scene_check.py [night|rain|manual]   # run a check profile
       python3 frigate_scene_check.py capture [cam ...]     # snapshot current scene as ground truth

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
OPENAI_MODEL     = "gpt-4.1-mini"  # vision-capable, non-reasoning fallback (see frigate_digest.py)
LOG_FILE         = "/config/frigate_scene_check.log"
LOCK_FILE        = "/config/frigate_scene_check.lock"    # separate from frigate_digest.lock —
                                                         # both pipelines may legitimately run at once
STATE_FILE       = "/config/frigate_scene_check.state.json"  # per-finding suppression memory
PROFILES_FILE    = "/config/frigate_scene_check_profiles.yaml"
PROMPT_FILE      = "/config/frigate_scene_check_prompt.txt"  # user-editable, falls back to DEFAULT_PROMPT
PROPERTY_CTX     = "/config/frigate_property_context.txt"
BASELINES_DIR    = "/config/frigate_scene_baselines"     # scene-check ground truth ("all clear")
DIGEST_BASELINES_DIR = "/config/frigate_baselines"       # digest baselines double as fallback anchors
SNAPSHOT_H       = 720   # latest.jpg height; bump per camera only if a check underperforms
DAY_START        = 6     # 06:00 — matches frigate_digest.py's day/night split
DAY_END          = 20    # 20:00
CONFIRM_DELAY_S  = 20    # wait before the 2-of-2 confirmation snapshot (kills one-frame glitches)
MAX_ALERT_IMAGES = 4     # cap on snapshot images attached to a family-group alert
LLM_TIMEOUT      = 600   # seconds — hard per-attempt wall clock (SIGALRM)
# qwen3-vl:8b thinking-runaway workaround — same ladder as frigate_digest.py: thinking tokens
# count against num_predict, so an empty done_reason="length" response retries with ×1.5 more.
OLLAMA_NUM_CTX      = 16384
OLLAMA_NUM_PREDICT  = 8192
OLLAMA_MAX_ATTEMPTS = 3
SEND_ATTEMPTS    = 3     # WhatsApp send retries

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
- Ignore diferenças de iluminação, sombras, reflexos, chuva, vento na vegetação, qualidade
  de imagem, carimbo de hora/data e pequenas mudanças de ângulo ou cor.
- Ignore pessoas e animais: a ronda procura OBJETOS e ESTADOS (portas, portões, roupas,
  objetos deixados), não movimento.
- Reporte um item SOMENTE se tiver certeza visual clara na imagem ATUAL. Na dúvida, NÃO reporte.
- Responda em português do Brasil.
__REFERENCES__
FORMATO DA RESPOSTA (obrigatório):
- Primeira linha: exatamente "ALERTA: SIM" ou "ALERTA: NAO".
- Se SIM: uma linha por achado, no formato "- [id] descrição curta e factual", usando
  apenas os ids da lista acima. Se NAO: nenhuma linha adicional.
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
    checks_txt = "\n".join(
        f"- [{c['id']}] {' '.join(str(c.get('instruction', c.get('label', c['id']))).split())}"
        for c in checks
    )
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


def parse_verdict(text: str, allowed_ids: set[str]) -> list[tuple[str, str]] | None:
    """Returns [(check_id, description)] — empty list means a clean 'ALERTA: NAO'.
    Returns None when the verdict is unparseable (no gate line, or SIM without any
    finding that carries a known [id]) — the caller treats None as NO finding plus a
    debug notice: for an 'is anything off' check, a false silence is cheaper than
    crying wolf to the family group."""
    m = _ALERT_GATE_RE.search(text or "")
    if not m:
        return None
    if m.group(1).upper().startswith("N"):
        return []
    findings = [(cid.lower(), desc.strip()) for cid, desc in _FINDING_RE.findall(text)
                if cid.lower() in allowed_ids]  # drop hallucinated categories
    return findings or None


# ── Ollama (from frigate_digest.py:905 — retry ladder intact) ─────────────────
class _OllamaTimeout(Exception):
    pass


def call_ollama(prompt: str, images: list[bytes]) -> tuple[str | None, dict]:
    """Returns (verdict_text_or_None, stats). Retries with a ×1.5 larger num_predict each
    time the model returns an EMPTY response cut off by length (done_reason="length")."""
    img_payload = [base64.b64encode(img).decode() for img in images] if images else None
    stats: dict = {"n_images": len(images), "elapsed_s": 0.0, "timed_out": False,
                   "error": None, "attempts": 0, "done_reason": None, "backend": "ollama"}

    def _alarm_handler(signum, frame):
        raise _OllamaTimeout()

    num_predict = OLLAMA_NUM_PREDICT
    for attempt in range(1, OLLAMA_MAX_ATTEMPTS + 1):
        stats["attempts"] = attempt
        payload: dict = {
            "model":   OLLAMA_MODEL,
            "prompt":  prompt,
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
def call_openai(prompt: str, images: list[bytes], api_key: str) -> str | None:
    content: list[dict] = [{"type": "text", "text": prompt}]
    for img_bytes in images:
        b64 = base64.b64encode(img_bytes).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"},
        })
    payload = {
        "model":       OPENAI_MODEL,
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


def analyze_camera(camera: str, reference: bytes, current: bytes, checks: list[dict],
                   prompt_template: str, is_night: bool, property_context: str,
                   location: str, secrets: dict) -> tuple[list[tuple[str, str]] | None, str]:
    """One VLM pass: reference + current → parsed findings.
    Returns (findings_or_None, backend_label). None = analysis failed/unparseable."""
    prompt = build_prompt(prompt_template, location, checks, is_night, property_context)
    allowed = {c["id"] for c in checks}
    images = [reference, current]  # order matters: prompt says FIRST=reference, SECOND=current

    text, stats = call_ollama(prompt, images)
    backend = "ollama"
    if text is None:
        api_key = secrets.get("openai_api_key", "")
        if api_key:
            text = call_openai(prompt, images, api_key)
            backend = "openai"
        else:
            log.error("Ollama failed and openai_api_key not in secrets.yaml — no fallback")
    if text is None:
        return None, backend
    findings = parse_verdict(text, allowed)
    if findings is None:
        log.warning("Unparseable verdict for %s (%s): %r", camera, backend, (text or "")[:200])
    return findings, backend


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
    return "\n".join(lines).strip()


def send_findings(findings_by_cam: dict[str, list[tuple[str, str]]],
                  snapshots: dict[str, bytes], profile: str,
                  cam_to_location: dict[str, str], secrets: dict) -> bool:
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

        t0 = time.monotonic()
        findings, backend = analyze_camera(cam, ref_img, current, checks, prompt_template,
                                           is_night, property_context, location, secrets)
        elapsed = time.monotonic() - t0
        if findings is None:
            debug_lines.append(f"{cam} → ⚠️ análise falhou ({backend}, {elapsed:.0f}s)")
            continue

        for c in checks:
            checked_keys.add(f"{cam}:{c['id']}")

        if findings and debug:
            # Show the debug channel exactly what the model saw on a positive.
            debug_pairs.append((ref_img, f"ref: {cam} ({os.path.basename(ref_path)})"))
            debug_pairs.append((current, f"atual (1ª): {cam}"))

        if findings and confirm_positive:
            # 2-of-2 confirmation on a FRESH snapshot — kills one-frame glitches
            # (headlight glare, insect on lens). Costs an extra call only on positives.
            time.sleep(CONFIRM_DELAY_S)
            second = fetch_latest(base, cam)
            if second:
                findings2, backend2 = analyze_camera(cam, ref_img, second, checks,
                                                     prompt_template, is_night,
                                                     property_context, location, secrets)
                confirmed_ids = {cid for cid, _ in (findings2 or [])}
                dropped = [cid for cid, _ in findings if cid not in confirmed_ids]
                if dropped:
                    log.info("%s: findings not confirmed on 2nd pass: %s", cam, dropped)
                findings = [(cid, desc) for cid, desc in findings if cid in confirmed_ids]
                current = second  # alert with the freshest image
                backend = f"{backend}+{backend2}"

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
        sent = send_findings(to_send, snapshots, profile, cam_to_location, secrets)
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
    profile = (sys.argv[1].strip() if len(sys.argv) > 1 else "manual").lower()
    if profile not in ("night", "rain", "manual", "capture"):
        profile = "manual"

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
