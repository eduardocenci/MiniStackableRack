#!/usr/bin/env python3
"""
Frigate Cross-Camera Digest
Deploy to: /config/scripts/frigate_digest.py

Usage: python3 frigate_digest.py [auto|manual]
  auto   — uses last_run timestamp from HA; updates it after sending
  manual — 30-min lookback; does NOT update last_run (on-demand)

Reads all config from /config/secrets.yaml.
Logs to /config/frigate_digest.log.

SECURITY CONSTRAINT (same as frigate_whatsapp.py):
  Sends to exactly ONE destination: secrets["whatsapp_group_jid"].
  Zero read calls to the WhatsApp API.
"""

import base64
import datetime
import glob as glob_module
import json
import logging
import os
import signal
import smtplib
import subprocess
import sys
import time
import traceback
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
import yaml

# ── Constants ─────────────────────────────────────────────────────────────────
HA_URL           = "http://localhost:8123"
OLLAMA_HOST      = "10.1.1.50"  # bnu-proxmox LAN IP — socat proxy forwards to ply-desktop:11434
OLLAMA_PORT      = 11434
OLLAMA_MODEL     = "qwen3-vl:8b"
OPENAI_MODEL     = "gpt-4o"
OPENAI_IN_PRICE  = 2.50 / 1_000_000    # USD per input token (gpt-4o)
OPENAI_OUT_PRICE = 10.0 / 1_000_000    # USD per output token (gpt-4o)
LOG_FILE         = "/config/frigate_digest.log"
LOCK_FILE        = "/config/frigate_digest.lock"
PROPERTY_CTX     = "/config/frigate_property_context.txt"
BASELINES_DIR    = "/config/frigate_baselines"
CLIP_DIR         = "/config/www"
FFMPEG           = "/usr/bin/ffmpeg"
RELEVANT_OBJECTS = {"person", "car", "dog", "cat", "animal", "bicycle", "motorcycle"}
LLM_TIMEOUT      = 600   # seconds — generation can be slow on first call
DAY_START        = 6     # 06:00
DAY_END          = 20    # 20:00
# Burst grouping: events whose gap (prev end_time → next start_time) exceeds this
# are treated as separate bursts. Matches the 60 s cooldown timer in the HA package.
COOLDOWN_GAP_S   = 60
# Video output — single-pass CFR re-encode eliminates frame jumps from VFR source clips.
VIDEO_FPS        = 15
VIDEO_W          = 854
VIDEO_H          = 480
SEND_ATTEMPTS    = 3     # WhatsApp send retries (rides over transient gateway hiccups)

_handler = logging.FileHandler(LOG_FILE)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [frigate_digest] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger(__name__)


# ── Secrets ───────────────────────────────────────────────────────────────────
def load_secrets() -> dict:
    with open("/config/secrets.yaml") as f:
        return yaml.safe_load(f)


# ── HA API helpers ────────────────────────────────────────────────────────────
def _ha_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def get_ha_state(entity_id: str, token: str) -> str:
    try:
        r = requests.get(f"{HA_URL}/api/states/{entity_id}",
                         headers=_ha_headers(token), timeout=10)
        if r.ok:
            return r.json().get("state", "")
    except requests.RequestException as exc:
        log.warning("get_ha_state(%s) failed: %s", entity_id, exc)
    return ""


def get_ha_number(entity_id: str, token: str) -> float:
    state = get_ha_state(entity_id, token)
    try:
        return float(state)
    except (ValueError, TypeError):
        return 0.0


def update_ha_state(entity_id: str, state: str, attributes: dict, token: str) -> None:
    try:
        requests.post(
            f"{HA_URL}/api/states/{entity_id}",
            json={"state": state, "attributes": attributes},
            headers=_ha_headers(token), timeout=10,
        )
    except requests.RequestException as exc:
        log.warning("update_ha_state(%s) failed: %s", entity_id, exc)


def update_ha_number(entity_id: str, value: float, token: str) -> None:
    update_ha_state(entity_id, str(round(value, 4)), {}, token)


def update_ha_datetime(entity_id: str, dt: datetime.datetime, token: str) -> None:
    update_ha_state(entity_id, dt.strftime("%Y-%m-%d %H:%M:%S"),
                    {"has_date": True, "has_time": True}, token)


# ── Frigate API helpers ───────────────────────────────────────────────────────
def frigate_base(host: str, port: str) -> str:
    return f"http://{host}:{port}"


def fetch_reviews(base: str, since: datetime.datetime) -> list[dict]:
    r = requests.get(
        f"{base}/api/review",
        params={"after": since.timestamp(), "limit": 50},
        timeout=30,
    )
    r.raise_for_status()
    return r.json() or []


def download_clip(base: str, event_id: str, dest_dir: str) -> str | None:
    path = os.path.join(dest_dir, f"frigate_clip_{event_id}.mp4")
    if os.path.exists(path):
        return path
    url = f"{base}/api/events/{event_id}/clip.mp4"
    try:
        r = requests.get(url, timeout=120, stream=True)
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
        log.info("Clip saved: %s (%d bytes)", path, os.path.getsize(path))
        return path
    except requests.RequestException as exc:
        log.warning("Failed to download clip for %s: %s", event_id, exc)
        return None


