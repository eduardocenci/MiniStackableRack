#!/usr/bin/env python3
"""waha-listener — generic WhatsApp event platform for the shared bnu WAHA.

Receives WAHA webhook events (``message.any``), archives messages + media per
chat, and runs a small rule engine (rules.yaml): each rule filters by chat /
sender / regex pattern and, on match, queues a pending job on disk and
notifies a trigger URL (e.g. the PC endpoint that spawns a headless Claude
run). Consumers then read the archive through the HTTP API below.

Finance ("Casa, financeiro") is the first rule — future WhatsApp automations
add a rule in rules.yaml, not a new service. No LLM in here — plumbing only.

Runs next to WAHA on LXC 101 (bnu-proxmox), docker network ``waha_default``,
port 8000 in-network (published on the LXC as 8788).

Media durability. WAHA writes each received file to its own disk, puts
``media.url`` in the event and deletes the file WHATSAPP_FILES_LIFETIME
(default 180 s) later. That URL names WAHA as WAHA sees itself
(``http://localhost:3000/api/files/…``) — inside this container localhost
is the listener, which is why every download failed with "connection
refused" until 2026-10-03. So: (1) at webhook time the file is pulled with
the host rewritten to WAHA_BASE_URL; (2) when that copy is missing or gone,
WAHA's messages API is asked to download the media from WhatsApp again
(``?downloadMedia=true`` — works while WhatsApp's CDN keeps the file, up to
about four weeks, per file; then it answers 403/404/410); retries back off
over ~4 min; (3) a repair pass (startup + every MEDIA_REPAIR_HOURS)
re-tries any archived media of the last MEDIA_BACKFILL_DAYS still missing;
(4) GET /media/{id} makes one on-demand try before answering 404.

Env:
  WAHA_BASE_URL        e.g. http://waha:3000 (docker-network name)
  WAHA_API_KEY         X-Api-Key for WAHA's files + messages API
  WAHA_SESSION         default "default" (a webhook event's own session wins)
  LISTENER_TOKEN       shared secret: X-Listener-Token header on read APIs,
                       ?token= on /webhook (WAHA's env-configured hook URL)
  RULES_FILE           default /config/rules.yaml
  DATA_DIR             default /data
  MEDIA_BACKFILL_DAYS  repair window, default 30 (≈ WhatsApp CDN retention)
  MEDIA_REPAIR_HOURS   repair pass period, default 6; 0 = no repair thread
  MEDIA_MAX_PASSES     failed passes before a media is left alone, default 3

Data layout (volume):
  /data/chats/<jid>/messages.jsonl    one line per archived message
  /data/media/<msg_id>.<ext>          media file, named by message id
  /data/media/_meta/<msg_id>.json     sidecar: served filename, mimetype,
                                      size, sha256, source, saved_utc
  /data/media/_failed/<msg_id>.json   not archived yet: passes, last error
  /data/pending/<rule>/<job>.json     queued jobs (wake-word matches)
  /data/processed/<rule>/<job>.json   acked jobs (moved on /ack)
"""
import hashlib
import json
import logging
import mimetypes
import os
import re
import threading
import time
import unicodedata
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)    # one INFO line per request otherwise
log = logging.getLogger("waha-listener")

