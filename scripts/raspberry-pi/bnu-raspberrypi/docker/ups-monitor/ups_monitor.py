#!/usr/bin/env python3
"""ups-monitor: the bnu rack nobreak (SMS Premium 1500 VA) → WhatsApp alerts + activation stats.

Runs on bnu-raspberrypi (container `ups-monitor`) and reads the UPS over its
USB-serial port every POLL_S seconds, using only the Q/I/F queries of sms_serial.py.

THE RULE (decisão Eduardo 11/11/2026: "mandar mensagem em casa BNU quando a energia
cair, quando estiver abaixo de 20 %, quando a energia voltar"):

  outage   = input < INPUT_MIN_V (80 V)  OR  (UPS on battery AND not in a self-test)
  episode  = one transfer to battery ("ativação"). It opens on the first outage poll
             and closes once the grid has been back for RESTORE_CONFIRM_S (10 s), so a
             flapping grid stays ONE episode. Its end time is the first grid poll.
  🔴 queda   once per episode, after ALERT_AFTER_S (10 s) on battery. Shorter blips
             are counted in the stats but never messaged.
  🟠 < 20 %  once per episode, when battery < LOW_BATTERY_PCT or the UPS raises its
             low-battery flag. The queda goes out first if it has not yet.
  ✅ volta   when an episode whose queda was sent closes.

Messages go through a persistent outbox (SQLite), oldest first, retried with backoff
for up to 24 h. Every text carries its own clock time, so a message delayed by a
dead uplink still reads right. MODE: log (no sends) | shadow (SmokeTests,
"[sombra]") | live (ALERT_JID = Casa BNU).

HTTP (PORT 8795): /health (poller alive) · /health/ups (UPS answered < 30 s ago) ·
/api/status · /api/stats (activations 24h/7d/30d/all, read by globalnet) ·
/api/events · /api/history?hours= · POST /api/test (one message to TEST_JID).
Stdlib only.
"""
import datetime
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import sms_serial

GIVE_UP_S = 24 * 3600      # outbox: stop retrying a message after a day
STALE_S = 30               # /health*: no successful read/loop for this long = unhealthy
INFO_REFRESH_S = 3600      # re-read model/firmware/rating hourly
RETENTION_DAYS = 90        # samples table; episodes and outbox are kept forever


# ── config / formatting ────────────────────────────────────────────────────────

class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        f = lambda k, d: float(env.get(k, d))  # noqa: E731
        self.dev = env.get("UPS_DEV", sms_serial.DEFAULT_DEV)
        self.state_dir = env.get("STATE_DIR", "/var/lib/ups-monitor")
        self.port = int(env.get("PORT", "8795"))
        self.poll_s = f("POLL_S", "2")
        self.alert_after_s = f("ALERT_AFTER_S", "10")
        self.restore_confirm_s = f("RESTORE_CONFIRM_S", "10")
        self.low_pct = f("LOW_BATTERY_PCT", "20")
        self.input_min_v = f("INPUT_MIN_V", "80")
        self.sample_s = f("SAMPLE_S", "60")
        self.sample_battery_s = f("SAMPLE_BATTERY_S", "10")
        mode = env.get("MODE", "log").strip().lower()
        self.mode = mode if mode in ("log", "shadow", "live") else "log"
        self.alert_jid = env.get("ALERT_JID", "").strip()
        self.test_jid = env.get("TEST_JID", "").strip()
        self.waha_url = env.get("WAHA_URL", "http://10.1.1.126:3000").rstrip("/")
        self.waha_key = env.get("WAHA_KEY", "")
        self.waha_session = env.get("WAHA_SESSION", "default")
        self.site = env.get("SITE_LABEL", "BNU")
        self.dashboard_url = env.get("DASHBOARD_URL", "http://bnu-raspberrypi:5001")
        self.tz = _tz(env.get("TZ", "America/Sao_Paulo"))


def _tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # no tzdata: Brazil has had no DST since 2019
        return datetime.timezone(datetime.timedelta(hours=-3), "BRT")


