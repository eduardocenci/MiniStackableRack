#!/usr/bin/env python3
"""condfy-bridge — Condfy gate events → SQLite → MQTT (Home Assistant) + WhatsApp.

Runs in LXC 101 "docker" on bnu-proxmox, next to WAHA. Polls the Condfy account
notification feed, stores every event in SQLite (the portal keeps only a ~4-day
rolling window), publishes all of them to MQTT for Home Assistant, and sends one
WhatsApp message to the ARA house group when a watched person passes a gate.

Never writes back to Condfy: notifications are never marked read, so the phone
app's own unread state is untouched.

  python app.py             daemon (default)
  python app.py --once      a single poll, verbose, never alerts
  python app.py --selftest  one canned WhatsApp message, then exit
  python app.py --report-dry            build last week's presence report, save only
  python app.py --report-test [chatId]  build it and send to chatId (default REPORT_GROUP_JID)
"""
import base64
import json
import logging
import os
import random
import re
import signal
import sqlite3
import sys
import time
import uuid
from datetime import datetime

import requests

from condfy import (ACCESS_TYPE, TZ, CondfyClient, LoginError, matches_watch,
                    parse_event_date, parse_message, slug)
from mqtt_bridge import T_LAST_EVENT, MqttBridge
from report import last_fire, parse_dow, render_image, report_caption, week_bounds, week_data
from waha import WahaClient

VERSION = "1.1.0"
log = logging.getLogger("condfy-bridge")

PAGE_SIZE = 15
MAX_PAGES = 5           # catch-up bound after a long outage
JID_RE = re.compile(r"^\d+@(g\.us|c\.us)$")
BACKOFF = [60, 120, 240, 480, 900]


def env(key, default=""):
    return os.environ.get(key, default).strip()


def env_int(key, default):
    try:
        return int(env(key) or default)
    except ValueError:
        return int(default)


