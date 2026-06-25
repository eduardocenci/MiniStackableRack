#!/usr/bin/env python3
"""
Frigate Review Email Alert
Deploy to: /config/scripts/frigate_email.py

Usage: python3 frigate_email.py <review_id>

- Fetches the Frigate review (and polls for GenAI metadata if not yet ready)
- Downloads the animated GIF preview for the first detection event
- Sends an HTML email with the GIF embedded inline
- Reads all config from /config/secrets.yaml (no HA API token needed)
- Deduplicates: exits early if the GIF for this review_id already exists

Frigate version compatibility:
  0.14–0.16  GenAI stored in data.description
  0.17+      GenAI stored in data.metadata (title, shortSummary, scene)
"""

import os
import sys
import time
import smtplib
import datetime
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

import requests
import yaml

# ── Config (non-secret) ───────────────────────────────────────────────────────
GIF_DIR  = "/config/www"
LOG_FILE = "/config/frigate_email.log"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

# GenAI polling: wait up to 10 minutes (20 attempts × 30 s)
POLL_ATTEMPTS = 20
POLL_INTERVAL = 30  # seconds

_handler = logging.FileHandler(LOG_FILE)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [frigate_email] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger(__name__)


# ── Secrets ───────────────────────────────────────────────────────────────────
def load_secrets() -> dict:
    with open("/config/secrets.yaml") as f:
        return yaml.safe_load(f)


# ── Frigate API helpers ───────────────────────────────────────────────────────
def frigate_base(host: str, port: str) -> str:
    return f"http://{host}:{port}"


def fetch_review(base: str, review_id: str) -> dict:
    # Frigate 0.17+: singular /api/review endpoint
    r = requests.get(f"{base}/api/review/{review_id}", timeout=15)
    if r.ok:
        data = r.json()
        if isinstance(data, dict) and data.get("id"):
            return data
        if isinstance(data, list) and data:
            return data[0]
    # Fallback: list endpoint with id filter
    r = requests.get(f"{base}/api/review", params={"id": review_id, "limit": 1}, timeout=15)
    r.raise_for_status()
    items = r.json()
    return items[0] if items else {}


def download_gif(base: str, event_id: str) -> bytes:
    url = f"{base}/api/events/{event_id}/preview.gif"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


# ── GenAI text extraction ─────────────────────────────────────────────────────
def _fix_encoding(text: str) -> str:
    """Repair double-encoded UTF-8 that Frigate sometimes stores (e.g. 'Ã¡' → 'á')."""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def extract_genai(review: dict) -> tuple[str, str, str]:
    """
    Returns (title, short_summary, scene) from the review's GenAI data.
    Handles both Frigate 0.14-0.16 (data.description) and 0.17+ (data.metadata).
    All three values may be empty strings if GenAI hasn't run yet.
    """
    data = review.get("data") or {}

    # Frigate 0.14–0.16: flat description field
    if data.get("description"):
        desc = _fix_encoding(data["description"])
        return "", desc, ""

    # Frigate 0.17+: structured metadata
    meta = data.get("metadata") or {}
    title   = _fix_encoding(meta.get("title") or "")
    summary = _fix_encoding(meta.get("shortSummary") or "")
    scene   = _fix_encoding(meta.get("scene") or "")
    return title, summary, scene


def has_genai(review: dict) -> bool:
    title, summary, scene = extract_genai(review)
    return bool(title or summary or scene)