def clock(ts, now, tz):
    """'14:32' today, '09/11 14:32' on another day (local time of the site)."""
    t = datetime.datetime.fromtimestamp(ts, tz)
    n = datetime.datetime.fromtimestamp(now, tz)
    return t.strftime("%H:%M") if t.date() == n.date() else t.strftime("%d/%m %H:%M")


def dur(s):
    """'45 s', '12 min', '1 h 05 min' (pt-BR)."""
    s = max(0, int(round(s)))
    if s < 60:
        return f"{s} s"
    m = s // 60
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min"


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# ── storage ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY,
  start_ts REAL NOT NULL, end_ts REAL, clear_since REAL,
  start_batt REAL, min_batt REAL, end_batt REAL, max_load REAL, min_input_v REAL,
  fall_alert_ts REAL, low_alert_ts REAL, restore_alert_ts REAL, end_note TEXT);
CREATE INDEX IF NOT EXISTS episodes_start ON episodes(start_ts);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY, created_ts REAL NOT NULL, kind TEXT NOT NULL, episode_id INTEGER,
  jid TEXT, text TEXT NOT NULL,
  status TEXT NOT NULL,              -- pending | sent | failed | skipped
  attempts INTEGER NOT NULL DEFAULT 0, next_try_ts REAL, sent_ts REAL, last_error TEXT);
CREATE TABLE IF NOT EXISTS samples(
  ts REAL PRIMARY KEY, input_v REAL, output_v REAL, load_pct REAL, battery_pct REAL,
  temperature_c REAL, status_byte TEXT, on_battery INTEGER);
