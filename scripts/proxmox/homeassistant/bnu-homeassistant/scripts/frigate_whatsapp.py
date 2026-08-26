#!/usr/bin/env python3
"""
Frigate Review WhatsApp Alert
Deploy to: /config/scripts/frigate_whatsapp.py

Usage: python3 frigate_whatsapp.py <review_id>

SECURITY CONSTRAINTS (enforced in code):
  - Destinations limited to the THREE group JIDs from secrets.yaml:
    whatsapp_group_jid (Casa Blumenau, default), whatsapp_group_jid_ceuazul
    (Casa Céu Azul — obra cameras) and whatsapp_smoketest_jid (--test mode
    only). Each run sends to exactly ONE of them.
  - Makes ZERO read calls to the WhatsApp API (no fetchMessages, fetchChats, etc.)
  - Outbound API calls: sendText (guaranteed) + sendMedia GIF attempt, both
    to the single chosen JID

ARA presence gate (decisão Eduardo 26/08/2026): reviews of the obra cameras
(CEUAZUL_CAMERAS) are sent ONLY when the canteiro LAN has NO phone online
at all — ANY non-fixed device counts, nicknamed or not: whoever has the
Wi-Fi credentials is assumed allowed to be there (nicknames remain
display-only, for the daily presence report). Fixed gear (router, camera,
the Pi) is excluded. netoverview unreachable → fail open (send).

Reads config from /config/secrets.yaml. Reuses the GIF already downloaded by
frigate_email.py if present; otherwise downloads it itself.
Debug: `frigate_whatsapp.py --presence-test` prints the gate + routing
state without sending anything. `frigate_whatsapp.py --test [review_id]`
sends a full "[TESTE]" alert (latest canteiro review by default) to the
SmokeTests group, with the real routing + gate verdict in the footer.
"""

import base64
import datetime
import logging
import os
import subprocess
import sys
import tempfile
import time

import requests
import yaml

from waha import WahaClient  # shared WAHA (WhatsApp HTTP API) client, /config/scripts/waha.py

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

# ── ARA (obra) routing + presence gate ────────────────────────────────────────
CEUAZUL_CAMERAS = {"canteiro", "canteiro_sub"}
# Tailnet IP of ara-raspberrypi — MagicDNS names may not resolve inside the
# HA container (same reason canteiro-watchdog defaults to the IP)
ARA_NTO_DEVICES = "http://100.66.255.82:5000/api/devices"
ARA_FIXED_MACS  = {"74:24:9f:c5:b2:5f", "54:ba:d9:bd:34:e3"}  # Starlink router, iM9 camera
ARA_FIXED_HOSTS = ("ara-raspberrypi",)


def phone_online() -> tuple[bool, str]:
    """(True, label) when ANY non-fixed device is online on the canteiro LAN.

    Nicknamed or not — anyone with the Wi-Fi credentials is assumed allowed
    to be at the obra (decisão Eduardo 26/08/2026). Only the fixed gear
    (router/camera by MAC, the Pi by hostname) is ignored.
    """
    try:
        r = requests.get(ARA_NTO_DEVICES, timeout=10)
        r.raise_for_status()
        for d in r.json().get("devices", []):
            if not d.get("online"):
                continue
            mac  = (d.get("mac") or "").lower()
            host = (d.get("hostname") or "").lower()
            if mac in ARA_FIXED_MACS or any(h in host for h in ARA_FIXED_HOSTS):
                continue
            return True, d.get("nickname") or d.get("display_name") or mac or d.get("ip") or "?"
        return False, ""
    except Exception as exc:  # fail open: a flaky Starlink must not swallow alerts
        log.warning("ARA presence check failed (%s) — failing open", exc)
        return False, ""


def latest_review_id(base: str) -> str:
    """Newest review whose GenAI is already done, preferring canteiro (test helper).

    Avoids the 30 s GenAI polling loop in --test mode — HA's shell_command
    kills anything past 60 s.
    """
    def genai_ready(r: dict) -> bool:
        data = r.get("data") or {}
        meta = data.get("metadata") or {}
        return bool(data.get("description") or meta.get("title") or meta.get("shortSummary"))

    fallback = ""
    for params in ({"cameras": "canteiro", "limit": 10}, {"limit": 10}):
        try:
            resp = requests.get(f"{base}/api/review", params=params, timeout=15)
            resp.raise_for_status()
            items = resp.json() or []
            for r in items:
                if genai_ready(r):
                    return r["id"]
            if items and not fallback:
                fallback = items[0]["id"]
        except requests.RequestException as exc:
            log.warning("latest_review_id(%s) failed: %s", params, exc)
    return fallback


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

    # 🔴 only when there is an actual concern; 🟡 for normal alerts; 🔵 for detections
    if threat_level > 0 or other_concerns:
        emoji = "🔴"
    else:
        emoji = _SEVERITY_EMOJI.get(severity, "🔵")

    header = f"*{emoji} Câmera: {camera}*"
    body   = title if title else summary

    remaining = MAX_MSG_CHARS - len(header) - 2
    if body and len(body) > remaining:
        body = body[:remaining - 1] + "…"

    parts = [header]
    if body:
        parts += ["", body]
    return "\n".join(parts)


# ── WhatsApp sender (outbound-only, WAHA) ─────────────────────────────────────
def send_text(client: WahaClient, jid: str, text: str) -> tuple[bool, str]:
    ok, detail = client.send_text(jid, text, timeout=30)
    if ok:
        log.info("Text sent")
    return ok, detail


_FFMPEG = "/usr/bin/ffmpeg"


