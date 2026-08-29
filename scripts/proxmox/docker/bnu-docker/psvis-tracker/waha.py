"""Send the flight report through WAHA (same container network)."""
import logging
import os

import requests

log = logging.getLogger("psvis.waha")

WAHA_BASE_URL = os.environ.get("WAHA_BASE_URL", "http://waha:3000")
WAHA_API_KEY = os.environ.get("WAHA_API_KEY", "")
WAHA_SESSION = os.environ.get("WAHA_SESSION", "default")

HEADERS = {"X-Api-Key": WAHA_API_KEY, "Content-Type": "application/json"}


def send_image(jid, image_url, caption):
    """sendImage by URL (works on WAHA Core; WAHA fetches the URL itself,
    so it must be resolvable from the WAHA container — e.g. this service's
    container name on the shared docker network). Falls back to plain text."""
    payload = {
        "session": WAHA_SESSION,
        "chatId": jid,
        "file": {"mimetype": "image/png", "filename": "voo.png", "url": image_url},
        "caption": caption,
    }
    r = requests.post(f"{WAHA_BASE_URL}/api/sendImage", json=payload, headers=HEADERS, timeout=60)
    if r.status_code >= 300:
        log.error("sendImage failed (%s): %s — falling back to text", r.status_code, r.text[:300])
        send_text(jid, caption)
    return r.status_code


def send_text(jid, text):
    payload = {"session": WAHA_SESSION, "chatId": jid, "text": text}
    r = requests.post(f"{WAHA_BASE_URL}/api/sendText", json=payload, headers=HEADERS, timeout=30)
    if r.status_code >= 300:
        log.error("sendText failed (%s): %s", r.status_code, r.text[:300])
    return r.status_code