"""
EP_FIELDS = ("start_ts", "end_ts", "clear_since", "start_batt", "min_batt", "end_batt",
             "max_load", "min_input_v", "fall_alert_ts", "low_alert_ts", "restore_alert_ts",
             "end_note")


class Store:
    """One SQLite connection shared by every thread, serialised by a lock."""

    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).lastrowid

    def meta(self, key, default=None):
        rows = self.q("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_meta(self, key, value):
        self.x("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))

    # episodes
    def open_episode(self):
        rows = self.q("SELECT * FROM episodes WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    def new_episode(self, ts, batt, load, vin):
        ep = dict.fromkeys(EP_FIELDS)
        ep.update(start_ts=ts, start_batt=batt, min_batt=batt, max_load=load, min_input_v=vin)
        ep["id"] = self.x(f"INSERT INTO episodes({','.join(EP_FIELDS)}) VALUES ({','.join('?' * len(EP_FIELDS))})",
                          tuple(ep[k] for k in EP_FIELDS))
        return ep

    def update_episode(self, ep, **changes):
        ep.update(changes)
        self.x(f"UPDATE episodes SET {', '.join(f'{k}=?' for k in changes)} WHERE id=?",
               (*changes.values(), ep["id"]))

    def episodes(self, since=None, limit=None):
        sql, args = "SELECT * FROM episodes", []
        if since is not None:
            sql += " WHERE start_ts >= ?"
            args.append(since)
        sql += " ORDER BY start_ts DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.q(sql, args)

    # outbox
    def enqueue(self, kind, episode_id, jid, text, status, now, error=None):
        return self.x("INSERT INTO outbox(created_ts, kind, episode_id, jid, text, status, next_try_ts, last_error) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (now, kind, episode_id, jid, text, status, now, error))

    def next_pending(self):
        rows = self.q("SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT 1")
        return rows[0] if rows else None

    def messages(self, limit=10):
        return self.q("SELECT id, created_ts, kind, episode_id, jid, text, status, attempts, sent_ts, last_error "
                      "FROM outbox ORDER BY id DESC LIMIT ?", (limit,))

    # samples
    def add_sample(self, ts, r, on_batt):
        self.x("INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (ts, r["input_v"], r["output_v"], r["load_pct"], r["battery_pct"], r["temperature_c"],
                r["status_byte"], int(on_batt)))

    def prune_samples(self, before):
        self.x("DELETE FROM samples WHERE ts < ?", (before,))


def episode_view(ep, now):
    """Episode row → API shape (duration of an ongoing one counts up to now)."""
    end = ep["end_ts"] if ep["end_ts"] is not None else (ep["clear_since"] or now)
    return {
        "id": ep["id"], "start_ts": ep["start_ts"], "end_ts": ep["end_ts"],
        "ongoing": ep["end_ts"] is None, "duration_s": round(end - ep["start_ts"], 1),
        "start_batt": ep["start_batt"], "min_batt": ep["min_batt"], "end_batt": ep["end_batt"],
        "max_load": ep["max_load"], "min_input_v": ep["min_input_v"],
        "alerted": ep["fall_alert_ts"] is not None, "low_battery": ep["low_alert_ts"] is not None,
        "end_note": ep["end_note"],
    }


# ── the rule ───────────────────────────────────────────────────────────────────

class Engine:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.lock = threading.RLock()
        self.ep = store.open_episode()          # an outage survives a container restart
        self.resumed = self.ep is not None
        self.reading = None
        self.read_ts = None
        self.loop_ts = None
        self.error = None
        self.fail_since = None
        self.info, self.rating, self.info_ts = None, None, 0.0
        self.last_sample = 0.0
        if store.meta("monitoring_since") is None:
            store.set_meta("monitoring_since", time.time())
        if self.resumed:
            log(f"resumed open episode #{self.ep['id']} (started {clock(self.ep['start_ts'], time.time(), cfg.tz)})")

    def is_outage(self, r):
        f = r["flags"]
        return r["input_v"] < self.cfg.input_min_v or (f["on_battery"] and not f["test_active"])

    def step(self, r, now):
        """Advance the rule by one poll. r = parsed Q reply, or None when the read failed
        (a failed read changes nothing: an open episode stays open)."""
        with self.lock:
            self.loop_ts = now
            if r is None:
                self.fail_since = self.fail_since or now
                return
            self.fail_since, self.error = None, None
            self.reading, self.read_ts = r, now
            out = self.is_outage(r)
            batt, load, vin = r["battery_pct"], r["load_pct"], r["input_v"]
            ep = self.ep
            if out:
                if ep is None:
                    ep = self.ep = self.store.new_episode(now, batt, load, vin)
                    log(f"ON BATTERY — episode #{ep['id']} (input {vin:.0f} V, battery {batt:.0f} %, load {load:.0f} %)")
                else:
                    ch = {}
                    if ep["clear_since"] is not None:
                        ch["clear_since"] = None
                        ch["end_batt"] = None
                        ch["end_note"] = None
                        log(f"episode #{ep['id']}: grid dropped again before the restore was confirmed")
                    if batt < (ep["min_batt"] if ep["min_batt"] is not None else 1e9):
                        ch["min_batt"] = batt
                    if load > (ep["max_load"] or 0):
                        ch["max_load"] = load
                    if vin < (ep["min_input_v"] if ep["min_input_v"] is not None else 1e9):
                        ch["min_input_v"] = vin
                    if ch:
                        self.store.update_episode(ep, **ch)
                low = batt < self.cfg.low_pct or r["flags"]["low_battery"]
                if ep["fall_alert_ts"] is None and (now - ep["start_ts"] >= self.cfg.alert_after_s or low):
                    self._alert("fall", ep, r, now)
                if low and ep["low_alert_ts"] is None:
                    self._alert("low", ep, r, now)
            elif ep is not None:
                if ep["clear_since"] is None:
                    note = "fim aproximado: o monitor reiniciou durante a falta" if self.resumed else None
                    self.store.update_episode(ep, clear_since=now, end_batt=batt, end_note=note)
                    log(f"episode #{ep['id']}: grid back (input {vin:.0f} V, battery {batt:.0f} %) — confirming")
                elif now - ep["clear_since"] >= self.cfg.restore_confirm_s:
                    self.store.update_episode(ep, end_ts=ep["clear_since"])
                    if ep["fall_alert_ts"] is not None:
                        self._alert("restore", ep, r, now)
                    log(f"episode #{ep['id']} closed: {dur(ep['end_ts'] - ep['start_ts'])} on battery, "
                        f"min {ep['min_batt']:.0f} %{'' if ep['fall_alert_ts'] else ' (blip, not messaged)'}")
                    self.ep = None
            self.resumed = False
            self._sample(r, now, out)

    def _sample(self, r, now, out):
        every = self.cfg.sample_battery_s if (out or self.ep) else self.cfg.sample_s
        if now - self.last_sample >= every:
            self.last_sample = now
            self.store.add_sample(now, r, out)
            if int(now) % 3600 < every:
                self.store.prune_samples(now - RETENTION_DAYS * 86400)

    # messages
    def text(self, kind, ep, r, now):
        c, S = self.cfg, self.cfg.site
        since = clock(ep["start_ts"], now, c.tz)
        batt, load = r["battery_pct"], r["load_pct"]
        if kind == "fall":
            vin = r["input_v"]
            head = (f"🔴 *{S} — Falta de energia* — o nobreak do rack está na bateria desde {since}."
                    if vin < c.input_min_v else
                    f"🔴 *{S} — Rede elétrica instável* — entrada em {vin:.0f} V; o nobreak do rack está "
                    f"na bateria desde {since}.")
            return (f"{head} Bateria {batt:.0f} % · carga {load:.0f} %.\n"
                    f"Aviso de novo se a bateria ficar abaixo de {c.low_pct:.0f} % e quando a energia voltar.\n"
                    f"{c.dashboard_url}")
        if kind == "low":
            elapsed = now - ep["start_ts"]
            eta = ""
            drop = (ep["start_batt"] or batt) - batt
            if elapsed >= 120 and drop >= 5:
                eta = f" No ritmo atual, ~{max(1, round(batt / (drop / elapsed) / 60))} min até esgotar (estimativa)."
            return (f"🟠 *{S} — Bateria do nobreak abaixo de {c.low_pct:.0f} %* — {batt:.0f} % após "
                    f"{dur(elapsed)} sem energia (desde {since}). Carga {load:.0f} %.{eta} "
                    f"Se a energia não voltar, o nobreak desliga quando a bateria acabar.")
        if kind == "restore":
            back = clock(ep["end_ts"], now, c.tz)
            note = " (horário aproximado: o monitor reiniciou durante a falta)" if ep.get("end_note") else ""
            return (f"✅ *{S} — Energia voltou* às {back}{note} — o nobreak ficou {dur(ep['end_ts'] - ep['start_ts'])} "
                    f"na bateria (desde {since}) e chegou a {ep['min_batt']:.0f} %. Recarregando.")
        raise ValueError(kind)

    def _alert(self, kind, ep, r, now):
        text = self.text(kind, ep, r, now)
        c = self.cfg
        if c.mode == "live":
            jid = c.alert_jid
        elif c.mode == "shadow":
            jid, text = c.test_jid, "[sombra] " + text
        else:
            jid = None
        status, err = ("pending", None) if jid else ("skipped", f"MODE={c.mode}" if c.mode == "log" else "no JID configured")
        self.store.enqueue(kind, ep["id"], jid, text, status, now, err)
        self.store.update_episode(ep, **{f"{kind}_alert_ts": now})
        log(f"alert {kind} (episode #{ep['id']}) → {status}{f' ({err})' if err else ''}: {text.splitlines()[0]}")

    # API views
    def state(self, now):
        if self.read_ts is None or now - self.read_ts > STALE_S:
            return "no_comm"
        if self.ep is None:
            return "grid"
        return "returning" if self.ep["clear_since"] is not None else "battery"

    def status(self, now):
        with self.lock:
            return {
                "ts": now, "state": self.state(now), "mode": self.cfg.mode,
                "comm_ok": self.read_ts is not None and now - self.read_ts <= STALE_S,
                "read_ts": self.read_ts, "error": self.error, "fail_since": self.fail_since,
                "reading": self.reading, "info": self.info, "rating": self.rating,
                "episode": episode_view(self.ep, now) if self.ep else None,
                "rule": {"alert_after_s": self.cfg.alert_after_s, "restore_confirm_s": self.cfg.restore_confirm_s,
                         "low_battery_pct": self.cfg.low_pct, "input_min_v": self.cfg.input_min_v,
                         "poll_s": self.cfg.poll_s},
            }

    def stats(self, now):
        eps = [episode_view(e, now) for e in self.store.episodes()]
        windows = {}
        for name, span in (("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400), ("all", None)):
            sel = [e for e in eps if span is None or e["start_ts"] >= now - span]
            windows[name] = {
                "activations": len(sel),
                "alerted": sum(1 for e in sel if e["alerted"]),
                "low_battery": sum(1 for e in sel if e["low_battery"]),
                "on_battery_s": round(sum(e["duration_s"] for e in sel), 1),
                "longest_s": max((e["duration_s"] for e in sel), default=0),
                "min_batt": min((e["min_batt"] for e in sel if e["min_batt"] is not None), default=None),
            }
        since = self.store.meta("monitoring_since")
        return {"ts": now, "monitoring_since": float(since) if since else None, "windows": windows,
                "last": eps[0] if eps else None, "recent": eps[:20],
                "messages": self.store.messages(10)}


# ── delivery ───────────────────────────────────────────────────────────────────

def waha_send(cfg, jid, text):
    """POST /api/sendText on the bnu WAHA → (ok, error)."""
    if not cfg.waha_key:
        return False, "WAHA_KEY not configured"
    body = json.dumps({"session": cfg.waha_session, "chatId": jid, "text": text}).encode()
    req = urllib.request.Request(f"{cfg.waha_url}/api/sendText", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Api-Key": cfg.waha_key})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300, None if resp.status < 300 else f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
    except (urllib.error.URLError, OSError) as e:
        return False, str(getattr(e, "reason", e))


class Outbox:
    """Sends pending messages oldest-first; the head of the queue blocks the rest so
    queda → < 20 % → volta never arrive out of order."""

    def __init__(self, cfg, store, send=None):
        self.cfg, self.store = cfg, store
        self.send = send or (lambda jid, text: waha_send(cfg, jid, text))

    def run_once(self, now):
        row = self.store.next_pending()
        if not row or (row["next_try_ts"] or 0) > now:
            return None
        ok, err = self.send(row["jid"], row["text"])
        attempts = row["attempts"] + 1
        if ok:
            self.store.x("UPDATE outbox SET status='sent', sent_ts=?, attempts=?, last_error=NULL WHERE id=?",
                         (now, attempts, row["id"]))
            log(f"sent #{row['id']} {row['kind']} (attempt {attempts})")
        elif now - row["created_ts"] >= GIVE_UP_S:
            self.store.x("UPDATE outbox SET status='failed', attempts=?, last_error=? WHERE id=?",
                         (attempts, err, row["id"]))
            log(f"GAVE UP #{row['id']} {row['kind']} after {attempts} attempts: {err}")
        else:
            wait = min(300, 10 * 2 ** min(attempts - 1, 5))
            self.store.x("UPDATE outbox SET attempts=?, last_error=?, next_try_ts=? WHERE id=?",
                         (attempts, err, now + wait, row["id"]))
            log(f"send #{row['id']} {row['kind']} failed (attempt {attempts}, retry in {wait} s): {err}")
        return ok


# ── threads / HTTP ─────────────────────────────────────────────────────────────

def poll_loop(cfg, engine, port, stop):
    last_err_log = 0.0
    while not stop.is_set():
        t0 = time.monotonic()
        now = time.time()
        if now - engine.info_ts >= INFO_REFRESH_S:  # info_ts starts at 0 → first poll reads it
            try:
                info = sms_serial.parse_info(port.query("I"))
                rating = sms_serial.parse_rating(port.query("F"))
                with engine.lock:
                    engine.info, engine.rating, engine.info_ts = info, rating, now
                log(f"UPS {info['model']} fw {info['firmware']} · {rating['voltage_range']} · "
                    f"{rating['battery_v']} V · {rating['frequency_hz']} Hz")
            except (sms_serial.UpsError, OSError) as e:
                engine.info_ts = now - INFO_REFRESH_S + 60  # retry in a minute
                engine.error = str(e)
        r = None
        try:
            r = sms_serial.parse_status(port.query("Q"))
        except (sms_serial.UpsError, OSError) as e:
            engine.error = str(e)
            if now - last_err_log >= 300:
                last_err_log = now
                log(f"UPS read failed: {e}")
        try:
            engine.step(r, now)
        except Exception as e:  # the poller must outlive any one bad step
            log(f"step failed: {e!r}")
        stop.wait(max(0.2, cfg.poll_s - (time.monotonic() - t0)))


def outbox_loop(outbox, stop):
    while not stop.is_set():
        try:
            outbox.run_once(time.time())
        except Exception as e:
            log(f"outbox round failed: {e!r}")
        stop.wait(2)


def make_handler(cfg, engine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet: globalnet polls every 15 s
            pass

        def _json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                return self._get()
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": str(e)})

        def _get(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            now = time.time()
            if u.path == "/health":
                ok = engine.loop_ts is not None and now - engine.loop_ts <= STALE_S
                return self._json(200 if ok else 503, {"ok": ok, "loop_age_s": None if engine.loop_ts is None else round(now - engine.loop_ts, 1)})
            if u.path == "/health/ups":
                ok = engine.read_ts is not None and now - engine.read_ts <= STALE_S
                return self._json(200 if ok else 503, {"ok": ok, "error": None if ok else engine.error})
            if u.path == "/api/status":
                return self._json(200, engine.status(now))
            if u.path == "/api/stats":
                return self._json(200, engine.stats(now))
            if u.path == "/api/events":
                limit = int((qs.get("limit") or ["50"])[0])
                return self._json(200, [episode_view(e, now) for e in engine.store.episodes(limit=limit)])
            if u.path == "/api/history":
                hours = float((qs.get("hours") or ["24"])[0])
                rows = engine.store.q("SELECT * FROM samples WHERE ts >= ? ORDER BY ts", (now - hours * 3600,))
                return self._json(200, rows)
            if u.path == "/":
                return self._json(200, {"service": "ups-monitor", "endpoints": [
                    "/health", "/health/ups", "/api/status", "/api/stats", "/api/events?limit=",
                    "/api/history?hours=", "POST /api/test"]})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/test":
                return self._json(404, {"error": "not found"})
            if not cfg.test_jid:
                return self._json(503, {"ok": False, "error": "TEST_JID not configured"})
            st = engine.status(time.time())
            r = st["reading"] or {}
            where = {"grid": "na rede", "battery": "NA BATERIA", "returning": "energia voltando",
                     "no_comm": "SEM COMUNICAÇÃO"}.get(st["state"], st["state"])
            text = (f"[teste] ups-monitor ({cfg.site}) → WhatsApp ok. Nobreak agora: "
                    f"{where} · entrada {r.get('input_v', 0):.0f} V · "
                    f"bateria {r.get('battery_pct', 0):.0f} % · carga {r.get('load_pct', 0):.0f} %. "
                    f"Avisos reais (MODE={cfg.mode}) vão para {'o grupo do site' if cfg.mode == 'live' else 'SmokeTests' if cfg.mode == 'shadow' else 'lugar nenhum (só log)'}.")
            ok, err = waha_send(cfg, cfg.test_jid, text)
            return self._json(200 if ok else 502, {"ok": ok, "error": err, "text": text})

    return Handler


def main():
    cfg = Config()
    os.makedirs(cfg.state_dir, exist_ok=True)
    store = Store(os.path.join(cfg.state_dir, "ups.db"))
    engine = Engine(cfg, store)
    outbox = Outbox(cfg, store)
    port = sms_serial.SmsPort(cfg.dev)
    stop = threading.Event()
    log(f"ups-monitor start: dev={cfg.dev} mode={cfg.mode} alert_jid={'set' if cfg.alert_jid else 'MISSING'} "
        f"test_jid={'set' if cfg.test_jid else 'MISSING'} waha_key={'set' if cfg.waha_key else 'MISSING'} "
        f"poll={cfg.poll_s:g}s alert_after={cfg.alert_after_s:g}s low<{cfg.low_pct:g}% http=:{cfg.port}")
    threading.Thread(target=poll_loop, args=(cfg, engine, port, stop), name="poll", daemon=True).start()
    threading.Thread(target=outbox_loop, args=(outbox, stop), name="outbox", daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", cfg.port), make_handler(cfg, engine)).serve_forever()


if __name__ == "__main__":
    main()