# ── Email builder ─────────────────────────────────────────────────────────────
_SEVERITY_COLOR = {
    "alert":     "#C62828",
    "detection": "#E65100",
}

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: Arial, sans-serif; background: #f5f5f5; margin: 0; padding: 0; }}
  .wrapper {{ max-width: 620px; margin: 24px auto; background: #fff;
              border-radius: 8px; overflow: hidden;
              box-shadow: 0 2px 8px rgba(0,0,0,.15); }}
  .header {{ background: {header_color}; color: #fff; padding: 20px 28px; }}
  .header h1 {{ margin: 0; font-size: 20px; font-weight: 700; }}
  .header p  {{ margin: 4px 0 0; font-size: 13px; opacity: .85; }}
  .body {{ padding: 24px 28px; }}
  .chip {{ display: inline-block; background: #f0f0f0; border-radius: 4px;
           padding: 2px 8px; font-size: 12px; color: #555; margin-right: 6px; }}
  .ai-title {{ font-size: 16px; font-weight: 600; color: #222; margin: 16px 0 6px; }}
  .ai-summary {{ background: #fafafa; border-left: 4px solid {header_color};
                 padding: 12px 16px; border-radius: 0 4px 4px 0;
                 font-size: 14px; line-height: 1.6; color: #333; margin-bottom: 14px; }}
  .ai-scene {{ font-size: 13px; line-height: 1.7; color: #555; margin-bottom: 18px; }}
  .ai-fallback {{ background: #fafafa; border-left: 4px solid {header_color};
                  padding: 12px 16px; border-radius: 0 4px 4px 0;
                  font-size: 14px; line-height: 1.6; color: #333; margin: 16px 0; }}
  .gif-wrap {{ text-align: center; margin: 18px 0; }}
  .gif-wrap img {{ max-width: 100%; border-radius: 6px;
                   box-shadow: 0 2px 6px rgba(0,0,0,.2); }}
  .footer {{ background: #fafafa; border-top: 1px solid #eee;
             padding: 12px 28px; font-size: 11px; color: #999; }}
</style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <h1>{severity_label}: {camera}</h1>
    <p>{timestamp}</p>
  </div>
  <div class="body">
    <p>{object_chips}</p>
    {genai_block}
    <div class="gif-wrap">
      <img src="cid:preview_gif" alt="Event preview">
    </div>
  </div>
  <div class="footer">
    Frigate NVR &nbsp;·&nbsp; bg-homeassistant &nbsp;·&nbsp; review {review_id}
  </div>
</div>
</body>
</html>
"""


def _build_genai_block(title: str, summary: str, scene: str) -> str:
    if not (title or summary or scene):
        return '<div class="ai-fallback">[GenAI summary unavailable]</div>'
    parts = []
    if title:
        parts.append(f'<div class="ai-title">{title}</div>')
    if summary:
        parts.append(f'<div class="ai-summary">{summary.replace(chr(10), "<br>")}</div>')
    if scene:
        parts.append(f'<div class="ai-scene">{scene.replace(chr(10), "<br>")}</div>')
    return "\n    ".join(parts)


def build_html(review: dict, review_id: str, title: str, summary: str, scene: str) -> str:
    camera   = review.get("camera", "unknown")
    severity = review.get("severity", "detection")
    objects  = (review.get("data") or {}).get("objects") or []

    header_color   = _SEVERITY_COLOR.get(severity, "#455A64")
    severity_label = severity.capitalize()
    timestamp      = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    object_chips   = " ".join(
        f'<span class="chip">{o}</span>' for o in objects
    ) or '<span class="chip">unknown object</span>'
    genai_block = _build_genai_block(title, summary, scene)

    return _HTML_TEMPLATE.format(
        header_color   = header_color,
        severity_label = severity_label,
        camera         = camera,
        timestamp      = timestamp,
        object_chips   = object_chips,
        genai_block    = genai_block,
        review_id      = review_id,
    )


def build_subject(review: dict, title: str) -> str:
    camera   = review.get("camera", "unknown")
    severity = review.get("severity", "detection").capitalize()
    objects  = (review.get("data") or {}).get("objects") or []
    obj_str  = ", ".join(objects) if objects else "object"
    suffix   = f" — {title}" if title else f" — {obj_str}"
    return f"[Frigate] {severity}: {camera}{suffix}"


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    if len(sys.argv) < 2:
        log.error("Usage: frigate_email.py <review_id>")
        sys.exit(1)

    review_id = sys.argv[1].strip()
    gif_path  = os.path.join(GIF_DIR, f"frigate_{review_id}.gif")

    # Deduplication: skip if already processed
    if os.path.exists(gif_path):
        log.info("Already processed review %s — skipping", review_id)
        return

    secrets    = load_secrets()
    base       = frigate_base(secrets["frigate_host"], secrets["frigate_port"])
    smtp_user  = secrets["boiler_smtp_user"]
    smtp_pass  = secrets["boiler_smtp_pass"]
    recipients = [e.strip() for e in secrets["frigate_email_recipients"].split(",") if e.strip()]

    if not recipients:
        log.error("No recipients in frigate_email_recipients (secrets.yaml)")
        sys.exit(1)

    # Fetch review; poll until GenAI content is available
    log.info("Fetching review %s ...", review_id)
    review = {}
    title = summary = scene = ""
    for attempt in range(1, POLL_ATTEMPTS + 1):
        try:
            review = fetch_review(base, review_id)
            title, summary, scene = extract_genai(review)
            if has_genai(review):
                log.info("GenAI content ready (attempt %d)", attempt)
                break
        except requests.RequestException as exc:
            log.warning("Attempt %d — Frigate API error: %s", attempt, exc)

        if attempt < POLL_ATTEMPTS:
            log.info("No GenAI content yet — waiting %ds (attempt %d/%d)",
                     POLL_INTERVAL, attempt, POLL_ATTEMPTS)
            time.sleep(POLL_INTERVAL)

    if not has_genai(review):
        log.warning("GenAI not available after polling — sending without it")

    # Resolve first detection event_id for the GIF
    detections = (review.get("data") or {}).get("detections") or []
    if not detections:
        log.error("Review %s has no detections — cannot fetch GIF", review_id)
        sys.exit(1)

    event_id = detections[0]
    log.info("Downloading preview.gif for event %s ...", event_id)
    try:
        gif_data = download_gif(base, event_id)
    except requests.RequestException as exc:
        log.error("Failed to download GIF: %s", exc)
        sys.exit(1)

    os.makedirs(GIF_DIR, exist_ok=True)
    with open(gif_path, "wb") as f:
        f.write(gif_data)
    log.info("GIF saved to %s (%d bytes)", gif_path, len(gif_data))

    log.info("Recipients: %s", recipients)
    html    = build_html(review, review_id, title, summary, scene)
    subject = build_subject(review, title)

    for recipient in recipients:
        msg            = MIMEMultipart("related")
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = recipient

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html, "html", "utf-8"))
        msg.attach(alt)

        gif_part = MIMEImage(gif_data, _subtype="gif")
        gif_part.add_header("Content-ID", "<preview_gif>")
        gif_part.add_header("Content-Disposition", "inline",
                            filename=f"frigate_{review_id}.gif")
        msg.attach(gif_part)

        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            log.info("Email sent to %s", recipient)
        except smtplib.SMTPException as exc:
            log.error("Failed to send to %s: %s", recipient, exc)

    log.info("Done — review %s", review_id)


if __name__ == "__main__":
    main()
