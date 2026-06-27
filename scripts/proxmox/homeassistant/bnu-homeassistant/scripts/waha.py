#!/usr/bin/env python3
"""Minimal WAHA (WhatsApp HTTP API) client — deploy to /config/scripts/waha.py.

Shared by frigate_digest.py and frigate_whatsapp.py. WAHA replaced the Evolution
API for the bnu region; it runs in LXC 101 "docker" at 10.1.1.126:3000 with the
NOWEB engine and a persisted session, so a dropped connection auto-reconnects
instead of dying silently (the failure mode that plagued Evolution).

Config keys (secrets.yaml):
  waha_api_url   e.g. http://10.1.1.126:3000
  waha_api_key   value sent in the X-Api-Key header
  waha_session   session name (default: "default")

For groups the chatId IS the WhatsApp JID "<id>@g.us" — the same value Evolution
took as `number`, so existing JIDs migrate unchanged. Every send retries with
linear backoff and returns (ok, detail) so callers can surface what went wrong.
"""
import logging
import time

import requests

log = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 3


class WahaClient:
    def __init__(self, secrets: dict, attempts: int = DEFAULT_ATTEMPTS):
        self.base     = secrets["waha_api_url"].rstrip("/")
        self.key      = secrets["waha_api_key"]
        self.session  = secrets.get("waha_session", "default")
        self.attempts = attempts

    @property
    def _headers(self) -> dict:
        return {"X-Api-Key": self.key, "Content-Type": "application/json"}

    def _post(self, path: str, payload: dict, timeout: int, what: str) -> tuple[bool, str]:
        last = "no attempt"
        for i in range(1, self.attempts + 1):
            try:
                r = requests.post(f"{self.base}{path}", json=payload,
                                  headers=self._headers, timeout=timeout)
                if r.status_code >= 400:
                    last = f"HTTP {r.status_code}: {r.text[:160]}"
                    log.warning("%s attempt %d/%d → %s", what, i, self.attempts, last)
                else:
                    return True, f"HTTP {r.status_code}"
            except requests.RequestException as exc:
                last = f"{type(exc).__name__}: {exc}"
                log.warning("%s attempt %d/%d → %s", what, i, self.attempts, last)
            if i < self.attempts:
                time.sleep(2 * i)
        return False, last

    def send_text(self, chat_id: str, text: str, timeout: int = 30) -> tuple[bool, str]:
        return self._post(
            "/api/sendText",
            {"session": self.session, "chatId": chat_id, "text": text},
            timeout, "WAHA sendText",
        )

    def send_video_url(self, chat_id: str, url: str, filename: str,
                       caption: str = "", timeout: int = 120) -> tuple[bool, str]:
        """Send a video the server fetches from `url` (avoids a large base64 payload)."""
        return self._post(
            "/api/sendVideo",
            {"session": self.session, "chatId": chat_id, "caption": caption, "convert": False,
             "file": {"mimetype": "video/mp4", "filename": filename, "url": url}},
            timeout, "WAHA sendVideo(url)",
        )

    def send_video_b64(self, chat_id: str, data_b64: str, filename: str,
                       caption: str = "", timeout: int = 60) -> tuple[bool, str]:
        """Send a video from inline base64 data (for small clips already in memory)."""
        return self._post(
            "/api/sendVideo",
            {"session": self.session, "chatId": chat_id, "caption": caption, "convert": False,
             "file": {"mimetype": "video/mp4", "filename": filename, "data": data_b64}},
            timeout, "WAHA sendVideo(b64)",
        )

    def send_image_b64(self, chat_id: str, data_b64: str, filename: str,
                       caption: str = "", timeout: int = 60) -> tuple[bool, str]:
        """Send an image from inline base64 data (used for debug snapshots)."""
        return self._post(
            "/api/sendImage",
            {"session": self.session, "chatId": chat_id, "caption": caption,
             "file": {"mimetype": "image/jpeg", "filename": filename, "data": data_b64}},
            timeout, "WAHA sendImage(b64)",
        )

    def session_status(self, timeout: int = 10) -> str:
        """Session status: WORKING / SCAN_QR_CODE / STARTING / STOPPED / FAILED, '' on error."""
        try:
            r = requests.get(f"{self.base}/api/sessions/{self.session}",
                            headers=self._headers, timeout=timeout)
            if r.ok:
                return (r.json() or {}).get("status", "")
        except requests.RequestException as exc:
            log.warning("WAHA session_status failed: %s", exc)
        return ""