def fetch_snapshot(base: str, event_id: str) -> bytes | None:
    try:
        r = requests.get(f"{base}/api/events/{event_id}/snapshot.jpg", timeout=30)
        r.raise_for_status()
        return r.content
    except requests.RequestException as exc:
        log.warning("Failed to fetch snapshot for %s: %s", event_id, exc)
        return None


# ── Encoding / GenAI helpers ──────────────────────────────────────────────────
def _fix_encoding(text: str) -> str:
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def extract_genai_text(review: dict) -> str:
    data = review.get("data") or {}
    if data.get("description"):
        return _fix_encoding(data["description"])
    meta = data.get("metadata") or {}
    title   = _fix_encoding(meta.get("title") or "")
    summary = _fix_encoding(meta.get("shortSummary") or "")
    return title or summary or ""


def is_relevant(review: dict) -> bool:
    objects = set((review.get("data") or {}).get("objects") or [])
    return bool(objects & RELEVANT_OBJECTS)


def cluster_by_gap(reviews: list[dict], max_gap_s: float) -> list[list[dict]]:
    """Split reviews into bursts ordered by start_time.

    A new burst begins whenever the gap between the previous review's end_time and
    the next review's start_time exceeds max_gap_s. Reviews still in progress
    (end_time is None) fall back to their start_time. Returns clusters oldest→newest,
    each cluster already sorted by start_time.
    """
    if not reviews:
        return []
    ordered = sorted(reviews, key=lambda r: r.get("start_time") or 0)
    clusters: list[list[dict]] = [[ordered[0]]]
    for r in ordered[1:]:
        prev = clusters[-1][-1]
        prev_end = prev.get("end_time") or prev.get("start_time") or 0
        if (r.get("start_time") or 0) - prev_end > max_gap_s:
            clusters.append([r])
        else:
            clusters[-1].append(r)
    return clusters


# ── Property context ──────────────────────────────────────────────────────────
def load_property_context(path: str) -> tuple[str, dict[str, str]]:
    """Returns (raw_text, camera_to_location_dict).

    The [Câmeras] section is parsed for camera-name→location mappings.
    Lines starting with # are comments. Everything else becomes context for the LLM.
    """
    camera_to_location: dict[str, str] = {}
    if not os.path.exists(path):
        log.info("Property context file not found: %s — LLM will use camera names", path)
        return "", camera_to_location

    with open(path, encoding="utf-8") as f:
        raw = f.read()

    in_cameras = False
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        low = stripped.lower()
        if low.startswith("[câmera") or low.startswith("[camera"):
            in_cameras = True
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_cameras = False
            continue
        if in_cameras and ":" in stripped:
            cam, _, loc = stripped.partition(":")
            cam = cam.strip()
            loc = loc.strip()
            if cam and loc:
                camera_to_location[cam] = loc

    # Strip comment lines for the LLM context
    clean_lines = [l for l in raw.splitlines() if not l.strip().startswith("#")]
    context = "\n".join(clean_lines).strip()
    return context, camera_to_location


# ── Baseline images ───────────────────────────────────────────────────────────
def load_baselines(cameras: list[str], baselines_dir: str, is_night: bool) -> dict[str, bytes]:
    suffix = "_night.jpg" if is_night else "_day.jpg"
    result = {}
    for cam in cameras:
        path = os.path.join(baselines_dir, f"{cam}{suffix}")
        if os.path.exists(path):
            with open(path, "rb") as f:
                result[cam] = f.read()
            log.info("Baseline loaded: %s", path)
    return result


# ── Video compilation ─────────────────────────────────────────────────────────
def compile_digest_video(events: list[dict], clips: dict[str, str | None]) -> str | None:
    """Stitch event clips into one MP4, chronological (oldest→newest).

    Single ffmpeg pass with a concat filter: each input is normalised to a constant
    frame rate (VIDEO_FPS), padded to VIDEO_W×VIDEO_H with square pixels, then
    concatenated. A single CFR re-encode eliminates the frame jumps that occur when
    variable-frame-rate Frigate clips are stream-copied together.
    """
    # Order strictly by event start timestamp so the montage is chronological.
    valid = [
        (e, clips[e["detection_id"]])
        for e in sorted(events, key=lambda e: e.get("start_ts") or 0)
        if e.get("detection_id") and clips.get(e["detection_id"])
    ]
    if not valid:
        log.info("No valid clips available — skipping video compilation")
        return None

    inputs: list[str]  = []
    filters: list[str] = []
    for i, (_event, clip_path) in enumerate(valid):
        inputs += ["-i", clip_path]
        filters.append(
            f"[{i}:v]fps={VIDEO_FPS},"
            f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
            f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,format=yuv420p[v{i}]"
        )
    concat_in = "".join(f"[v{i}]" for i in range(len(valid)))
    filter_complex = ";".join(filters) + f";{concat_in}concat=n={len(valid)}:v=1:a=0[out]"

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    output = os.path.join(CLIP_DIR, f"frigate_digest_{timestamp}.mp4")

    # The per-input fps filter plus output -r both force constant frame rate, so the
    # montage has one continuous, jump-free timeline regardless of source VFR clips.
    cmd = [FFMPEG, "-y", *inputs,
           "-filter_complex", filter_complex, "-map", "[out]",
           "-r", str(VIDEO_FPS),
           "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", output]
    timeout = min(600, 60 + 25 * len(valid))
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
        log.info("Digest video compiled: %s (%d bytes, %d clips, CFR %dfps)",
                 output, os.path.getsize(output), len(valid), VIDEO_FPS)
        return output
    except subprocess.CalledProcessError as exc:
        log.error("Video compile failed: %s", _ffmpeg_err(exc))
        return None
    except subprocess.TimeoutExpired as exc:
        log.error("Video compile timed out after %ds: %s", timeout, exc)
        return None


