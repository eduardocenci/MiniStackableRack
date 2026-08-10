#!/usr/bin/env python3
"""Offline end-to-end checks for condfy-bridge.

Stubs paho and WAHA, points the service at a throwaway SQLite file, and drives
`poll()` through the situations that are expensive to get wrong in production:

  1. first run seeds the existing backlog silently (no alert storm)
  2. HA discovery payloads are shaped the way Home Assistant expects
  3. a fresh watched event alerts exactly once
  4. re-polling the same feed is silent (dedup on the notification id)
  5. the per-person cooldown suppresses a follow-up
  6. a different watched person is unaffected by that cooldown
  7. an unwatched person is stored but never alerted
  8. the age gate blocks a stale event
  9. an unparseable sentence is still stored and still matches the watchlist
 10. a non-access notification routes to the notice topic
 11. while MQTT is down, events are held rather than marked published
 12. the weekly presence report pairs passages, fires exactly once per slot,
     and backs off after a failure instead of retrying every poll

No pytest, no network, nothing touched outside a temp directory — matching the
waha-listener convention of plain scripts plus doctests. Run it after changing
anything in app.py, and pair it with the doctests:

    python3 test_bridge.py && python3 -m doctest condfy.py report.py

Exits non-zero if any check fails.
"""
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

TEST_JID = "000000000000000000@g.us"     # deliberately not a real group

# --------------------------------------------------------------------------- #
# stub paho.mqtt.client — records publishes instead of talking to a broker
# --------------------------------------------------------------------------- #
PUBLISHED = []


class _FakeMqttClient:
    def __init__(self, *a, **k):
        self.on_connect = None

    def username_pw_set(self, *a, **k):
        pass

    def will_set(self, *a, **k):
        self.will = a

    def reconnect_delay_set(self, **k):
        pass

    def connect_async(self, *a, **k):
        # paho fires on_connect after the CONNACK, and the discovery publish
        # hangs off that callback — the stub has to do the same.
        if self.on_connect:
            self.on_connect(self, None, None, 0, None)

    connect = connect_async

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def publish(self, topic, payload, qos=1, retain=False):
        PUBLISHED.append((topic, payload, retain))


_paho = types.ModuleType("paho")
_paho_mqtt = types.ModuleType("paho.mqtt")
_client_mod = types.ModuleType("paho.mqtt.client")
_client_mod.Client = _FakeMqttClient
_client_mod.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
_client_mod.MQTTv311 = 4
_paho.mqtt = _paho_mqtt
_paho_mqtt.client = _client_mod
sys.modules.update({"paho": _paho, "paho.mqtt": _paho_mqtt,
                    "paho.mqtt.client": _client_mod})

# Env must be set before importing app — CFG is built at import time.
os.environ.update({
    "DB_PATH": os.path.join(tempfile.mkdtemp(), "condfy.db"),
    "WATCH_NAMES": "Enio Faqueti,Altair Dalpra",
    "GROUP_JID": TEST_JID,
    "CONDFY_EMAIL": "test@example.invalid",
    "CONDFY_PASSWORD": "not-a-real-password",
    "ALERT_COOLDOWN_MIN": "10",
    "ALERT_MAX_PER_HOUR": "8",
    "MAX_ALERT_AGE_MIN": "60",
})

import app  # noqa: E402
from condfy import TZ  # noqa: E402

SENT = []
FEED = []
FAILURES = []


class _FakeWaha:
    def send_text(self, jid, text):
        SENT.append((jid, text))
        return True, "HTTP 201"

    def session_status(self):
        return "WORKING"


class _FakeResponse:
    status_code, ok, headers = 200, True, {}

    def raise_for_status(self):
        pass


def fake_notifications(page=0, size=15):
    return _FakeResponse(), {"page": page, "size": size, "total": len(FEED),
                             "first": True, "last": True, "content": FEED}


