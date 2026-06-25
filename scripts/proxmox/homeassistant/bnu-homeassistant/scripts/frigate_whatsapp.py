#!/usr/bin/env python3
"""
Frigate Review WhatsApp Alert
Deploy to: /config/scripts/frigate_whatsapp.py

Usage: python3 frigate_whatsapp.py <review_id>

SECURITY CONSTRAINTS (enforced in code):
  - Sends to exactly ONE destination: secrets["whatsapp_group_jid"] (Casa Blumenau)
  - Makes ZERO read calls to the WhatsApp API (no fetchMessages, fetchChats, etc.)
  - Outbound API calls: sendText (guaranteed) + sendMedia GIF attempt, both to ALLOWED_JID

Reads config from /config/secrets.yaml. Reuses the GIF already downloaded by
frigate_email.py if present; otherwise downloads it itself.
"""

import base64
import datetime
import logging
import os
import sys
import time

import requests
import yaml

# ── Config ────────────────────────────────────────────────────────────────────
GIF_DIR        = "/config/www"
LOG_FILE       = "/config/frigate_whatsapp.log"
MAX_MSG_CHARS  = 280
POLL_ATTEMPTS  = 20
POLL_INTERVAL  = 30

_handler = logging.FileHandler(LOG_FILE)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [frigate_whatsapp] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger(__name__)

_SEVERITY_EMOJI = {"alert": "🟡", "detection": "🔵"}


# ── Secrets ───────────────────────────────────────────────────────────────────
def load_secrets() -> dict:
    with open("/config/secrets.yaml") as f:
        return yaml.safe_load(f)


# ── Frigate API helpers (mirrors frigate_email.py) ───────────────────────────
def frigate_base(host: str, port: str) -> str:
    return f"http://{host}:{port}"


def fetch_review(base: str, review_id: str) -> dict:
    r = requests.get(f"{base}/api/review/{review_id}", timeout=15)
    if r.ok:
        data = r.json()
        if isinstance(data, dict) and data.get("id"):
            return data
        if isinstance(data, list) and data:
            return data[0]
    r = requests.get(f"{base}/api/review", params={"id": review_id, "limit": 1}, timeout=15)
    r.raise_for_status()
    items = r.json()
    return items[0] if items else {}


def download_gif(base: str, event_id: str) -> bytes:
    r = requests.get(f"{base}/api/events/{event_id}/preview.gif", timeout=60)
    r.raise_for_status()
    return r.content


def _fix_encoding(text: str) -> str:
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def extract_genai(review: dict) -> tuple[str, str, str, int, str | None]:
    """Returns (title, shortSummary, scene, potential_threat_level, other_concerns)."""
    data = review.get("data") or {}
    if data.get("description"):
        return "", _fix_encoding(data["description"]), "", 0, None
    meta = data.get("metadata") or {}
    return (
        _fix_encoding(meta.get("title") or ""),
        _fix_encoding(meta.get("shortSummary") or ""),
        _fix_encoding(meta.get("scene") or ""),
        int(meta.get("potential_threat_level") or 0),
        _fix_encoding(meta.get("other_concerns") or "") or None,
    )


def has_genai(review: dict) -> bool:
    title, summary, scene, _, _ = extract_genai(review)
    return any((title, summary, scene))


# ── Message builder ───────────────────────────────────────────────────────────
def build_message(review: dict, title: str, summary: str,
                  threat_level: int, other_concerns: str | None) -> str:
    camera   = review.get("camera", "unknown")
    severity = review.get("severity", "detection")
    objects  = (review.get("data") or {}).get("objects") or []
    obj_str  = ", ".join(objects) if objects else "object"

    # 🔴 only when there is an actual concern; 🟡 for normal alerts; 🔵 for detections
    if threat_level > 0 or other_concerns:
        emoji = "🔴"
    else:
        emoji = _SEVERITY_EMOJI.get(severity, "🔵")

    header   = f"*{emoji} {severity.capitalize()}: {camera}*"
    body     = title if title else summary

    base_len = len(header) + 1 + len(obj_str) + 2
    remaining = MAX_MSG_CHARS - base_len
    if body and len(body) > remaining:
        body = body[:remaining - 1] + "…"

    parts = [header, obj_str]
    if body:
        parts += ["", body]
    return "\n".join(parts)


# ── WhatsApp sender (outbound-only) ───────────────────────────────────────────
def send_text(api_url: str, api_key: str, instance: str, jid: str, text: str) -> None:
    url = f"{api_url}/message/sendText/{instance}"
    r = requests.post(
        url,
        json={"number": jid, "textMessage": {"text": text}},
        headers={"apikey": api_key},
        timeout=30,
    )
    r.raise_for_status()
    log.info("Text sent (status %s)", r.status_code)


def try_send_gif(api_url: str, api_key: str, instance: str,
                 jid: str, gif_data: bytes, review_id: str) -> None:
    """Best-effort GIF send — logs warning on failure, never raises."""
    gif_b64 = base64.b64encode(gif_data).decode()
    url = f"{api_url}/message/sendMedia/{instance}"
    try:
        r = requests.post(
            url,
            json={
                "number": jid,
                "mediaMessage": {
                    "mediatype": "video",
                    "mimetype": "video/mp4",
                    "media": gif_b64,
                    "caption": "",
                    "fileName": f"frigate_{review_id}.mp4",
                    "gifPlayback": True,
                },
            },
            headers={"apikey": api_key},
            timeout=60,
        )
        r.raise_for_status()
        log.info("GIF sent (status %s)", r.status_code)
    except Exception as exc:
        log.warning("GIF send failed (non-fatal): %s", exc)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    if len(sys.argv) < 2:
        log.error("Usage: frigate_whatsapp.py <review_id>")
        sys.exit(1)

    review_id = sys.argv[1].strip()
    gif_path  = os.path.join(GIF_DIR, f"frigate_{review_id}.gif")

    secrets  = load_secrets()
    api_url  = secrets["whatsapp_api_url"].rstrip("/")
    api_key  = secrets["whatsapp_api_key"]
    instance = secrets["whatsapp_instance"]

    # SECURITY: enforce single allowed destination — no other JID may be used
    ALLOWED_JID = secrets["whatsapp_group_jid"]
    if not ALLOWED_JID or "@g.us" not in ALLOWED_JID:
        log.error("whatsapp_group_jid in secrets.yaml is missing or not a group JID (@g.us). Aborting.")
        sys.exit(1)

    base = frigate_base(secrets["frigate_host"], secrets["frigate_port"])

    log.info("Fetching review %s ...", review_id)
    review = {}
    title = summary = scene = ""
    threat_level = 0
    other_concerns = None
    for attempt in range(1, POLL_ATTEMPTS + 1):
        try:
            review = fetch_review(base, review_id)
            title, summary, scene, threat_level, other_concerns = extract_genai(review)
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

    # Get GIF: reuse if already downloaded by frigate_email.py, else fetch
    if os.path.exists(gif_path):
        log.info("Reusing GIF from %s", gif_path)
        with open(gif_path, "rb") as f:
            gif_data = f.read()
    else:
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

    message = build_message(review, title, summary, threat_level, other_concerns)
    log.info("Sending to %s (%d chars)", ALLOWED_JID, len(message))

    try:
        send_text(api_url, api_key, instance, ALLOWED_JID, message)
    except requests.RequestException as exc:
        log.error("WhatsApp sendText error: %s", exc)
        sys.exit(1)

    try_send_gif(api_url, api_key, instance, ALLOWED_JID, gif_data, review_id)

    log.info("Done — review %s", review_id)


if __name__ == "__main__":
    main()