def _ffmpeg_err(exc: Exception) -> str:
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, bytes):
        tail = stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        return " | ".join(tail) if tail else str(exc)
    return str(exc)


# ── LLM prompt builder ────────────────────────────────────────────────────────
def _build_prompt(events: list[dict], context: str, baselines: dict[str, bytes],
                  snapshots: dict[str, bytes | None],
                  window_min: int) -> tuple[str, list[bytes]]:
    """Returns (text_prompt, ordered_list_of_image_bytes).

    Image order: baselines first (one per camera, deduplicated), then snapshots in event order.
    """
    image_bytes: list[bytes] = []
    seen_baseline_cams: set[str] = set()

    for event in events:
        cam = event["camera"]
        if cam in baselines and cam not in seen_baseline_cams:
            image_bytes.append(baselines[cam])
            seen_baseline_cams.add(cam)

    for event in events:
        snap = snapshots.get(event.get("detection_id") or "")
        if snap:
            image_bytes.append(snap)

    context_block = f"\n[CONTEXTO DA PROPRIEDADE]\n{context}\n" if context.strip() else ""

    if seen_baseline_cams:
        n = len(seen_baseline_cams)
        image_note = (
            f"\n[IMAGENS] As primeiras {n} imagem(ns) são baselines (como a propriedade "
            "aparece normalmente). As imagens seguintes são do evento atual. "
            "Compare e descreva o que está diferente ou incomum.\n"
        )
    elif image_bytes:
        image_note = "\n[IMAGENS] As imagens abaixo são capturas do evento atual.\n"
    else:
        image_note = ""

    event_lines = "\n".join(
        f"- [{e['time']}] {e['location']}: {e['text'] or '(sem descrição)'} "
        f"(objetos: {', '.join(e['objects']) or 'desconhecido'})"
        for e in events
    )

    prompt = (
        "Você é um assistente de segurança residencial. Analise os dados e imagens abaixo.\n"
        f"{context_block}{image_note}\n"
        f"[EVENTOS DETECTADOS — últimos {window_min} min]\n"
        f"{event_lines}\n\n"
        "INSTRUÇÕES:\n"
        "- Use SOMENTE as referências de localização fornecidas (frente, fundos, portão, etc.).\n"
        "- NUNCA mencione nomes técnicos de câmeras.\n"
        "- Se imagens baseline forem fornecidas, compare com as atuais e descreva diferenças.\n"
        "- Conecte eventos entre locais quando fizer sentido (ex: mesma pessoa em locais diferentes).\n"
        "- Escreva 2–5 frases em português. Mencione preocupações de segurança se houver.\n"
        "- Responda APENAS com o resumo narrativo, sem introdução, título ou conclusão."
    )
    return prompt, image_bytes


# ── Ollama ─────────────────────────────────────────────────────────────────────
class _OllamaTimeout(Exception):
    pass