def gif_to_mp4(gif_data: bytes) -> bytes:
    """Convert GIF bytes to MP4 bytes via ffmpeg. Raises on failure."""
    gif_tmp = mp4_tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as f:
            f.write(gif_data)
            gif_tmp = f.name
        mp4_tmp = gif_tmp.replace(".gif", ".mp4")
        subprocess.run(
            [_FFMPEG, "-y", "-i", gif_tmp,
             "-movflags", "+faststart",
             "-pix_fmt", "yuv420p",
             "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
             mp4_tmp],
            check=True, capture_output=True,
        )
        with open(mp4_tmp, "rb") as f:
            return f.read()
    finally:
        for p in (gif_tmp, mp4_tmp):
            if p and os.path.exists(p):
                os.unlink(p)


def try_send_gif(client: WahaClient, jid: str, gif_data: bytes, review_id: str) -> None:
    """Best-effort short clip send (GIF→MP4) — logs warning on failure, never raises."""
    try:
        log.info("Converting GIF → MP4 ...")
        mp4_data = gif_to_mp4(gif_data)
        log.info("Conversion done (%d bytes)", len(mp4_data))
    except Exception as exc:
        log.warning("GIF→MP4 conversion failed (non-fatal): %s", exc)
        return

    mp4_b64 = base64.b64encode(mp4_data).decode()
    ok, detail = client.send_video_b64(jid, mp4_b64, f"frigate_{review_id}.mp4", timeout=60)
    if ok:
        log.info("GIF (as video) sent")
    else:
        log.warning("GIF send failed (non-fatal): %s", detail)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    if len(sys.argv) < 2:
        log.error("Usage: frigate_whatsapp.py <review_id>")
        sys.exit(1)

    if sys.argv[1] == "--presence-test":
        secrets = load_secrets()
        present, who = phone_online()
        print(f"phone_online={present} ({who or '-'}) | "
              f"canteiro→{secrets.get('whatsapp_group_jid_ceuazul', 'MISSING')} | "
              f"default→{secrets.get('whatsapp_group_jid')} | "
              f"test→{secrets.get('whatsapp_smoketest_jid', 'MISSING')}")
        return

    test_mode = sys.argv[1] == "--test"

    secrets = load_secrets()
    client  = WahaClient(secrets)

    # SECURITY: destinations limited to the group JIDs from secrets.yaml
    DEFAULT_JID   = secrets["whatsapp_group_jid"]                           # Casa Blumenau
    CEUAZUL_JID   = secrets.get("whatsapp_group_jid_ceuazul") or DEFAULT_JID  # Casa Céu Azul
    SMOKETEST_JID = secrets.get("whatsapp_smoketest_jid") or ""
    jids = [DEFAULT_JID, CEUAZUL_JID] + ([SMOKETEST_JID] if test_mode else [])
    for jid in jids:
        if not jid or "@g.us" not in jid:
            log.error("whatsapp group JID missing or not a group JID (@g.us) in secrets.yaml. Aborting.")
            sys.exit(1)

    base = frigate_base(secrets["frigate_host"], secrets["frigate_port"])

    if test_mode:
        review_id = sys.argv[2].strip() if len(sys.argv) > 2 else latest_review_id(base)
        if not review_id:
            log.error("--test: no review found in Frigate to use")
            print("--test: no review found in Frigate to use")
            sys.exit(1)
        print(f"--test using review {review_id} -> {SMOKETEST_JID}")
    else:
        review_id = sys.argv[1].strip()
    gif_path = os.path.join(GIF_DIR, f"frigate_{review_id}.gif")

    log.info("Fetching review %s ...", review_id)
    review = {}
    title = summary = scene = ""
    threat_level = 0
    other_concerns = None
    poll_attempts = 1 if test_mode else POLL_ATTEMPTS  # no long waits under HA's 60 s cap
    for attempt in range(1, poll_attempts + 1):
        try:
            review = fetch_review(base, review_id)
            title, summary, scene, threat_level, other_concerns = extract_genai(review)
            if has_genai(review):
                log.info("GenAI content ready (attempt %d)", attempt)
                break
        except requests.RequestException as exc:
            log.warning("Attempt %d — Frigate API error: %s", attempt, exc)

        if attempt < poll_attempts:
            log.info("No GenAI content yet — waiting %ds (attempt %d/%d)",
                     POLL_INTERVAL, attempt, poll_attempts)
            time.sleep(POLL_INTERVAL)

    if not has_genai(review):
        log.warning("GenAI not available after polling — sending without it")

    # Per-camera destination + ARA presence gate
    camera    = review.get("camera", "")
    test_note = ""
    if camera in CEUAZUL_CAMERAS:
        ALLOWED_JID = CEUAZUL_JID
        present, who = phone_online()
        if present and not test_mode:
            log.info("Suppressed — phone online at the obra (%s); review %s (camera %s) not sent",
                     who, review_id, camera)
            return
        if test_mode:
            gate = f"suprimiria ({who} online)" if present else "enviaria (rede do canteiro vazia)"
            test_note = f"destino real: Casa Céu Azul · gate agora: {gate}"
    else:
        ALLOWED_JID = DEFAULT_JID
        if test_mode:
            test_note = "destino real: Casa Blumenau (câmera fora da obra, sem gate)"
    if test_mode:
        ALLOWED_JID = SMOKETEST_JID

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
    if test_mode:
        message = f"[TESTE] {message}\n\n_{test_note}_"
    log.info("Sending to %s (%d chars)", ALLOWED_JID, len(message))

    ok, detail = send_text(client, ALLOWED_JID, message)
    if not ok:
        log.error("WhatsApp sendText error: %s", detail)
        sys.exit(1)

    try_send_gif(client, ALLOWED_JID, gif_data, review_id)

    log.info("Done — review %s", review_id)


if __name__ == "__main__":
    main()