CFG = {
    "db_path": env("DB_PATH", "/data/condfy.db"),
    "base_url": env("CONDFY_BASE_URL"),
    "email": env("CONDFY_EMAIL"),
    "password": os.environ.get("CONDFY_PASSWORD", ""),
    "login_path": env("CONDFY_LOGIN_PATH"),
    "bootstrap_token": env("CONDFY_BOOTSTRAP_TOKEN"),
    "license_id": env_int("CONDFY_LICENSE_ID", 9358),
    "poll_seconds": env_int("POLL_SECONDS", 60),
    "watch_names": [n.strip() for n in env("WATCH_NAMES").split(",") if n.strip()],
    "cooldown_min": env_int("ALERT_COOLDOWN_MIN", 10),
    "max_per_hour": env_int("ALERT_MAX_PER_HOUR", 8),
    "max_age_min": env_int("MAX_ALERT_AGE_MIN", 60),
    "group_jid": env("GROUP_JID"),
    "mqtt_host": env("MQTT_HOST", "10.1.1.124"),
    "mqtt_port": env_int("MQTT_PORT", 1883),
    "mqtt_user": env("MQTT_USER"),
    "mqtt_password": os.environ.get("MQTT_PASSWORD", ""),
    "discovery_prefix": env("MQTT_DISCOVERY_PREFIX", "homeassistant"),
    "waha_url": env("WAHA_BASE_URL", "http://waha:3000"),
    "waha_key": os.environ.get("WAHA_API_KEY", ""),
    "waha_session": env("WAHA_SESSION", "default"),
    "report_jid": env("REPORT_GROUP_JID"),
    "report_person": env("REPORT_PERSON", "Enio Faqueti"),
    "report_display": env("REPORT_PERSON_DISPLAY", "Ênio Faqueti"),
    "report_dow": parse_dow(env("REPORT_DOW", "sun")),
    "report_hour": env_int("REPORT_HOUR_LOCAL", 8),
}

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  license_id INTEGER, resource_id INTEGER,
  type_name TEXT NOT NULL, title TEXT, message TEXT NOT NULL,
  raw_date TEXT NOT NULL, ts_local TEXT NOT NULL, ts_utc INTEGER NOT NULL,
  person TEXT, person_key TEXT, gate TEXT, gate_key TEXT, method TEXT,
  parsed INTEGER NOT NULL DEFAULT 0, watched INTEGER NOT NULL DEFAULT 0,
  watch_name TEXT,
  first_seen_utc INTEGER NOT NULL,
  published INTEGER NOT NULL DEFAULT 0, alerted INTEGER NOT NULL DEFAULT 0,
  raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts     ON events(ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_events_person ON events(person_key, ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_events_todo   ON events(published, alerted);
CREATE TABLE IF NOT EXISTS state (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_utc INTEGER NOT NULL
);
"""


def now():
    return int(time.time())


def iso(ts):
    return datetime.fromtimestamp(ts, TZ).isoformat()


# --------------------------------------------------------------------------- #
# state helpers
# --------------------------------------------------------------------------- #
class Ctx:
    def __init__(self, db, client, mqtt=None, waha=None):
        self.db, self.client, self.mqtt, self.waha = db, client, mqtt, waha
        # Set from paho's network thread on (re)connect; acted on by the main
        # loop, because this sqlite3 connection belongs to the main thread only.
        self.needs_republish = True
        self.failures = 0
        self.login_failures = 0
        self.last_http_status = 0
        self.latency_ms = 0
        self.alerts_suppressed = 0


def get_state(db, key, default=None):
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(db, key, value):
    db.execute(
        "INSERT INTO state(key,value,updated_utc) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_utc=excluded.updated_utc",
        (key, str(value), now()),
    )
    db.commit()


# --------------------------------------------------------------------------- #
# ingestion
# --------------------------------------------------------------------------- #
def to_row(item, watch_names):
    """Map one API item to a DB row dict. Never raises on odd input."""
    message = item.get("message") or ""
    person, gate, method = parse_message(message)
    try:
        ts_local, ts_utc = parse_event_date(item.get("date") or "")
    except (ValueError, AttributeError):
        ts_utc = now()
        ts_local = iso(ts_utc)
        log.warning("unparseable date %r on event %s", item.get("date"), item.get("id"))
    watch_name = matches_watch(watch_names, person, message)
    return {
        "id": int(item["id"]),
        "license_id": item.get("licenseId"),
        "resource_id": item.get("resourceId"),
        "type_name": item.get("typeName") or "",
        "title": item.get("title") or "",
        "message": message,
        "raw_date": item.get("date") or "",
        "ts_local": ts_local,
        "ts_utc": ts_utc,
        "person": person,
        "person_key": slug(person) if person else None,
        "gate": gate,
        "gate_key": slug(gate) if gate else None,
        "method": method,
        "parsed": 1 if person else 0,
        "watched": 1 if watch_name else 0,
        "watch_name": watch_name,
        "first_seen_utc": now(),
        "raw_json": json.dumps(item, ensure_ascii=False),
    }


def insert_row(db, row, handled=False):
    """INSERT OR IGNORE — the primary key is the dedup. True if it was new."""
    cur = db.execute(
        "INSERT OR IGNORE INTO events (id,license_id,resource_id,type_name,title,message,"
        "raw_date,ts_local,ts_utc,person,person_key,gate,gate_key,method,parsed,watched,"
        "watch_name,first_seen_utc,published,alerted,raw_json) VALUES "
        "(:id,:license_id,:resource_id,:type_name,:title,:message,:raw_date,:ts_local,"
        ":ts_utc,:person,:person_key,:gate,:gate_key,:method,:parsed,:watched,:watch_name,"
        ":first_seen_utc,:published,:alerted,:raw_json)",
        {**row, "published": 1 if handled else 0, "alerted": 1 if handled else 0},
    )
    return cur.rowcount > 0


def fetch_feed(ctx):
    """Feed items, newest first. Walks pages only while every id is unseen."""
    items, page = [], 0
    while page < MAX_PAGES:
        started = time.monotonic()
        response, data = ctx.client.notifications(page=page, size=PAGE_SIZE)
        ctx.latency_ms = int((time.monotonic() - started) * 1000)
        ctx.last_http_status = response.status_code
        if response.status_code == 429:
            raise RetryAfter(int(response.headers.get("Retry-After") or 300))
        response.raise_for_status()
        content = data.get("content") or []
        items.extend(content)
        ids = [int(c["id"]) for c in content if c.get("id") is not None]
        seen_any = bool(ids) and bool(ctx.db.execute(
            "SELECT 1 FROM events WHERE id IN (%s) LIMIT 1" % ",".join("?" * len(ids)), ids
        ).fetchone())
        if seen_any or data.get("last") or not content:
            break
        page += 1
        log.info("every id on page %d was new — walking back one more page", page - 1)
    return items


class RetryAfter(Exception):
    def __init__(self, seconds):
        super().__init__(f"rate limited, retry in {seconds}s")
        self.seconds = seconds


# --------------------------------------------------------------------------- #
# publishing
# --------------------------------------------------------------------------- #
def event_payload(row):
    return {
        "id": row["id"],
        "event_type": row["gate_key"] or "outro",
        "ts": row["ts_local"],
        "person": row["person"],
        "person_key": row["person_key"],
        "gate": row["gate"],
        "gate_key": row["gate_key"],
        "method": row["method"],
        "watched": bool(row["watched"]),
        "parsed": bool(row["parsed"]),
        "type": row["type_name"],
        "condo": row["title"],
        "resource_id": row["resource_id"],
        "message": row["message"],
    }


def publish_event(ctx, row):
    payload = event_payload(row)
    ctx.mqtt.publish_event(payload)
    if row["person"]:
        ctx.mqtt.publish_person(row["person"], {
            "last_seen": row["ts_local"], "gate": row["gate"], "gate_key": row["gate_key"],
            "method": row["method"], "event_id": row["id"], "message": row["message"],
        })
    if row["gate"]:
        ctx.mqtt.publish_gate(row["gate"], {
            "last_seen": row["ts_local"], "person": row["person"],
            "method": row["method"], "event_id": row["id"],
        })
    ctx.db.execute("UPDATE events SET published=1 WHERE id=?", (row["id"],))
    ctx.db.commit()


def republish_snapshots(ctx):
    """Re-assert retained state after a (re)connect, from SQLite."""
    if not ctx.mqtt:
        return
    newest = ctx.db.execute(
        "SELECT * FROM events WHERE type_name=? ORDER BY ts_utc DESC, id DESC LIMIT 1",
        (ACCESS_TYPE,),
    ).fetchone()
    if newest:
        ctx.mqtt.publish(T_LAST_EVENT, event_payload(newest), retain=True)
    for name in CFG["watch_names"]:
        row = ctx.db.execute(
            "SELECT * FROM events WHERE person_key=? ORDER BY ts_utc DESC, id DESC LIMIT 1",
            (slug(name),),
        ).fetchone()
        if row:
            ctx.mqtt.publish_person(name, {
                "last_seen": row["ts_local"], "gate": row["gate"], "gate_key": row["gate_key"],
                "method": row["method"], "event_id": row["id"], "message": row["message"],
            })
    publish_health(ctx)


def publish_health(ctx):
    if not ctx.mqtt:
        return
    total = ctx.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    exp = ctx.client.token_exp
    last_success = get_state(ctx.db, "last_success_utc")
    ctx.mqtt.publish_health({
        "last_success": iso(int(last_success)) if last_success else None,
        "consecutive_failures": ctx.failures,
        "login_failures": ctx.login_failures,
        "login_failed": ctx.login_failures >= 3,
        "token_expires": iso(exp) if exp else None,
        "events_total": total,
        "last_http_status": ctx.last_http_status,
        "latency_ms": ctx.latency_ms,
        "whatsapp_enabled": whatsapp_enabled(),
        "alerts_suppressed": ctx.alerts_suppressed,
        "platform_notice": get_state(ctx.db, "platform_notice", "") or "",
        "version": VERSION,
    })


# --------------------------------------------------------------------------- #
# WhatsApp
# --------------------------------------------------------------------------- #
def whatsapp_enabled():
    jid = CFG["group_jid"]
    return bool(jid) and bool(JID_RE.match(jid))


def send_whatsapp(ctx, text):
    """Send, unless the group JID is unset/malformed — then log what would go out."""
    jid = CFG["group_jid"]
    if not jid:
        log.warning("WhatsApp disabled (GROUP_JID unset) — would have sent:\n%s", text)
        return False
    if not JID_RE.match(jid):
        log.error("GROUP_JID %r is not a valid JID — refusing to send", jid)
        return False
    ok, detail = ctx.waha.send_text(jid, text)
    log.info("WhatsApp → %s: %s (%s)", jid, "ok" if ok else "FAILED", detail)
    return ok


def format_alert(rows):
    """One event reads as a sentence; several collapse into a list."""
    def when(row):
        return datetime.fromisoformat(row["ts_local"]).strftime("%d/%m/%Y %H:%M")

    if len(rows) == 1:
        row = rows[0]
        head = f"🚪 *{row['title'] or 'Condfy'}* — acesso registrado\n\n"
        if row["parsed"]:
            method = f" ({row['method']})" if row["method"] else ""
            body = f"*{row['person']}* passou pelo *{row['gate']}*{method}"
        else:
            body = f"_\"{row['message']}\"_"
        return f"{head}{body}\n🕒 {when(row)}"

    lines = [f"🚪 *{rows[0]['title'] or 'Condfy'}* — {len(rows)} acessos\n"]
    for row in rows:
        hhmm = datetime.fromisoformat(row["ts_local"]).strftime("%H:%M")
        if row["parsed"]:
            method = f" ({row['method']})" if row["method"] else ""
            lines.append(f"• {hhmm} *{row['person']}* — {row['gate']}{method}")
        else:
            lines.append(f"• {hhmm} _\"{row['message']}\"_")
    return "\n".join(lines)


def alertable(ctx, row):
    """Age gate, per-person cooldown and the hourly fuse, in that order."""
    if not row["watched"] or row["alerted"]:
        return False
    age_min = (now() - row["ts_utc"]) / 60
    if age_min > CFG["max_age_min"]:
        log.info("event %s is %.0f min old — not alerting", row["id"], age_min)
        return False
    key = f"last_alert_utc:{row['person_key'] or slug(row['watch_name'] or 'x')}"
    last = get_state(ctx.db, key)
    if last and now() - int(last) < CFG["cooldown_min"] * 60:
        log.info("cooldown active for %s — not alerting", row["watch_name"])
        ctx.alerts_suppressed += 1
        return False
    bucket = now() // 3600
    if int(get_state(ctx.db, "alerts_hour_bucket", 0)) != bucket:
        set_state(ctx.db, "alerts_hour_bucket", bucket)
        set_state(ctx.db, "alerts_in_bucket", 0)
    if int(get_state(ctx.db, "alerts_in_bucket", 0)) >= CFG["max_per_hour"]:
        log.warning("hourly alert fuse tripped (%d/h) — suppressing", CFG["max_per_hour"])
        ctx.alerts_suppressed += 1
        return False
    return True


def send_alerts(ctx, rows):
    """One message per poll cycle for all alertable events."""
    if not rows:
        return
    if not send_whatsapp(ctx, format_alert(rows)):
        # Leave alerted=0 so the events retry on a later poll — a WAHA session
        # that is down (as opposed to a rejected message) would otherwise eat
        # the alert silently. The age gate bounds the retrying: once the events
        # pass MAX_ALERT_AGE_MIN they stop being alertable on their own.
        log.warning("send failed — %d event(s) stay pending for a later poll", len(rows))
        return
    set_state(ctx.db, "alerts_in_bucket",
              int(get_state(ctx.db, "alerts_in_bucket", 0)) + 1)
    for row in rows:
        key = f"last_alert_utc:{row['person_key'] or slug(row['watch_name'] or 'x')}"
        set_state(ctx.db, key, now())
    ctx.db.executemany("UPDATE events SET alerted=1 WHERE id=?",
                       [(row["id"],) for row in rows])
    ctx.db.commit()


def health_alert(ctx, text, key, min_interval_s):
    last = get_state(ctx.db, key)
    if last and now() - int(last) < min_interval_s:
        return
    if send_whatsapp(ctx, text):
        set_state(ctx.db, key, now())


# --------------------------------------------------------------------------- #
# weekly presence report
# --------------------------------------------------------------------------- #
REPORT_RETRY_S = 1800


def build_report(ctx, fire_local):
    """Render last week's presence image; returns (data, jpeg_path)."""
    start, end = week_bounds(fire_local)
    data = week_data(ctx.db, slug(CFG["report_person"]), start, end)
    path = f"/data/report-{start.isoformat()}.jpg"
    render_image(data, CFG["report_display"], path)
    return data, path


def send_report(ctx, data, path, jid):
    with open(path, "rb") as fh:
        payload = base64.b64encode(fh.read()).decode()
    ok, detail = ctx.waha.send_image_b64(
        jid, payload, os.path.basename(path),
        caption=report_caption(data, CFG["report_display"]))
    log.info("weekly report → %s: %s (%s)", jid, "ok" if ok else "FAILED", detail)
    return ok


def maybe_send_report(ctx):
    """Fire the weekly report once per schedule slot; late starts catch up.

    The state gate makes the send exactly-once per fire time even across
    restarts; a failed attempt retries every REPORT_RETRY_S rather than every
    poll, so a broken WAHA session does not turn into a 60-second error loop.
    """
    jid = CFG["report_jid"]
    if not jid:
        return
    if not JID_RE.match(jid):
        log.error("REPORT_GROUP_JID %r is not a valid JID — weekly report off", jid)
        return
    fire = last_fire(datetime.now(TZ), CFG["report_dow"], CFG["report_hour"])
    fire_utc = int(fire.timestamp())
    if int(get_state(ctx.db, "report_last_fire_utc", 0) or 0) >= fire_utc:
        return
    last_try = int(get_state(ctx.db, "report_last_attempt_utc", 0) or 0)
    if now() - last_try < REPORT_RETRY_S:
        return
    set_state(ctx.db, "report_last_attempt_utc", now())
    try:
        data, path = build_report(ctx, fire)
        if send_report(ctx, data, path, jid):
            set_state(ctx.db, "report_last_fire_utc", fire_utc)
    except Exception:
        log.exception("weekly report failed — retrying in %ds", REPORT_RETRY_S)


# --------------------------------------------------------------------------- #
# one poll cycle
# --------------------------------------------------------------------------- #
def poll(ctx, allow_alerts=True):
    items = fetch_feed(ctx)
    seeding = get_state(ctx.db, "seeded") is None
    fresh = []

    for item in items:
        if item.get("id") is None:
            continue
        if CFG["license_id"] and item.get("licenseId") not in (None, CFG["license_id"]):
            continue
        row = to_row(item, CFG["watch_names"])
        if insert_row(ctx.db, row, handled=seeding):
            fresh.append(row["id"])
    ctx.db.commit()

    if seeding:
        set_state(ctx.db, "seeded", 1)
        log.info("seeded %d events (no alerts on first run)", len(fresh))

    set_state(ctx.db, "last_success_utc", now())

    # Publish anything not yet published (covers this cycle and any crash gap),
    # oldest first so last_event ends up holding the newest. While the broker is
    # unreachable, rows stay published=0 and are retried on a later poll rather
    # than being marked done and silently lost.
    pending = ctx.db.execute(
        "SELECT * FROM events WHERE published=0 ORDER BY ts_utc ASC, id ASC"
    ).fetchall()
    if pending and not (ctx.mqtt and ctx.mqtt.connected):
        log.warning("MQTT not connected — %d event(s) held for a later poll", len(pending))
        pending = []
    for row in pending:
        if row["type_name"] == ACCESS_TYPE:
            publish_event(ctx, row)
        else:
            note = row["message"]
            set_state(ctx.db, "platform_notice", note)
            if ctx.mqtt:
                ctx.mqtt.publish_notice(
                    {"ts": row["ts_local"], "type": row["type_name"], "message": note})
            ctx.db.execute("UPDATE events SET published=1 WHERE id=?", (row["id"],))
            ctx.db.commit()
            health_alert(ctx, f"⚠️ *Condfy* — aviso da plataforma:\n_{note}_",
                         "notice_alert_utc", 7 * 86400)

    if allow_alerts:
        candidates = ctx.db.execute(
            "SELECT * FROM events WHERE watched=1 AND alerted=0 ORDER BY ts_utc ASC, id ASC"
        ).fetchall()
        send_alerts(ctx, [row for row in candidates if alertable(ctx, row)])

    if ctx.needs_republish:
        republish_snapshots(ctx)
        ctx.needs_republish = False
    publish_health(ctx)
    return len(fresh)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def open_db():
    db = sqlite3.connect(CFG["db_path"], timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.commit()
    return db


def build_ctx(with_mqtt=True):
    db = open_db()
    token = get_state(db, "token") or CFG["bootstrap_token"] or None
    login_path = get_state(db, "login_path") or CFG["login_path"] or None

    def remember(new_token, path):
        set_state(db, "token", new_token)
        set_state(db, "login_path", path)

    device_uuid = get_state(db, "device_uuid")
    if not device_uuid:
        device_uuid = str(uuid.uuid4())
        set_state(db, "device_uuid", device_uuid)

    client = CondfyClient(CFG["base_url"], CFG["email"], CFG["password"],
                          token=token, login_path=login_path, on_token=remember,
                          device_uuid=device_uuid)
    waha = WahaClient({"waha_api_url": CFG["waha_url"], "waha_api_key": CFG["waha_key"],
                       "waha_session": CFG["waha_session"]})
    ctx = Ctx(db, client, None, waha)
    if with_mqtt:
        ctx.mqtt = MqttBridge(
            CFG["mqtt_host"], CFG["mqtt_port"], CFG["mqtt_user"], CFG["mqtt_password"],
            CFG["discovery_prefix"], CFG["watch_names"], VERSION,
            on_ready=lambda: setattr(ctx, "needs_republish", True),
        )
        ctx.mqtt.connect()
    return ctx


def selftest(ctx):
    status = ctx.waha.session_status()
    log.info("WAHA session status: %s", status or "<unreachable>")
    text = ("🚪 *condfy-bridge* — teste de instalação.\n"
            "Se você está lendo isto, o grupo está correto e os alertas de acesso "
            "do Céu Azul virão por aqui.")
    return 0 if send_whatsapp(ctx, text) else 1


def main():
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = sys.argv[1:]

    if "--selftest" in args:
        return selftest(build_ctx(with_mqtt=False))

    if "--report-dry" in args or "--report-test" in args:
        ctx = build_ctx(with_mqtt=False)
        fire = last_fire(datetime.now(TZ), CFG["report_dow"], CFG["report_hour"])
        data, path = build_report(ctx, fire)
        print(report_caption(data, CFG["report_display"]))
        print(f"image: {path}")
        if "--report-dry" in args:
            return 0
        i = args.index("--report-test")
        jid = args[i + 1] if i + 1 < len(args) else CFG["report_jid"]
        if not jid or not JID_RE.match(jid):
            log.error("--report-test needs a valid chat JID (arg or REPORT_GROUP_JID)")
            return 1
        return 0 if send_report(ctx, data, path, jid) else 1

    once = "--once" in args
    ctx = build_ctx(with_mqtt=True)
    log.info("condfy-bridge %s — watching %s | poll %ss | whatsapp %s",
             VERSION, ", ".join(CFG["watch_names"]) or "<nobody>",
             CFG["poll_seconds"], "on" if whatsapp_enabled() else "OFF")

    if once:
        try:
            count = poll(ctx, allow_alerts=False)
            log.info("single poll ok — %d new event(s), no alerts sent", count)
            return 0
        except Exception:
            log.exception("single poll failed")
            return 1

    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(True))

    while not stopping:
        delay = CFG["poll_seconds"]
        try:
            count = poll(ctx)
            ctx.failures = 0
            ctx.login_failures = 0
            log.info("poll ok in %dms — %d new", ctx.latency_ms, count)
        except RetryAfter as exc:
            ctx.failures += 1
            delay = max(exc.seconds, 300)
            log.warning("%s", exc)
        except LoginError as exc:
            ctx.login_failures += 1
            ctx.failures += 1
            delay = BACKOFF[min(ctx.failures - 1, len(BACKOFF) - 1)]
            log.error("login failed (%d): %s", ctx.login_failures, exc)
            if ctx.login_failures >= 3:
                last = get_state(ctx.db, "last_success_utc")
                health_alert(ctx, "⚠️ *condfy-bridge* — 3 falhas seguidas de login no "
                                  "portal Condfy.\nÚltima coleta OK: "
                                  f"{iso(int(last)) if last else 'nunca'}.\n"
                                  "Verifique ARA_CONDFY_EMAIL / ARA_CONDFY_PASSWORD.",
                             "login_alert_utc", 6 * 3600)
            publish_health(ctx)
        except (requests.RequestException, sqlite3.Error) as exc:
            ctx.failures += 1
            delay = BACKOFF[min(ctx.failures - 1, len(BACKOFF) - 1)]
            log.warning("poll failed (%d): %s: %s", ctx.failures, type(exc).__name__, exc)
            publish_health(ctx)
        except Exception:
            ctx.failures += 1
            delay = BACKOFF[min(ctx.failures - 1, len(BACKOFF) - 1)]
            log.exception("unexpected poll failure (%d)", ctx.failures)

        maybe_send_report(ctx)

        slept = 0.0
        step = delay + random.uniform(0, 0.2 * CFG["poll_seconds"])
        while slept < step and not stopping:
            time.sleep(min(1.0, step - slept))
            slept += 1.0

    log.info("shutting down")
    if ctx.mqtt:
        ctx.mqtt.close()
    ctx.db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