def call_ollama(events: list[dict], context: str, baselines: dict[str, bytes],
                snapshots: dict[str, bytes | None], window_min: int) -> tuple[str | None, dict]:
    """Returns (narrative_or_None, stats_dict).

    stats keys: n_images, elapsed_s, timed_out, error,
                prompt_tokens, output_tokens, response_chars, thinking_chars, done_reason.
    """
    prompt, images = _build_prompt(events, context, baselines, snapshots, window_min)
    payload: dict = {
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  False,
        # qwen3-vl:8b is a thinking model — thinking tokens count against num_predict.
        # With 13 images the thinking phase can consume ~3000+ tokens; set high enough
        # that the model finishes thinking and still has room for the actual response.
        "options": {"temperature": 0.3, "num_predict": 6144},
    }
    if images:
        payload["images"] = [base64.b64encode(img).decode() for img in images]

    stats: dict = {
        "n_images": len(images), "elapsed_s": 0.0,
        "timed_out": False, "error": None,
        "prompt_tokens": 0, "output_tokens": 0,
        "response_chars": 0, "thinking_chars": 0, "done_reason": None,
    }

    def _alarm_handler(signum, frame):
        raise _OllamaTimeout()

    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(LLM_TIMEOUT)  # hard wall-clock deadline; won't be reset by trickling tokens
    t0 = time.monotonic()
    try:
        log.info("Calling Ollama (%d images)...", len(images))
        r = requests.post(
            f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/generate",
            json=payload,
            # No requests timeout — SIGALRM is the authoritative deadline
        )
        r.raise_for_status()
        data     = r.json()
        response = data.get("response", "").strip()
        thinking = data.get("thinking", "")
        stats.update({
            "elapsed_s":      time.monotonic() - t0,
            "response_chars": len(response),
            "thinking_chars": len(thinking),
            "done_reason":    data.get("done_reason"),
            "prompt_tokens":  data.get("prompt_eval_count", 0),
            "output_tokens":  data.get("eval_count", 0),
        })
        log.info("Ollama response (%d chars, thinking=%d chars, done=%s): %s...",
                 len(response), len(thinking), data.get("done_reason"), response[:120])
        return response or None, stats
    except _OllamaTimeout:
        stats.update({"elapsed_s": time.monotonic() - t0, "timed_out": True})
        log.error("Ollama call timed out after %ds", LLM_TIMEOUT)
        return None, stats
    except requests.RequestException as exc:
        stats.update({"elapsed_s": time.monotonic() - t0, "error": str(exc)})
        log.error("Ollama call failed: %s", exc)
        return None, stats
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


# ── OpenAI ────────────────────────────────────────────────────────────────────
def call_openai(events: list[dict], context: str, baselines: dict[str, bytes],
                snapshots: dict[str, bytes | None], window_min: int,
                api_key: str) -> tuple[str | None, int, int]:
    """Returns (narrative, input_tokens, output_tokens)."""
    prompt, images = _build_prompt(events, context, baselines, snapshots, window_min)

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
        "max_tokens":  600,
        "temperature": 0.3,
    }
    try:
        log.info("Calling OpenAI (%s images)...", len(images))
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        narrative  = (data["choices"][0]["message"]["content"] or "").strip()
        usage      = data.get("usage", {})
        tokens_in  = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)
        log.info("OpenAI response (%d chars, %d in/%d out tokens): %s...",
                 len(narrative), tokens_in, tokens_out, narrative[:120])
        return narrative or None, tokens_in, tokens_out
    except requests.RequestException as exc:
        log.error("OpenAI call failed: %s", exc)
        return None, 0, 0


# ── WhatsApp sender ───────────────────────────────────────────────────────────
def _post_with_retry(url: str, payload: dict, api_key: str, timeout: int,
                     what: str) -> tuple[bool, str]:
    """POST with up to SEND_ATTEMPTS tries and linear backoff.

    Returns (ok, detail). detail carries the failure reason (HTTP status + body
    snippet, or the exception) so callers can surface *what went wrong*.
    """
    last = "no attempt"
    for i in range(1, SEND_ATTEMPTS + 1):
        try:
            r = requests.post(url, json=payload, headers={"apikey": api_key}, timeout=timeout)
            if r.status_code >= 400:
                last = f"HTTP {r.status_code}: {r.text[:160]}"
                log.warning("%s attempt %d/%d → %s", what, i, SEND_ATTEMPTS, last)
            else:
                return True, f"HTTP {r.status_code}"
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
            log.warning("%s attempt %d/%d → %s", what, i, SEND_ATTEMPTS, last)
        if i < SEND_ATTEMPTS:
            time.sleep(2 * i)
    return False, last


def send_whatsapp_digest(events: list[dict], narrative: str,
                         video_path: str | None, secrets: dict) -> tuple[bool, str]:
    """Send the digest text (+video) to the Casa Blumenau group.

    Returns (text_ok, detail) — detail describes the failure when text_ok is False.
    """
    api_url  = secrets["whatsapp_api_url"].rstrip("/")
    api_key  = secrets["whatsapp_api_key"]
    instance = secrets["whatsapp_instance"]
    jid      = secrets["whatsapp_group_jid"]

    # SECURITY: only group JIDs allowed
    if not jid or "@g.us" not in jid:
        log.error("whatsapp_group_jid missing or not a group JID — aborting")
        return False, "group_jid missing/invalid"

    locations = list(dict.fromkeys(e["location"] for e in events))
    ts_start = events[0]["time"] if events else ""
    ts_end   = events[-1]["time"] if events else ""
    ts_range = f"{ts_start}–{ts_end}" if ts_start != ts_end else ts_start
    text = (
        f"*📹 Resumo — {ts_range}*\n\n"
        f"{narrative}\n\n"
        f"_{len(events)} evento(s) · {', '.join(locations)}_"
    )

    ok, detail = _post_with_retry(
        f"{api_url}/message/sendText/{instance}",
        {"number": jid, "textMessage": {"text": text}},
        api_key, timeout=30, what="WhatsApp sendText",
    )
    if not ok:
        log.error("WhatsApp sendText failed after %d attempts: %s", SEND_ATTEMPTS, detail)
        return False, detail
    log.info("WhatsApp text sent")

    if not video_path or not os.path.exists(video_path):
        return True, detail

    filename = os.path.basename(video_path)
    # Serve via HA local web server so the gateway downloads it directly (avoids ~17 MB JSON payload)
    video_url = f"http://10.1.1.124:8123/local/{filename}"
    vok, vdetail = _post_with_retry(
        f"{api_url}/message/sendMedia/{instance}",
        {"number": jid, "mediaMessage": {
            "mediatype": "video", "mimetype": "video/mp4",
            "media": video_url, "caption": "", "fileName": filename,
        }},
        api_key, timeout=120, what="WhatsApp sendMedia",
    )
    if vok:
        log.info("WhatsApp video sent via URL")
    else:
        log.warning("WhatsApp video send failed (non-fatal): %s", vdetail)
    return True, detail


