#!/usr/bin/env python3
"""waha-listener — generic WhatsApp event platform for the shared bnu WAHA.

Receives WAHA webhook events (``message``), archives messages + media per
chat, and runs a small rule engine (rules.yaml): each rule filters by chat /
sender / regex pattern and, on match, queues a pending job on disk and
notifies a trigger URL (e.g. the PC endpoint that spawns a headless Claude
run). Consumers then read the archive through the HTTP API below.

Finance ("Casa, financeiro") is the first rule — future WhatsApp automations
add a rule in rules.yaml, not a new service. No LLM in here — plumbing only.

Runs next to WAHA on LXC 101 (bnu-proxmox), docker network ``waha_default``,
port 8000 in-network (published on the LXC as 8788).

Env:
  WAHA_BASE_URL   e.g. http://waha:3000 (docker-network name)
  WAHA_API_KEY    X-Api-Key, used when fetching media files from WAHA
  LISTENER_TOKEN  shared secret: X-Listener-Token header on read APIs,
                  ?token= on /webhook (WAHA's env-configured hook URL)
  RULES_FILE      default /config/rules.yaml
  DATA_DIR        default /data

Data layout (volume):
  /data/chats/<jid>/messages.jsonl   one line per archived message
  /data/media/<msg_id>.<ext>         downloaded media, named by message id
  /data/pending/<rule>/<job>.json    queued jobs (wake-word matches)
  /data/processed/<rule>/<job>.json  acked jobs (moved on /ack)
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

import httpx
import yaml
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("waha-listener")

WAHA_BASE_URL = os.environ.get("WAHA_BASE_URL", "http://waha:3000").rstrip("/")
WAHA_API_KEY = os.environ.get("WAHA_API_KEY", "")
LISTENER_TOKEN = os.environ.get("LISTENER_TOKEN", "")
RULES_FILE = Path(os.environ.get("RULES_FILE", "/config/rules.yaml"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

_JID_RE = re.compile(r"^[A-Za-z0-9@._-]+$")

app = FastAPI(title="waha-listener", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------- rules ----
def load_rules() -> dict:
    """rules.yaml with ${ENV} expansion; reloaded on every webhook (cheap,
    keeps `docker compose restart` unnecessary for rule tweaks)."""
    try:
        raw = os.path.expandvars(RULES_FILE.read_text())
        cfg = yaml.safe_load(raw) or {}
    except FileNotFoundError:
        log.warning("rules file %s missing — archiving nothing", RULES_FILE)
        return {"archive_chats": [], "rules": []}
    cfg.setdefault("archive_chats", [])
    cfg.setdefault("rules", [])
    return cfg


def archived_chats(cfg: dict) -> set[str] | None:
    """Chats to archive. None means archive everything ('*')."""
    explicit = set(cfg["archive_chats"])
    if "*" in explicit:
        return None
    return explicit | {r["chat"] for r in cfg["rules"] if r.get("chat")}


# ----------------------------------------------------------------- auth ----
def require_token(request: Request) -> None:
    if not LISTENER_TOKEN:
        raise HTTPException(500, "LISTENER_TOKEN not configured")
    if request.headers.get("X-Listener-Token") != LISTENER_TOKEN:
        raise HTTPException(401, "bad or missing X-Listener-Token")


def safe_jid(jid: str) -> str:
    if not _JID_RE.match(jid or ""):
        raise HTTPException(400, "bad jid")
    return jid


# ---------------------------------------------------------------- store ----
def append_message(rec: dict) -> None:
    chat_dir = DATA_DIR / "chats" / safe_jid(rec["chat"])
    chat_dir.mkdir(parents=True, exist_ok=True)
    with (chat_dir / "messages.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def download_media(msg_id: str, media: dict) -> None:
    """Fetch the media file WAHA exposes for this message (runs in background)."""
    url = media.get("url")
    if not url:
        log.warning("message %s hasMedia but no media.url (WAHA Core limit?)", msg_id)
        return
    ext = os.path.splitext(media.get("filename") or "")[1]
    if not ext:
        mime = (media.get("mimetype") or "").split(";")[0]
        ext = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png",
               "video/mp4": ".mp4", "audio/ogg": ".ogg"}.get(mime, ".bin")
    dest = DATA_DIR / "media" / f"{msg_id}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return
    try:
        with httpx.Client(timeout=60) as client:
            r = client.get(url, headers={"X-Api-Key": WAHA_API_KEY})
            r.raise_for_status()
            dest.write_bytes(r.content)
        log.info("media %s → %s (%d bytes)", msg_id, dest.name, dest.stat().st_size)
    except Exception as exc:  # noqa: BLE001 — archive must survive bad media
        log.warning("media download failed for %s: %s", msg_id, exc)


def notify(rule_name: str, job: dict, notify_url: str) -> None:
    """Fire-and-forget trigger ping, 3 linear-backoff attempts. The pending
    job file stays either way — the scheduled fallback sweeps it."""
    for i in range(1, 4):
        try:
            r = httpx.post(notify_url, json=job, timeout=10,
                           headers={"X-Listener-Token": LISTENER_TOKEN})
            log.info("notify %s attempt %d → HTTP %s", rule_name, i, r.status_code)
            if r.status_code < 400:
                return
        except Exception as exc:  # noqa: BLE001
            log.warning("notify %s attempt %d failed: %s", rule_name, i, exc)
        time.sleep(2 * i)


def queue_job(rule: dict, rec: dict, mode: str) -> dict:
    job = {
        "job_id": f"{int(rec['ts'])}-{uuid.uuid4().hex[:6]}",
        "rule": rule["name"],
        "mode": mode,
        "chat": rec["chat"],
        "message_id": rec["id"],
        "body": rec["body"],
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    pending = DATA_DIR / "pending" / rule["name"]
    pending.mkdir(parents=True, exist_ok=True)
    (pending / f"{job['job_id']}.json").write_text(
        json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("queued job %s rule=%s mode=%s", job["job_id"], rule["name"], mode)
    if rule.get("notify_url"):
        threading.Thread(target=notify, args=(rule["name"], job, rule["notify_url"]),
                         daemon=True).start()
    return job


# -------------------------------------------------------------- webhook ----
@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks,
                  token: str = Query(default="")):
    if not LISTENER_TOKEN or token != LISTENER_TOKEN:
        raise HTTPException(401, "bad or missing ?token=")
    event = await request.json()
    # 2026.7.2 moved fromMe (wake words are fromMe — the session is Eduardo's
    # own account) off the plain "message" event; the hook now subscribes
    # message.any, and older "message" events stay accepted.
    if event.get("event") not in ("message", "message.any"):
        return {"ok": True, "ignored": event.get("event")}
    p = event.get("payload") or {}
    chat = p.get("from") or ""
    # LID rollout (2026-08-02): lid-addressed chats may arrive with a lid-form
    # `from`; the raw Baileys key carries the phone-number alias — prefer the
    # @g.us/@c.us form so rules.yaml keeps matching one canonical id.
    key = (p.get("_data") or {}).get("key") or {}
    alt = key.get("remoteJidAlt") or ""
    if chat.endswith("@lid") and (alt.endswith("@g.us") or alt.endswith("@c.us")
                                  or alt.endswith("@s.whatsapp.net")):
        chat = alt.replace("@s.whatsapp.net", "@c.us")
    cfg = load_rules()
    wanted = archived_chats(cfg)
    if wanted is not None and chat not in wanted:
        # Silent drops hid the 2026-08-02 LID addressing break for 3 days —
        # always log the rejected chat id and raw routing key (ids, no body).
        log.info("ignored chat %r key=%s", chat,
                 json.dumps((p.get("_data") or {}).get("key") or {})[:300])
        return {"ok": True, "ignored": "chat not archived"}

    media = p.get("media") or {}
    rec = {
        "id": str(p.get("id") or uuid.uuid4().hex),
        "ts": float(p.get("timestamp") or time.time()),
        "chat": chat,
        "participant": p.get("participant") or "",
        "push_name": p.get("pushName") or (p.get("_data") or {}).get("pushName") or "",
        "from_me": bool(p.get("fromMe")),
        "body": p.get("body") or p.get("caption") or "",
        "has_media": bool(p.get("hasMedia")),
        "media_mime": media.get("mimetype") or "",
        "media_name": media.get("filename") or "",
    }
    append_message(rec)
    if rec["has_media"]:
        background.add_task(download_media, rec["id"], media)

    fired = []
    for rule in cfg["rules"]:
        if rule.get("chat") and rule["chat"] != chat:
            continue
        if rule.get("from_me") and not rec["from_me"]:
            continue
        if rule.get("participant") and rule["participant"] != rec["participant"]:
            continue
        pattern = rule.get("pattern")
        if pattern and not re.search(pattern, rec["body"], re.IGNORECASE):
            continue
        mode = "default"
        for name, extra in (rule.get("modes") or {}).items():
            if re.search(extra, rec["body"], re.IGNORECASE):
                mode = name
                break
        fired.append(queue_job(rule, rec, mode)["job_id"])
    return {"ok": True, "archived": rec["id"], "jobs": fired}


# ------------------------------------------------------------- read API ----
@app.get("/health")
def health():
    return {"ok": True, "rules": [r["name"] for r in load_rules()["rules"]]}


@app.get("/pending", dependencies=[Depends(require_token)])
def pending(rule: str = Query(default="")):
    base = DATA_DIR / "pending"
    jobs = []
    for f in sorted(base.glob(f"{rule or '*'}/*.json")):
        jobs.append(json.loads(f.read_text(encoding="utf-8")))
    return {"jobs": jobs}


@app.post("/ack/{rule}/{job_id}", dependencies=[Depends(require_token)])
def ack(rule: str, job_id: str):
    src = DATA_DIR / "pending" / safe_jid(rule) / f"{safe_jid(job_id)}.json"
    if not src.exists():
        raise HTTPException(404, "no such pending job")
    dest = DATA_DIR / "processed" / rule
    dest.mkdir(parents=True, exist_ok=True)
    src.rename(dest / src.name)
    return {"ok": True}


@app.get("/messages", dependencies=[Depends(require_token)])
def messages(chat: str, since: float = 0.0, limit: int = 200):
    path = DATA_DIR / "chats" / safe_jid(chat) / "messages.jsonl"
    if not path.exists():
        return {"messages": []}
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ts", 0) > since:
                out.append(rec)
    return {"messages": out[-limit:]}


@app.get("/media/{msg_id}", dependencies=[Depends(require_token)])
def media(msg_id: str):
    matches = list((DATA_DIR / "media").glob(f"{safe_jid(msg_id)}.*"))
    if not matches:
        raise HTTPException(404, "no media for that message id")
    return FileResponse(matches[0])


@app.get("/chats", dependencies=[Depends(require_token)])
def chats():
    base = DATA_DIR / "chats"
    if not base.exists():
        return {"chats": []}
    return {"chats": sorted(p.name for p in base.iterdir() if p.is_dir())}


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception):
    log.exception("unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})