def ago(minutes):
    return (datetime.now(TZ) - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")


def item(eid, minutes, message, type_name="CONTROLE_ACESSOS"):
    return {"id": eid, "date": ago(minutes), "title": "Céu Azul", "message": message,
            "typeName": type_name, "licenseId": 9358, "resourceId": eid * 3,
            "open": True, "read": False}


def check(label, condition):
    print(("  ok   " if condition else "  FAIL ") + label)
    if not condition:
        FAILURES.append(label)


ctx = app.build_ctx(with_mqtt=True)
ctx.waha = _FakeWaha()
ctx.client.ensure_token = lambda skew=300: "test-session"
ctx.client.notifications = fake_notifications

# --- 1. first run: a pre-existing backlog must seed silently ---------------- #
FEED[:] = [
    item(735171475, 90, "Altair Dalpra passou por portão grande utilizando tag"),
    item(734322776, 700, "Altair Dalpra passou por portão pequeno utilizando tag"),
    item(733427962, 2000, "Enio Faqueti passou por portão grande utilizando tag"),
]
new = app.poll(ctx)
check("seeding inserts every event", new == 3)
check("seeding sends NO whatsapp", SENT == [])
check("seeded flag set", app.get_state(ctx.db, "seeded") == "1")
check("last_event snapshot published",
      any(t.endswith("last_event") for t, _, _ in PUBLISHED))

# --- 2. discovery payloads are what Home Assistant consumes ----------------- #
disc = {t: json.loads(p) for t, p, _ in PUBLISHED if "/config" in t}
check("discovery published for every entity", len(disc) == 6)
online = disc.get("homeassistant/binary_sensor/condfy_ara/online/config", {})
check("online binary_sensor has NO availability_topic",
      "availability_topic" not in online)
check("online binary_sensor is connectivity",
      online.get("device_class") == "connectivity")
person = disc.get("homeassistant/sensor/condfy_ara/enio_faqueti_last_seen/config", {})
check("per-person sensor is a timestamp", person.get("device_class") == "timestamp")
check("per-person sensor reads its own topic",
      person.get("state_topic") == "condfy/ara/person/enio_faqueti/state")
check("per-person sensor has availability", "availability_topic" in person)
login_pb = disc.get("homeassistant/binary_sensor/condfy_ara/login_problema/config", {})
check("login problem template renders ON/OFF strings",
      "'ON'" in login_pb.get("value_template", "")
      and "'OFF'" in login_pb.get("value_template", ""))
check("every discovery config carries unique_id and device",
      all("unique_id" in c and "device" in c for c in disc.values()))
check("both watched people got a sensor",
      "homeassistant/sensor/condfy_ara/altair_dalpra_last_seen/config" in disc)

# --- 3. a fresh watched event must alert ------------------------------------ #
PUBLISHED.clear()
FEED.insert(0, item(735300000, 2, "Enio Faqueti passou por portão grande utilizando tag"))
app.poll(ctx)
check("fresh watched event alerts once", len(SENT) == 1)
check("alert names the person", "Enio Faqueti" in SENT[0][1])
check("alert names the gate", "portão grande" in SENT[0][1])
check("alert went to the configured group", SENT[0][0] == TEST_JID)
events = [(t, r) for t, _, r in PUBLISHED if t == "condfy/ara/event"]
check("event topic published", len(events) == 1)
check("event topic NOT retained (would replay on reconnect)",
      events and events[0][1] is False)
check("last_event IS retained",
      all(r for t, _, r in PUBLISHED if t == "condfy/ara/last_event"))
check("person topic published",
      any(t == "condfy/ara/person/enio_faqueti/state" for t, _, _ in PUBLISHED))

# --- 4. same feed again: dedup means silence -------------------------------- #
app.poll(ctx)
check("re-polling the same feed sends nothing new", len(SENT) == 1)

# --- 5. second event for the same person, within the cooldown --------------- #
FEED.insert(0, item(735300001, 1, "Enio Faqueti passou por portão pequeno utilizando tag"))
app.poll(ctx)
check("per-person cooldown suppresses the follow-up", len(SENT) == 1)

# --- 6. a different watched person is not affected by that cooldown --------- #
FEED.insert(0, item(735300002, 1, "Altair Dalpra passou por portão grande utilizando tag"))
app.poll(ctx)
check("other watched person still alerts", len(SENT) == 2)
check("second alert names Altair", "Altair Dalpra" in SENT[1][1])

# --- 7. an unwatched person is logged but never alerted --------------------- #
FEED.insert(0, item(735300003, 1, "Fulano de Tal passou por portão grande utilizando tag"))
app.poll(ctx)
check("unwatched person does not alert", len(SENT) == 2)
row = ctx.db.execute("SELECT * FROM events WHERE id=735300003").fetchone()
check("unwatched person is still stored", row is not None and row["watched"] == 0)

# --- 8. an old event never alerts, even when watched ------------------------ #
FEED.insert(0, item(735300004, 300, "Enio Faqueti passou por portão grande utilizando tag"))
app.poll(ctx)
check("age gate blocks a 5h-old event", len(SENT) == 2)

# --- 9. an unparseable sentence still stores and still matches the watchlist - #
app.set_state(ctx.db, "last_alert_utc:enio_faqueti", 0)      # clear the cooldown
FEED.insert(0, item(735300005, 1, "Enio Faqueti teve acesso negado no portão grande"))
app.poll(ctx)
row = ctx.db.execute("SELECT * FROM events WHERE id=735300005").fetchone()
check("unparsed row stored with parsed=0", row is not None and row["parsed"] == 0)
check("unparsed row still flagged watched", row["watched"] == 1)
check("unparsed sentence still alerts", len(SENT) == 3)
check("unparsed alert quotes the raw sentence", "acesso negado" in SENT[2][1])

# --- 10. a non-access notification routes to notice, not the access sensors -- #
FEED.insert(0, item(735300006, 1, "Seu acesso à plataforma irá expirar em 30/09/2026",
                    type_name="AVISO"))
PUBLISHED.clear()
app.poll(ctx)
check("platform notice published to the notice topic",
      any(t == "condfy/ara/notice" for t, _, _ in PUBLISHED))
check("platform notice recorded in state",
      "expirar" in (app.get_state(ctx.db, "platform_notice") or ""))
health = [p for t, p, _ in PUBLISHED if t == "condfy/ara/bridge/state"]
check("health payload published", bool(health))
if health:
    h = json.loads(health[-1])
    check("health reports whatsapp enabled", h["whatsapp_enabled"] is True)
    check("health reports no login problem", h["login_failed"] is False)
    check("health counts events", h["events_total"] >= 9)

# --- 11. broker down: events are held, not silently marked published -------- #
ctx.mqtt.connected = False
app.set_state(ctx.db, "last_alert_utc:altair_dalpra", 0)
FEED.insert(0, item(735300007, 1, "Altair Dalpra passou por portão pequeno utilizando tag"))
before = len(SENT)
app.poll(ctx)
row = ctx.db.execute("SELECT * FROM events WHERE id=735300007").fetchone()
check("event held unpublished while the broker is down", row["published"] == 0)
check("WhatsApp still fires while MQTT is down", len(SENT) == before + 1)

ctx.mqtt.connected = True                                     # broker comes back
app.poll(ctx)
row = ctx.db.execute("SELECT * FROM events WHERE id=735300007").fetchone()
check("held event publishes once the broker returns", row["published"] == 1)

# --- 12. weekly presence report --------------------------------------------- #
import report  # noqa: E402
from datetime import date  # noqa: E402


def presence_row(eid, ts_local, person, type_name="CONTROLE_ACESSOS"):
    ts = datetime.fromisoformat(ts_local)
    ctx.db.execute(
        "INSERT INTO events (id,type_name,title,message,raw_date,ts_local,ts_utc,"
        "person,person_key,gate,gate_key,method,parsed,watched,first_seen_utc,"
        "published,alerted,raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,1,?,1,1,'{}')",
        (eid, type_name, "Céu Azul", f"{person} passou por portão grande utilizando tag",
         ts_local[:16], ts_local, int(ts.timestamp()), person,
         app.slug(person), "portão grande", "portao_grande", "tag", int(ts.timestamp())))


# A person the other sections never touch: their fixtures are timestamped
# relative to *now* and would otherwise bleed into this fixed week.
WEEK = [  # Sun 2026-08-02 … Sat 2026-08-08
    (900001, "2026-08-04T07:26:00-03:00", "Reporte Teste"),
    (900002, "2026-08-04T07:29:00-03:00", "Reporte Teste"),
    (900003, "2026-08-04T07:37:00-03:00", "Reporte Teste"),
    (900004, "2026-08-04T11:08:00-03:00", "Reporte Teste"),
    (900005, "2026-08-06T09:00:00-03:00", "Reporte Teste"),
    (900006, "2026-08-06T10:30:00-03:00", "Reporte Teste"),
    (900007, "2026-08-06T12:00:00-03:00", "Reporte Teste"),   # entrada sem saída
    (900008, "2026-08-04T08:00:00-03:00", "Altair Dalpra"),   # outra pessoa
    (900009, "2026-08-01T09:00:00-03:00", "Reporte Teste"),   # fora da semana
]
for eid, ts_local, person in WEEK:
    presence_row(eid, ts_local, person)
presence_row(900010, "2026-08-05T09:00:00-03:00", "Reporte Teste", type_name="AVISO")
ctx.db.commit()

wd = report.week_data(ctx.db, "reporte_teste", date(2026, 8, 2), date(2026, 8, 8))
check("report: passages counted", wd["passages"] == 7)
check("report: days present", wd["days_present"] == 2)
check("report: tuesday pairs into two blocks", len(wd["days"][2]["blocks"]) == 2)
check("report: tuesday minutes 3+211", int(wd["days"][2]["minutes"]) == 214)
check("report: thursday keeps the open entrada",
      wd["days"][4]["open_entry"] is not None and len(wd["days"][4]["blocks"]) == 1)
check("report: open entrada excluded from the total", int(wd["total_min"]) == 304)
check("report: other person and out-of-week rows ignored",
      all(e["ts"].date() >= date(2026, 8, 2) for d in wd["days"] for e in d["events"]))
cap = report.report_caption(wd, "Ênio Faqueti")
check("report caption carries the total", "5 h 4 min" in cap)
check("report caption flags the missing saída", "sem saída" in cap)

_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
try:
    from PIL import Image  # noqa: F401
    HAVE_RENDER = os.path.exists(_FONT)
except ImportError:
    HAVE_RENDER = False
if HAVE_RENDER:
    out_jpg = report.render_image(wd, "Ênio Faqueti",
                                  os.path.join(tempfile.mkdtemp(), "report.jpg"))
    check("report image renders non-trivially", os.path.getsize(out_jpg) > 20000)
else:
    print("  skip render_image (Pillow or DejaVu fonts not available here)")

REPORT_SENT = []
orig_build, orig_send = app.build_report, app.send_report
app.build_report = lambda ctx, fire: (wd, "/tmp/fake.jpg")
app.send_report = lambda ctx, data, path, jid: (REPORT_SENT.append(jid), True)[1]
app.CFG["report_jid"] = TEST_JID
app.set_state(ctx.db, "report_last_fire_utc", 0)
app.set_state(ctx.db, "report_last_attempt_utc", 0)
app.maybe_send_report(ctx)
check("report fires once its slot is due", REPORT_SENT == [TEST_JID])
app.maybe_send_report(ctx)
check("report does not fire twice for the same slot", len(REPORT_SENT) == 1)

FAILS = []
app.set_state(ctx.db, "report_last_fire_utc", 0)
app.set_state(ctx.db, "report_last_attempt_utc", 0)
app.build_report = lambda ctx, fire: FAILS.append(1) or (_ for _ in ()).throw(RuntimeError("boom"))
app.maybe_send_report(ctx)
check("failed report leaves the slot unsent",
      int(app.get_state(ctx.db, "report_last_fire_utc")) == 0 and len(FAILS) == 1)
app.maybe_send_report(ctx)
check("failed report backs off instead of retrying next poll", len(FAILS) == 1)
app.build_report, app.send_report = orig_build, orig_send
app.CFG["report_jid"] = ""

total = ctx.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
print(f"\nevents stored: {total} | whatsapp sent: {len(SENT)}")
print("FAILURES:", FAILURES or "none")
sys.exit(1 if FAILURES else 0)