# ── Debug channel (SmokeTests group, email fallback) ─────────────────────────
def _send_debug_email(text: str, secrets: dict, reason: str = "") -> bool:
    """Transport-independent fallback for debug messages (Gmail SMTP).

    Lets the debug channel survive a WhatsApp outage — the very situation where a
    WhatsApp-only debug channel would go silent.
    """
    smtp_user  = secrets.get("boiler_smtp_user")
    smtp_pass  = secrets.get("boiler_smtp_pass")
    recipients = [e.strip() for e in secrets.get("frigate_email_recipients", "").split(",") if e.strip()]
    if not (smtp_user and smtp_pass and recipients):
        log.warning("[DEBUG] Email fallback unavailable (missing SMTP creds/recipients)")
        return False

    first = (text.strip().splitlines() or ["debug"])[0]
    body  = text + (f"\n\n— WhatsApp debug delivery failed: {reason}" if reason else "")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"[Frigate Debug] {first[:80]}"
    msg["From"]    = smtp_user
    msg["To"]      = ", ".join(recipients)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        log.info("[DEBUG] Fallback email sent to %d recipient(s)", len(recipients))
        return True
    except smtplib.SMTPException as exc:
        log.warning("[DEBUG] Fallback email failed: %s", exc)
        return False


def send_debug_whatsapp(text: str, secrets: dict) -> bool:
    """Send a diagnostic message to the SmokeTests group, retrying, then falling
    back to email if the WhatsApp transport is unavailable.

    Uses whatsapp_smoketest_jid — completely separate from the Casa Blumenau group
    JID used for real notifications. Returns True if delivered by either channel.
    """
    api_url  = secrets["whatsapp_api_url"].rstrip("/")
    api_key  = secrets["whatsapp_api_key"]
    instance = secrets["whatsapp_instance"]
    jid      = secrets.get("whatsapp_smoketest_jid", "")

    if jid and "@g.us" in jid:
        ok, detail = _post_with_retry(
            f"{api_url}/message/sendText/{instance}",
            {"number": jid, "textMessage": {"text": text}},
            api_key, timeout=15, what="[DEBUG] sendText",
        )
        if ok:
            log.info("[DEBUG] Debug message sent (WhatsApp)")
            return True
        log.warning("[DEBUG] WhatsApp debug failed after %d attempts: %s — falling back to email",
                    SEND_ATTEMPTS, detail)
        reason = detail
    else:
        log.warning("[DEBUG] whatsapp_smoketest_jid missing/invalid — falling back to email")
        reason = "smoketest_jid missing/invalid"

    return _send_debug_email(text, secrets, reason)


