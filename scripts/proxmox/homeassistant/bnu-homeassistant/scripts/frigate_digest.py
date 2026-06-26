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
import smtplib
import subprocess
import sys
import tempfile
import time
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
PROPERTY_CTX     = "/config/frigate_property_context.txt"
BASELINES_DIR    = "/config/frigate_baselines"
CLIP_DIR         = "/config/www"
FFMPEG           = "/usr/bin/ffmpeg"
RELEVANT_OBJECTS = {"person", "car", "dog", "cat", "animal", "bicycle", "motorcycle"}
LLM_TIMEOUT      = 600   # seconds — generation can be slow on first call
DAY_START        = 6     # 06:00
DAY_END          = 20    # 20:00

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


def read_ha_datetime(entity_id: str, token: str) -> datetime.datetime | None:
    state = get_ha_state(entity_id, token)
    if not state or state in ("unknown", "unavailable", "None", ""):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.datetime.strptime(state, fmt)
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            pass
    return None


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
    valid = [
        (e, clips[e["detection_id"]])
        for e in events
        if e.get("detection_id") and clips.get(e["detection_id"])
    ]
    if not valid:
        log.info("No valid clips available — skipping video compilation")
        return None

    tmp_dir = tempfile.mkdtemp(prefix="frigate_digest_")
    labeled_clips: list[str] = []
    tmp_clips:    list[str] = []   # only temp files we created (safe to delete)

    for i, (event, clip_path) in enumerate(valid):
        out = os.path.join(tmp_dir, f"labeled_{i:03d}.mp4")
        cmd = [
            FFMPEG, "-y", "-i", clip_path,
            "-vf", "scale=854:480:force_original_aspect_ratio=decrease,pad=854:480:(ow-iw)/2:(oh-ih)/2",
            "-c:v", "libx264", "-crf", "28", "-preset", "fast", "-an", out,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=180)
            labeled_clips.append(out)
            tmp_clips.append(out)
            log.info("Clip %d scaled: %s", i, out)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            log.warning("Failed to scale clip %d (%s): %s — using original", i, clip_path, exc)
            labeled_clips.append(clip_path)  # original stays; don't delete it

    if not labeled_clips:
        log.error("No clips available — cannot compile video")
        return None

    concat_file = os.path.join(tmp_dir, "filelist.txt")
    with open(concat_file, "w") as f:
        for clip in labeled_clips:
            f.write(f"file '{clip}'\n")

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    output = os.path.join(CLIP_DIR, f"frigate_digest_{timestamp}.mp4")

    try:
        subprocess.run(
            [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", concat_file, "-c", "copy", output],
            check=True, capture_output=True, timeout=300,
        )
        log.info("Digest video compiled: %s (%d bytes)", output, os.path.getsize(output))
        return output
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        log.error("Video concat failed: %s", exc)
        return None
    finally:
        for p in tmp_clips + [concat_file]:
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


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
def call_ollama(events: list[dict], context: str, baselines: dict[str, bytes],
                snapshots: dict[str, bytes | None], window_min: int) -> str | None:
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
    try:
        log.info("Calling Ollama (%s images)...", len(images))
        r = requests.post(
            f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/generate",
            json=payload, timeout=LLM_TIMEOUT,
        )
        r.raise_for_status()
        data     = r.json()
        response = data.get("response", "").strip()
        thinking = data.get("thinking", "")
        log.info("Ollama response (%d chars, thinking=%d chars, done=%s): %s...",
                 len(response), len(thinking), data.get("done_reason"), response[:120])
        return response or None
    except requests.RequestException as exc:
        log.error("Ollama call failed: %s", exc)
        return None


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
def send_whatsapp_digest(events: list[dict], narrative: str,
                         video_path: str | None, secrets: dict) -> None:
    api_url  = secrets["whatsapp_api_url"].rstrip("/")
    api_key  = secrets["whatsapp_api_key"]
    instance = secrets["whatsapp_instance"]
    jid      = secrets["whatsapp_group_jid"]

    # SECURITY: only group JIDs allowed
    if not jid or "@g.us" not in jid:
        log.error("whatsapp_group_jid missing or not a group JID — aborting")
        return

    locations = list(dict.fromkeys(e["location"] for e in events))
    ts_start = events[0]["time"] if events else ""
    ts_end   = events[-1]["time"] if events else ""
    ts_range = f"{ts_start}–{ts_end}" if ts_start != ts_end else ts_start
    text = (
        f"*📹 Resumo — {ts_range}*\n\n"
        f"{narrative}\n\n"
        f"_{len(events)} evento(s) · {', '.join(locations)}_"
    )

    try:
        r = requests.post(
            f"{api_url}/message/sendText/{instance}",
            json={"number": jid, "textMessage": {"text": text}},
            headers={"apikey": api_key}, timeout=30,
        )
        r.raise_for_status()
        log.info("WhatsApp text sent (status %s)", r.status_code)
    except requests.RequestException as exc:
        log.error("WhatsApp sendText failed: %s", exc)
        return

    if not video_path or not os.path.exists(video_path):
        return

    filename = os.path.basename(video_path)
    # Serve via HA local web server so the gateway downloads it directly (avoids ~17 MB JSON payload)
    video_url = f"http://10.1.1.124:8123/local/{filename}"
    try:
        r = requests.post(
            f"{api_url}/message/sendMedia/{instance}",
            json={
                "number": jid,
                "mediaMessage": {
                    "mediatype": "video",
                    "mimetype":  "video/mp4",
                    "media":     video_url,
                    "caption":   "",
                    "fileName":  filename,
                },
            },
            headers={"apikey": api_key}, timeout=120,
        )
        r.raise_for_status()
        log.info("WhatsApp video sent via URL (status %s)", r.status_code)
    except requests.RequestException as exc:
        log.warning("WhatsApp video send failed (non-fatal): %s", exc)


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
    locations = list(dict.fromkeys(e["location"] for e in events))
    ts_start  = events[0]["time"] if events else ""
    ts_end    = events[-1]["time"] if events else ""
    ts_range  = f"{ts_start}–{ts_end}" if ts_start != ts_end else ts_start
    subject   = f"[Frigate] Resumo: {', '.join(locations)} — {ts_range}"

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
def main() -> None:
    mode = (sys.argv[1].strip() if len(sys.argv) > 1 else "auto").lower()
    log.info("=== Digest run starting (mode=%s) ===", mode)

    secrets  = load_secrets()
    ha_token = secrets.get("boiler_ha_token", "")
    now      = datetime.datetime.now()
    is_night = not (DAY_START <= now.hour < DAY_END)

    # Time window
    if mode == "manual":
        since      = now - datetime.timedelta(minutes=30)
        window_min = 30
    else:
        since_dt   = read_ha_datetime("input_datetime.frigate_digest_last_run", ha_token)
        since      = since_dt if since_dt else now - datetime.timedelta(hours=1)
        window_min = max(1, int((now - since).total_seconds() / 60))

    log.info("Window: %s → %s (%d min)", since.strftime("%H:%M:%S"), now.strftime("%H:%M:%S"), window_min)

    # Fetch and filter reviews
    base = frigate_base(secrets["frigate_host"], secrets["frigate_port"])
    try:
        reviews = fetch_reviews(base, since)
    except requests.RequestException as exc:
        log.error("Failed to fetch reviews from Frigate: %s", exc)
        sys.exit(1)

    log.info("Fetched %d total reviews; filtering for relevant objects...", len(reviews))
    reviews = [r for r in reviews if is_relevant(r)]
    if not reviews:
        log.info("No relevant events (person/car/animal) in window — skipping digest")
        sys.exit(0)

    cameras_seen = list(dict.fromkeys(r.get("camera", "?") for r in reviews))
    log.info("%d relevant reviews across cameras: %s", len(reviews), cameras_seen)

    # Build event list with location labels
    context, camera_to_location = load_property_context(PROPERTY_CTX)
    events: list[dict] = []
    for r in sorted(reviews, key=lambda x: x.get("start_time", 0)):
        det_list = (r.get("data") or {}).get("detections") or []
        det_id   = det_list[0] if det_list else None
        cam      = r.get("camera", "unknown")
        events.append({
            "camera":       cam,
            "location":     camera_to_location.get(cam, cam),
            "time":         datetime.datetime.fromtimestamp(
                r.get("start_time", 0)).strftime("%H:%M"),
            "text":         extract_genai_text(r),
            "objects":      (r.get("data") or {}).get("objects") or [],
            "detection_id": det_id,
        })

    # Download clips (actual recordings) and snapshots (keyframes for LLM)
    clips: dict[str, str | None]     = {}
    snapshots: dict[str, bytes | None] = {}
    for event in events:
        det_id = event["detection_id"]
        if det_id:
            clips[det_id]     = download_clip(base, det_id, CLIP_DIR)
            snapshots[det_id] = fetch_snapshot(base, det_id)

    # Compile video
    video_path = compile_digest_video(events, clips)

    # Load baseline images
    all_cams = list(dict.fromkeys(e["camera"] for e in events))
    baselines = load_baselines(all_cams, BASELINES_DIR, is_night)

    # LLM inference
    llm_mode = get_ha_state("input_select.frigate_digest_llm", ha_token) or "Ollama (local)"
    log.info("LLM mode: %s", llm_mode)

    narrative_ollama: str | None = None
    narrative_openai: str | None = None
    openai_cost: float | None    = None

    if llm_mode in ("Ollama (local)", "Both"):
        narrative_ollama = call_ollama(events, context, baselines, snapshots, window_min)

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
        log.error("No LLM narrative produced — aborting")
        sys.exit(1)

    llm_label = (
        "Ollama" if (narrative_ollama and not narrative_openai) else
        "OpenAI" if (narrative_openai and not narrative_ollama) else
        "Ollama + OpenAI"
    )

    # Send to channels gated by existing toggles
    notify_whatsapp = get_ha_state("input_boolean.frigate_notify_whatsapp", ha_token)
    notify_email    = get_ha_state("input_boolean.frigate_notify_email",    ha_token)

    if notify_whatsapp == "on":
        send_whatsapp_digest(events, narrative, video_path, secrets)
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