WAHA_BASE_URL = os.environ.get("WAHA_BASE_URL", "http://waha:3000").rstrip("/")
WAHA_API_KEY = os.environ.get("WAHA_API_KEY", "")
WAHA_SESSION = os.environ.get("WAHA_SESSION", "default")
LISTENER_TOKEN = os.environ.get("LISTENER_TOKEN", "")
RULES_FILE = Path(os.environ.get("RULES_FILE", "/config/rules.yaml"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MEDIA_BACKFILL_DAYS = float(os.environ.get("MEDIA_BACKFILL_DAYS", "30"))
MEDIA_REPAIR_HOURS = float(os.environ.get("MEDIA_REPAIR_HOURS", "6"))
MEDIA_MAX_PASSES = int(os.environ.get("MEDIA_MAX_PASSES", "3"))

_JID_RE = re.compile(r"^[A-Za-z0-9@._-]+$")


class _AccessLogFilter(logging.Filter):
    """uvicorn access log: mask the webhook ?token= and drop the dashboard's
    20-second /health polls, which buried every media line in the logs."""
    _token = re.compile(r"token=[^&\s]+")

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            if args[2] == "/health" and args[4] == 200:
                return False
            if "token=" in str(args[2]):
                record.args = args[:2] + (self._token.sub("token=***", str(args[2])),) + args[3:]
        return True


logging.getLogger("uvicorn.access").addFilter(_AccessLogFilter())


@asynccontextmanager
async def lifespan(_app: FastAPI):
    start_media_repair()
    yield


app = FastAPI(title="waha-listener", docs_url=None, redoc_url=None, lifespan=lifespan)


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


def iter_records(since_ts: float = 0.0):
    """Every archived message (all chats) with ts >= since_ts."""
    base = DATA_DIR / "chats"
    if not base.exists():
        return
    for chat_dir in sorted(base.iterdir()):
        path = chat_dir / "messages.jsonl"
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("ts", 0) >= since_ts:
                    yield rec


def find_record(msg_id: str) -> dict:
    return next((r for r in iter_records() if r.get("id") == msg_id), {})


# ---------------------------------------------------------------- media ----
MEDIA_DIR = DATA_DIR / "media"
META_DIR = MEDIA_DIR / "_meta"
FAILED_DIR = MEDIA_DIR / "_failed"
TMP_DIR = MEDIA_DIR / "_tmp"
# Webhook-time schedule (seconds before each try). Try 1 reads the copy WAHA
# made on arrival; the rest ask WAHA to download the file from WhatsApp again.
MEDIA_RETRY_DELAYS = (0, 3, 10, 30, 60, 120)
# WAHA names its files <key-id>.<ext>; keep its extensions so served names
# match the ones the consumers already filed (…jpeg, …oga).
_EXT_BY_MIME = {"image/jpeg": ".jpeg", "image/png": ".png", "image/webp": ".webp",
                "audio/ogg": ".oga", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
                "video/mp4": ".mp4", "application/pdf": ".pdf"}

_media_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="media")
_inflight: set[str] = set()
_inflight_lock = threading.Lock()
_backfill_lock = threading.Lock()
media_stats = {"saved": 0, "failed": 0, "last_saved_utc": None, "last_error": None}
backfill_status: dict = {"running": False, "last": None}


class MediaError(RuntimeError):
    """Media not fetched this time — the retry schedule tries again."""


class MediaGone(MediaError):
    """WhatsApp's CDN no longer has the file — retrying this pass is pointless."""


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json(path: Path) -> dict:
    """A sidecar / failure record; {} when missing or half-written."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def waha_url(url: str) -> str:
    """WAHA's media.url → a URL reachable from this container: WAHA has no
    WAHA_BASE_URL of its own, so its URLs say ``http://localhost:3000/…``."""
    parts = urllib.parse.urlsplit(url)
    if (parts.path.startswith("/api/files/")
            or parts.hostname in ("localhost", "127.0.0.1", "0.0.0.0")):
        return WAHA_BASE_URL + parts.path + (f"?{parts.query}" if parts.query else "")
    return url


def clean_filename(name: str | None) -> str:
    """Sender-supplied file name → a safe basename ('' when unusable)."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(c for c in name if c.isprintable()).strip().strip(".")[:200]


def media_ext(filename: str, url_path: str, mimetype: str) -> str:
    for cand in (filename, url_path):
        ext = os.path.splitext(cand or "")[1].lower()
        if 1 < len(ext) <= 8 and ext[1:].isalnum():
            return ext
    mime = (mimetype or "").split(";")[0].strip().lower()
    return _EXT_BY_MIME.get(mime) or mimetypes.guess_extension(mime) or ".bin"


def _id_parts(msg_id: str) -> list[str]:
    """``false_<chat>_<key>[_<participant>]`` → its parts ([] for other forms)."""
    parts = msg_id.split("_")
    return parts if len(parts) >= 3 and parts[0] in ("true", "false") else []


def key_id(msg_id: str) -> str:
    """WhatsApp's message key — WAHA's file stem (``3EB0E139218F604DFF854A``)."""
    parts = _id_parts(msg_id)
    return parts[2] if parts else msg_id


def stored_media(msg_id: str) -> tuple[Path, dict] | None:
    """(file, sidecar) of an archived media, or None."""
    meta = _read_json(META_DIR / f"{msg_id}.json")
    if meta.get("file") and (MEDIA_DIR / meta["file"]).is_file():
        return MEDIA_DIR / meta["file"], meta
    for path in MEDIA_DIR.glob(f"{msg_id}.*"):         # file without sidecar
        if path.is_file():
            return path, {}
    return None


def _waha_error(err) -> str:
    """WAHA's media.error (a Boom object) → '403 Forbidden: Failed to fetch…'."""
    if isinstance(err, dict):
        out = err.get("output") or {}
        payload = out.get("payload") or {}
        code = out.get("statusCode") or payload.get("statusCode") or ""
        message = payload.get("message") or err.get("message") or ""
        text = f"{code} {payload.get('error') or ''}: {message}"
        return text.split(" from https://")[0][:200]
    return json.dumps(err)[:200]


def waha_media(client: httpx.Client, session: str, chats: list[str], msg_id: str) -> dict:
    """Ask WAHA to download one message's media from WhatsApp again →
    its ``media`` dict (url, mimetype, filename)."""
    last = "message not found on WAHA"
    for chat in chats:
        r = client.get(
            f"{WAHA_BASE_URL}/api/{session}/chats/{urllib.parse.quote(chat, safe='')}"
            f"/messages/{urllib.parse.quote(msg_id, safe='')}",
            params={"downloadMedia": "true"}, headers={"X-Api-Key": WAHA_API_KEY}, timeout=180)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        msg = r.json() if r.content else None
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        if not msg:
            continue
        media = msg.get("media") or {}
        if media.get("url"):
            return media
        err = media.get("error")
        last = f"WAHA has no media.url ({_waha_error(err) if err else 'no error given'})"
        if isinstance(err, dict) and (err.get("output") or {}).get("statusCode") in (403, 404, 410):
            raise MediaGone(last)
    raise MediaError(last)


def save_media(client: httpx.Client, msg_id: str, chat: str, media: dict, source: str) -> dict:
    """Stream WAHA's file to data/media/<msg_id>.<ext> + its sidecar."""
    url = waha_url(media["url"])
    url_path = urllib.parse.urlsplit(url).path
    filename = clean_filename(media.get("filename"))
    ext = media_ext(filename, url_path, media.get("mimetype") or "")
    shown = filename or clean_filename(os.path.basename(url_path)) or key_id(msg_id) + ext
    if not os.path.splitext(shown)[1]:
        shown += ext
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TMP_DIR / f"{uuid.uuid4().hex}{ext}"
    digest, size, ctype = hashlib.sha256(), 0, ""
    dest = MEDIA_DIR / f"{msg_id}{ext}"
    try:
        with client.stream("GET", url, headers={"X-Api-Key": WAHA_API_KEY}, timeout=300) as r:
            if r.status_code == 404:
                raise MediaError(f"WAHA file gone (404 {url_path})")
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            with tmp.open("wb") as fh:
                for chunk in r.iter_bytes(1 << 16):
                    fh.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        if not size:
            raise MediaError("WAHA served an empty file")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    meta = {"msg_id": msg_id, "chat": chat, "file": dest.name, "filename": shown,
            "mimetype": media.get("mimetype") or ctype, "size": size,
            "sha256": digest.hexdigest(), "source": source, "saved_utc": _utc()}
    META_DIR.mkdir(parents=True, exist_ok=True)
    (META_DIR / f"{msg_id}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    (FAILED_DIR / f"{msg_id}.json").unlink(missing_ok=True)
    return meta


def _failed_passes(msg_id: str) -> int:
    return _read_json(FAILED_DIR / f"{msg_id}.json").get("passes", 0)


def _record_failure(msg_id: str, chat: str, err: Exception | None) -> None:
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    (FAILED_DIR / f"{msg_id}.json").write_text(json.dumps(
        {"msg_id": msg_id, "chat": chat, "passes": _failed_passes(msg_id) + 1,
         "last_error": str(err)[:300], "last_try_utc": _utc()}, ensure_ascii=False, indent=1),
        encoding="utf-8")


def archive_media(msg_id: str, chat: str, media: dict | None = None, session: str = "",
                  raw_chat: str = "", delays: tuple | None = None, source: str = "webhook") -> str:
    """Persist one message's media → 'saved' | 'exists' | 'busy' | 'failed'."""
    media = dict(media or {})
    delays = delays or MEDIA_RETRY_DELAYS
    if stored_media(msg_id):
        return "exists"
    with _inflight_lock:
        if msg_id in _inflight:
            return "busy"
        _inflight.add(msg_id)
    # the message is looked up by the archive's canonical chat, the event's
    # raw `from` (lid form) and the chat embedded in the id, in that order
    embedded = (_id_parts(msg_id) or ["", ""])[1]
    chats = list(dict.fromkeys(c for c in (chat, raw_chat, embedded) if c))
    last: Exception | None = None
    try:
        with httpx.Client() as client:
            for attempt, delay in enumerate(delays, 1):
                time.sleep(delay)
                try:
                    if attempt > 1 or not media.get("url"):
                        fresh = waha_media(client, session or WAHA_SESSION, chats, msg_id)
                        media = {**fresh,
                                 "filename": fresh.get("filename") or media.get("filename"),
                                 "mimetype": fresh.get("mimetype") or media.get("mimetype")}
                    meta = save_media(client, msg_id, chat, media, source)
                    with _inflight_lock:
                        media_stats["saved"] += 1
                        media_stats["last_saved_utc"] = meta["saved_utc"]
                    log.info("media %s → %s (%d bytes, %s, %s)", msg_id, meta["file"],
                             meta["size"], meta["filename"], source)
                    return "saved"
                except MediaGone as exc:
                    last = exc
                    break
                except Exception as exc:  # noqa: BLE001 — retried, then recorded
                    last = exc
                    if attempt < len(delays):
                        log.info("media %s try %d/%d failed (%s) — retrying in %ss",
                                 msg_id, attempt, len(delays), exc, delays[attempt])
        _record_failure(msg_id, chat, last)
        with _inflight_lock:
            media_stats["failed"] += 1
            media_stats["last_error"] = f"{_utc()} {msg_id}: {last}"[:400]
        log.warning("media %s NOT archived (%s, %d tries): %s", msg_id, source, attempt, last)
        return "failed"
    except Exception:  # noqa: BLE001 — a pool thread must never die silently
        log.exception("media %s: unexpected error", msg_id)
        return "failed"
    finally:
        with _inflight_lock:
            _inflight.discard(msg_id)


def backfill_media(days: float, force: bool = False) -> dict:
    """Fetch every archived media of the last `days` days still missing."""
    counts = {"days": days, "checked": 0, "exists": 0, "saved": 0, "failed": 0,
              "busy": 0, "gave_up": 0}
    seen: set[str] = set()
    for rec in iter_records(time.time() - days * 86400):
        if not rec.get("has_media") or rec["id"] in seen:
            continue
        seen.add(rec["id"])
        counts["checked"] += 1
        if stored_media(rec["id"]):
            counts["exists"] += 1
            continue
        if not force and _failed_passes(rec["id"]) >= MEDIA_MAX_PASSES:
            counts["gave_up"] += 1
            continue
        hint = {"filename": rec.get("media_name"), "mimetype": rec.get("media_mime")}
        counts[archive_media(rec["id"], rec["chat"], hint, delays=(0, 5), source="backfill")] += 1
    return counts


def run_backfill(days: float, force: bool = False) -> bool:
    """One backfill pass unless one is already running (then False)."""
    if not _backfill_lock.acquire(blocking=False):
        return False
    try:
        backfill_status.update(running=True, started_utc=_utc(), days=days, force=force)
        counts = backfill_media(days, force)
        backfill_status["last"] = {**counts, "finished_utc": _utc()}
        log.info("media backfill pass: %s", counts)
    except Exception:  # noqa: BLE001
        log.exception("media backfill pass crashed")
    finally:
        backfill_status["running"] = False
        _backfill_lock.release()
    return True


def _repair_loop() -> None:
    time.sleep(30)                      # let WAHA settle after a joint restart
    while True:
        run_backfill(MEDIA_BACKFILL_DAYS)
        time.sleep(MEDIA_REPAIR_HOURS * 3600)


def start_media_repair() -> None:
    for leftover in TMP_DIR.glob("*"):
        leftover.unlink(missing_ok=True)
    if MEDIA_REPAIR_HOURS > 0:
        threading.Thread(target=_repair_loop, name="media-repair", daemon=True).start()


def content_disposition(name: str) -> str:
    """RFC 6266: the UTF-8 name in filename*, then an ASCII fallback LAST —
    pre-2026-10-03 clients take whatever follows the final 'filename='."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    fallback = "".join(c if c.isprintable() and c not in '"\\' else "_" for c in folded)
    return (f"attachment; filename*=UTF-8''{urllib.parse.quote(name, safe='')}; "
            f'filename="{fallback}"')


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
async def webhook(request: Request, token: str = Query(default="")):
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
        # own pool, not BackgroundTasks: the retry schedule sleeps for minutes
        # and must not hold the threadpool the sync endpoints run on
        _media_pool.submit(archive_media, rec["id"], chat, media,
                           event.get("session") or "", p.get("from") or "")

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
    return {"ok": True, "rules": [r["name"] for r in load_rules()["rules"]],
            "media": media_stats}


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


@app.get("/media", dependencies=[Depends(require_token)])
def media_index():
    """Archive health: files stored, media not archived yet, repair passes."""
    stored = sum(1 for p in MEDIA_DIR.glob("*") if p.is_file()) if MEDIA_DIR.exists() else 0
    failed = ([_read_json(p) for p in sorted(FAILED_DIR.glob("*.json"))]
              if FAILED_DIR.exists() else [])
    return {"stored": stored, "failed": failed, "stats": media_stats, "backfill": backfill_status}


@app.post("/media/backfill", dependencies=[Depends(require_token)])
def media_backfill(days: float = Query(default=MEDIA_BACKFILL_DAYS, gt=0, le=400),
                   force: bool = False):
    """Start a repair pass now (background) — e.g. after an outage."""
    if backfill_status["running"]:
        raise HTTPException(409, "a backfill pass is already running")
    threading.Thread(target=run_backfill, args=(days, force), daemon=True).start()
    return {"ok": True, "started": True, "days": days, "force": force}


@app.get("/media/{msg_id}", dependencies=[Depends(require_token)])
def media(msg_id: str):
    found = stored_media(safe_jid(msg_id))
    if not found:
        # Not archived (yet): one on-demand try through WAHA before the 404.
        rec = find_record(msg_id)
        hint = {"filename": rec.get("media_name"), "mimetype": rec.get("media_mime")}
        result = archive_media(msg_id, rec.get("chat") or "", hint, delays=(0,),
                               source="on-demand")
        found = stored_media(msg_id)
        if not found:
            why = _read_json(FAILED_DIR / f"{msg_id}.json").get("last_error")
            detail = f"{result}: {why}" if why else result
            raise HTTPException(404, f"no media for that message id ({detail})")
    path, meta = found
    disposition = content_disposition(meta.get("filename") or path.name)
    return FileResponse(path, media_type=meta.get("mimetype") or None,
                        headers={"Content-Disposition": disposition})


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