# ── Email sender ──────────────────────────────────────────────────────────────
_DIGEST_HTML = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: Arial, sans-serif; background: #f5f5f5; margin: 0; padding: 0; }}
  .wrapper {{ max-width: 640px; margin: 24px auto; background: #fff;
              border-radius: 8px; overflow: hidden;
              box-shadow: 0 2px 8px rgba(0,0,0,.15); }}
  .header {{ background: #1565C0; color: #fff; padding: 20px 28px; }}
  .header h1 {{ margin: 0; font-size: 20px; font-weight: 700; }}
  .header p  {{ margin: 4px 0 0; font-size: 13px; opacity: .85; }}
  .body {{ padding: 24px 28px; }}
  .narrative {{ background: #E3F2FD; border-left: 4px solid #1565C0;
               padding: 14px 18px; border-radius: 0 6px 6px 0;
               font-size: 15px; line-height: 1.7; color: #222; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #f0f0f0; padding: 8px 10px; text-align: left; border-bottom: 2px solid #ddd; }}
  td {{ border-top: 1px solid #eee; padding: 8px 10px; vertical-align: top; }}
  .chip {{ display: inline-block; background: #e8f5e9; color: #2e7d32;
           border-radius: 4px; padding: 1px 6px; font-size: 11px; margin: 1px; }}
  .meta {{ font-size: 12px; color: #888; margin-top: 14px; }}
  .footer {{ background: #fafafa; border-top: 1px solid #eee;
             padding: 12px 28px; font-size: 11px; color: #999; }}
</style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <h1>📹 Resumo de Atividade</h1>
    <p>{timestamp} &nbsp;·&nbsp; {n_events} evento(s) &nbsp;·&nbsp; últimos {window_min} min</p>
  </div>
  <div class="body">
    <div class="narrative">{narrative_html}</div>
    <table>
      <thead><tr><th>Hora</th><th>Local</th><th>Objetos</th><th>Descrição</th></tr></thead>
      <tbody>{event_rows}</tbody>
    </table>
    {cost_line}
    {video_note}
  </div>
  <div class="footer">
    Frigate Digest &nbsp;·&nbsp; bnu-homeassistant &nbsp;·&nbsp; gerado por {llm_label}
  </div>
</div>
</body>
</html>
"""


def send_email_digest(events: list[dict], narrative: str,
                      video_path: str | None, since: datetime.datetime,
                      window_min: int, secrets: dict, llm_label: str,
                      openai_cost: float | None = None) -> None:
    smtp_user  = secrets["boiler_smtp_user"]
    smtp_pass  = secrets["boiler_smtp_pass"]
    recipients = [
        e.strip() for e in secrets.get("frigate_email_recipients", "").split(",") if e.strip()
    ]
    if not recipients:
        log.error("No frigate_email_recipients in secrets.yaml")
        return

    timestamp    = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
    locations    = list(dict.fromkeys(e["location"] for e in events))
    ts_start     = events[0]["time"] if events else ""
    ts_end       = events[-1]["time"] if events else ""
    ts_range     = f"{ts_start}–{ts_end}" if ts_start != ts_end else ts_start
    event_rows   = "".join(
        f"<tr><td>{e['time']}</td><td>{e['location']}</td>"
        f"<td>{''.join(f'<span class=\"chip\">{o}</span>' for o in e['objects'])}</td>"
        f"<td>{(e['text'] or '')[:120]}</td></tr>"
        for e in events
    )
    cost_line = (
        f'<p class="meta">OpenAI: ${openai_cost:.4f} nesta chamada</p>'
        if openai_cost else ""
    )
    video_note = ""
    if video_path and os.path.exists(video_path):
        size_mb = os.path.getsize(video_path) / 1_048_576
        video_note = f'<p class="meta">📎 Vídeo compilado em anexo ({size_mb:.1f} MB)</p>'

    html = _DIGEST_HTML.format(
        timestamp     = f"{ts_range} — {timestamp}",
        n_events      = len(events),
        window_min    = window_min,
        narrative_html = narrative.replace("\n", "<br>"),
        event_rows    = event_rows,
        cost_line     = cost_line,
        video_note    = video_note,
        llm_label     = llm_label,
    )
    subject = f"[Frigate] Resumo: {', '.join(locations)} — {ts_range}"

    for recipient in recipients:
        msg            = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = recipient
        msg.attach(MIMEText(html, "html", "utf-8"))

        if video_path and os.path.exists(video_path):
            with open(video_path, "rb") as f:
                video_data = f.read()
            part = MIMEBase("video", "mp4")
            part.set_payload(video_data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment",
                            filename=os.path.basename(video_path))
            msg.attach(part)

        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            log.info("Digest email sent to %s", recipient)
        except smtplib.SMTPException as exc:
            log.error("Failed to send digest email to %s: %s", recipient, exc)


# ── Cleanup ───────────────────────────────────────────────────────────────────
def cleanup_old_files(pattern: str, max_age_hours: float) -> None:
    cutoff = time.time() - max_age_hours * 3600
    for path in glob_module.glob(pattern):
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
                log.info("Cleaned up: %s", path)
        except OSError:
            pass


# ── Main ──────────────────────────────────────────────────────────────────────
def _lock_is_held() -> bool:
    """Return True only if a live process holds the lock; removes stale locks."""
    if not os.path.exists(LOCK_FILE):
        return False
    try:
        with open(LOCK_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # signal 0 = existence check, no signal sent
        return True       # process is alive → lock is valid
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        log.info("Removing stale lock (process gone or unreadable)")
        try:
            os.unlink(LOCK_FILE)
        except OSError:
            pass
        return False


def main() -> None:
    mode = (sys.argv[1].strip() if len(sys.argv) > 1 else "auto").lower()

    if not os.environ.get("_FRIGATE_WORKER"):
        # Launcher: spawn an independent worker and exit immediately.
        # HA's shell_command sees the launcher exit in < 0.1 s, so its 60-second
        # timeout never fires.  The worker runs in a new session (start_new_session=True)
        # with no connection to HA's process group.
        env = os.environ.copy()
        env["_FRIGATE_WORKER"] = "1"
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

    # Worker process ─ single-instance guard then run
    if _lock_is_held():
        log.info("Digest already running — skipping trigger")
        sys.exit(0)
    lock_written = False
    try:
        with open(LOCK_FILE, "w") as _lf:
            _lf.write(str(os.getpid()))
        lock_written = True
    except OSError as exc:
        log.warning("Cannot create lock file (%s) — proceeding without lock", exc)
    try:
        _run(mode)
    finally:
        if lock_written:
            try:
                os.unlink(LOCK_FILE)
            except OSError:
                pass


class _DigestSkip(Exception):
    """Graceful no-op exit (e.g. no relevant events) — not a failure."""


def _run(mode: str) -> None:
    """Wrapper: reads the debug flag up front and reports any hard failure to the
    SmokeTests channel, so the debug channel surfaces *failed* attempts too."""
    log.info("=== Digest run starting (mode=%s) ===", mode)
    secrets  = load_secrets()
    ha_token = secrets.get("boiler_ha_token", "")
    debug_mode = get_ha_state("input_boolean.frigate_debug", ha_token) == "on"
    try:
        _run_inner(mode, secrets, ha_token, debug_mode)
    except _DigestSkip as skip:
        log.info("Digest skipped: %s", skip)
    except Exception as exc:
        log.exception("Digest run FAILED")
        if debug_mode:
            tb = traceback.format_exc().strip().splitlines()
            where = tb[-2].strip() if len(tb) >= 2 else ""
            send_debug_whatsapp(
                f"❌ *Digest FALHOU* ({mode})\n"
                f"{type(exc).__name__}: {str(exc)[:200]}\n"
                f"`{where[:160]}`",
                secrets,
            )
        raise


def _run_inner(mode: str, secrets: dict, ha_token: str, debug_mode: bool) -> None:
    now      = datetime.datetime.now()
    is_night = not (DAY_START <= now.hour < DAY_END)

    # Fixed 30-min lookback for both modes; clustering (below) isolates the real
    # burst for auto, while manual deliberately reports the whole 30-min window.
    since      = now - datetime.timedelta(minutes=30)
    window_min = 30
    log.info("Window: %s → %s (%d min)", since.strftime("%H:%M:%S"), now.strftime("%H:%M:%S"), window_min)

    # Fetch and filter reviews
    base = frigate_base(secrets["frigate_host"], secrets["frigate_port"])
    try:
        reviews = fetch_reviews(base, since)
    except requests.RequestException as exc:
        raise RuntimeError(f"Frigate fetch failed: {exc}") from exc

    log.info("Fetched %d total reviews; filtering for relevant objects...", len(reviews))
    reviews = [r for r in reviews if is_relevant(r)]
    if not reviews:
        raise _DigestSkip("no relevant events (person/car/animal) in window")

    # Group into bursts; an auto digest sends ONLY the most recent burst so events
    # separated by more than the cooldown are never lumped together.
    clusters = cluster_by_gap(reviews, COOLDOWN_GAP_S)
    if mode == "auto" and len(clusters) > 1:
        dropped = sum(len(c) for c in clusters[:-1])
        log.info("Auto: %d bursts in window; sending latest (%d events), dropping %d older",
                 len(clusters), len(clusters[-1]), dropped)
        reviews = clusters[-1]
    else:
        reviews = [r for c in clusters for r in c]   # manual: keep all, still ordered

    cameras_seen = list(dict.fromkeys(r.get("camera", "?") for r in reviews))
    log.info("%d relevant reviews across cameras: %s", len(reviews), cameras_seen)

    # Build event list with location labels (chronological by start_time)
    context, camera_to_location = load_property_context(PROPERTY_CTX)
    events: list[dict] = []
    for r in sorted(reviews, key=lambda x: x.get("start_time") or 0):
        det_list = (r.get("data") or {}).get("detections") or []
        det_id   = det_list[0] if det_list else None
        cam      = r.get("camera", "unknown")
        start_ts = r.get("start_time") or 0
        events.append({
            "camera":       cam,
            "location":     camera_to_location.get(cam, cam),
            "time":         datetime.datetime.fromtimestamp(start_ts).strftime("%H:%M"),
            "start_ts":     start_ts,
            "end_ts":       r.get("end_time") or start_ts,
            "text":         extract_genai_text(r),
            "objects":      (r.get("data") or {}).get("objects") or [],
            "detection_id": det_id,
        })

    span_start = datetime.datetime.fromtimestamp(events[0]["start_ts"]).strftime("%H:%M:%S")
    span_end   = datetime.datetime.fromtimestamp(events[-1]["end_ts"]).strftime("%H:%M:%S")

    # Download clips (actual recordings) and snapshots (keyframes for LLM)
    clips: dict[str, str | None]     = {}
    snapshots: dict[str, bytes | None] = {}
    for event in events:
        det_id = event["detection_id"]
        if det_id:
            clips[det_id]     = download_clip(base, det_id, CLIP_DIR)
            snapshots[det_id] = fetch_snapshot(base, det_id)

    # Compile video (single-pass CFR, chronological)
    video_path = compile_digest_video(events, clips)

    # Load baseline images
    all_cams = list(dict.fromkeys(e["camera"] for e in events))
    baselines = load_baselines(all_cams, BASELINES_DIR, is_night)

    # LLM inference
    llm_mode = get_ha_state("input_select.frigate_digest_llm", ha_token) or "Ollama (local)"
    log.info("LLM mode: %s", llm_mode)

    # Debug D1: "triggered" — as soon as a burst is in hand, before inference.
    if debug_mode:
        n_snaps = sum(1 for s in snapshots.values() if s)
        send_debug_whatsapp(
            f"🔍 *Frigate Digest* · {mode}\n"
            f"⏱ burst {span_start} → {span_end}\n"
            f"📸 {len(events)} event(s) · {', '.join(cameras_seen)}\n"
            f"🖼 {n_snaps} snapshot(s) · 🎞 {'ok' if video_path else 'none'} → {llm_mode}\n"
            f"⏳ Inference starting…",
            secrets,
        )

    narrative_ollama: str | None = None
    narrative_openai: str | None = None
    openai_cost: float | None    = None
    ollama_stats: dict            = {}

    if llm_mode in ("Ollama (local)", "Both"):
        narrative_ollama, ollama_stats = call_ollama(events, context, baselines, snapshots, window_min)

        # Debug D2: post-inference stats / failure detail.
        if debug_mode:
            if ollama_stats.get("timed_out"):
                status = f"⏰ TIMED OUT ({LLM_TIMEOUT}s)"
            elif ollama_stats.get("error"):
                status = f"❌ {ollama_stats['error'][:160]}"
            else:
                status = f"✅ {ollama_stats['elapsed_s']:.1f}s"
            send_debug_whatsapp(
                f"*Ollama* {status}\n"
                f"📥 {ollama_stats.get('n_images', 0)} image(s)"
                + (f" · prompt={ollama_stats['prompt_tokens']} tok" if ollama_stats.get("prompt_tokens") else "")
                + f"\n📤 {ollama_stats.get('response_chars', 0)} chars · {ollama_stats.get('output_tokens', 0)} tok out"
                + f"\n💭 thinking={ollama_stats.get('thinking_chars', 0)} chars"
                + (f" · done={ollama_stats['done_reason']}" if ollama_stats.get("done_reason") else ""),
                secrets,
            )

    if llm_mode in ("OpenAI (cloud)", "Both"):
        api_key = secrets.get("openai_api_key", "")
        if not api_key:
            log.error("openai_api_key not in secrets.yaml — skipping OpenAI")
        else:
            narrative_openai, tok_in, tok_out = call_openai(
                events, context, baselines, snapshots, window_min, api_key)
            if tok_in or tok_out:
                openai_cost = tok_in * OPENAI_IN_PRICE + tok_out * OPENAI_OUT_PRICE
                log.info("OpenAI cost this call: $%.4f (%d in / %d out tokens)",
                         openai_cost, tok_in, tok_out)
                prev_cost = get_ha_number("input_number.frigate_digest_openai_cost_total", ha_token)
                update_ha_number("input_number.frigate_digest_openai_cost_total",
                                 prev_cost + openai_cost, ha_token)

    if llm_mode == "Both" and narrative_ollama and narrative_openai:
        log.info("=== OLLAMA NARRATIVE ===\n%s", narrative_ollama)
        log.info("=== OPENAI NARRATIVE ===\n%s", narrative_openai)

    narrative = narrative_ollama or narrative_openai
    if not narrative:
        raise RuntimeError("no LLM narrative produced (inference failed/timed out)")

    llm_label = (
        "Ollama" if (narrative_ollama and not narrative_openai) else
        "OpenAI" if (narrative_openai and not narrative_ollama) else
        "Ollama + OpenAI"
    )

    # Send to channels gated by existing toggles
    notify_whatsapp = get_ha_state("input_boolean.frigate_notify_whatsapp", ha_token)
    notify_email    = get_ha_state("input_boolean.frigate_notify_email",    ha_token)

    if notify_whatsapp == "on":
        wa_ok, wa_detail = send_whatsapp_digest(events, narrative, video_path, secrets)
        if debug_mode:
            # Debug D3: did the real notification actually go out?
            send_debug_whatsapp(
                "📤 *Notificação enviada* ✅ (Casa Blumenau)" if wa_ok
                else f"📤 *Envio FALHOU* ❌\n{wa_detail[:200]}",
                secrets,
            )
    else:
        log.info("WhatsApp disabled (frigate_notify_whatsapp=off) — skipping")

    if notify_email == "on":
        send_email_digest(events, narrative, video_path, since, window_min,
                          secrets, llm_label, openai_cost)
    else:
        log.info("Email disabled (frigate_notify_email=off) — skipping")

    # Update last_run only for auto mode
    if mode == "auto":
        update_ha_datetime("input_datetime.frigate_digest_last_run", now, ha_token)
        log.info("Updated frigate_digest_last_run to %s", now)

    # Cleanup files older than 2 hours
    cleanup_old_files(os.path.join(CLIP_DIR, "frigate_clip_*.mp4"),    max_age_hours=2)
    cleanup_old_files(os.path.join(CLIP_DIR, "frigate_digest_*.mp4"),  max_age_hours=2)

    log.info("=== Digest complete: %d events, video=%s, LLM=%s ===",
             len(events), video_path, llm_label)


if __name__ == "__main__":
    main()
