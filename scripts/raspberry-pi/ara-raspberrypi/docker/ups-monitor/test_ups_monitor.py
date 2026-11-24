"""Offline tests of the ara ups-monitor rule — simulated outages, no USB, no WAHA.

    python -m pytest -q test_ups_monitor.py      (or: python test_ups_monitor.py)
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ecoflow_hid  # noqa: E402
import ups_monitor as um  # noqa: E402

# Real reports captured from the EcoFlow RIVER 3 Plus on 2026-11-24 (on grid, 100 %)
RAW = {0x07: bytes.fromhex("07 0c 00"), 0x0C: bytes.fromhex("0c 64"),
       0x0D: bytes.fromhex("0d a4 09"), 0x0B: bytes.fromhex("0b 14 05")}
RAW_INFO = {0x01: bytes.fromhex("01 1e 01"), 0x0A: bytes.fromhex("0a 64 05"), 0x0F: bytes.fromhex("0f 0a")}

T0 = 1_762_880_000.0  # 2025-11-11 13:53 BRT — any fixed epoch works


def reading(batt=100, ac=True, low=False, runtime=2468):
    """A reading built from raw reports, the way the poller builds it."""
    bits = 0x0008 | (0x0004 if ac else 0x0002) | (0x0010 if low else 0)
    return ecoflow_hid.parse_status({
        0x07: bytes([0x07, bits & 0xFF, bits >> 8]), 0x0C: bytes([0x0C, int(batt)]),
        0x0D: bytes([0x0D, runtime & 0xFF, runtime >> 8]), 0x0B: RAW[0x0B]})


GRID = reading()


def outage(batt=98.0):
    return reading(batt=batt, ac=False)


class Base(unittest.TestCase):
    env = {"MODE": "live", "ALERT_JID": "ara@g.us", "TEST_JID": "smoke@g.us", "TZ": "America/Sao_Paulo"}

    def setUp(self):
        self.store = um.Store(":memory:")
        self.cfg = um.Config(dict(self.env))
        self.eng = um.Engine(self.cfg, self.store)

    def run_seq(self, seq, start=T0, step=2.0):
        """Feed (reading, n_polls) pairs every `step` s; returns the next timestamp."""
        t = start
        for r, n in seq:
            for _ in range(n):
                self.eng.step(r, t)
                t += step
        return t

    def sent(self):
        return [(m["kind"], m["jid"], m["status"]) for m in reversed(self.store.messages(50))]

    def texts(self):
        return [m["text"] for m in reversed(self.store.messages(50))]


class ProtocolTest(unittest.TestCase):
    def test_parse_real_reports(self):
        s = ecoflow_hid.parse_status(RAW)
        self.assertEqual((s["battery_pct"], s["runtime_s"], s["battery_v"], s["status_byte"]),
                         (100.0, 2468, 13.0, "0x000c"))
        f = s["flags"]
        self.assertTrue(f["ac_present"] and f["battery_present"])
        self.assertFalse(f["on_battery"] or f["low_battery"] or f["discharging"])
        self.assertIsNone(s["input_v"])
        self.assertIsNone(s["load_pct"])
        self.assertEqual(ecoflow_hid.parse_rating(RAW_INFO),
                         {"capacity_wh": 286, "battery_v": 13.8, "warning_pct": 10})

    def test_ac_lost_is_on_battery(self):
        f = outage()["flags"]
        self.assertTrue(f["on_battery"] and f["discharging"] and not f["ac_present"])

    def test_find_device_reads_sysfs(self):
        with tempfile.TemporaryDirectory() as d:
            for name, files in (("1-1", {"idVendor": "2109", "idProduct": "3431"}),
                                ("1-1.2", {"idVendor": "3746", "idProduct": "ffff", "busnum": "1", "devnum": "3",
                                           "product": "EF-UPS-RIVER 3 Plus", "manufacturer": "EcoFlow",
                                           "serial": "X1", "bcdDevice": "0100"})):
                os.makedirs(os.path.join(d, name))
                for k, v in files.items():
                    with open(os.path.join(d, name, k), "w") as fh:
                        fh.write(v + "\n")
            dev = ecoflow_hid.find_device(d)
            self.assertEqual((dev["node"], dev["model"], dev["firmware"]),
                             ("/dev/bus/usb/001/003", "EF-UPS-RIVER 3 Plus", "1.00"))
            os.remove(os.path.join(d, "1-1.2", "idVendor"))
            with self.assertRaises(ecoflow_hid.UpsError):
                ecoflow_hid.find_device(d)


class RuleTest(Base):
    def test_grid_is_quiet(self):
        self.run_seq([(GRID, 100)])
        self.assertEqual(self.store.episodes(), [])
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.eng.state(T0 + 200), "grid")

    def test_blip_counted_not_messaged(self):
        self.run_seq([(GRID, 3), (outage(), 2), (GRID, 10)])  # 4 s on battery
        eps = self.store.episodes()
        self.assertEqual(len(eps), 1)
        self.assertIsNotNone(eps[0]["end_ts"])
        self.assertAlmostEqual(eps[0]["end_ts"] - eps[0]["start_ts"], 4.0)
        self.assertEqual(self.sent(), [])
        st = self.eng.stats(T0 + 100)["windows"]["24h"]
        self.assertEqual((st["activations"], st["alerted"]), (1, 0))

    def test_full_outage_fall_low_restore(self):
        seq = [(GRID, 5)]
        seq += [(outage(98 - i * 0.2), 1) for i in range(400)]   # 800 s, battery 98 → 18.2
        seq += [(GRID, 10)]
        self.run_seq(seq)
        self.assertEqual(self.sent(), [("fall", "ara@g.us", "pending"), ("low", "ara@g.us", "pending"),
                                       ("restore", "ara@g.us", "pending")])
        fall, low, restore = self.texts()
        self.assertIn("Falta de energia no canteiro", fall)
        self.assertIn("autonomia estimada 41 min", fall)
        self.assertIn("na bateria desde 13:53", fall)
        self.assertIn("abaixo de 20 %", low)
        self.assertIn("Energia voltou", restore)
        self.assertIn("13 min na bateria", restore)
        self.assertIn("chegou a 18 %", restore)
        ep = self.store.episodes()[0]
        self.assertIsNotNone(ep["fall_alert_ts"])
        self.assertAlmostEqual(ep["fall_alert_ts"] - ep["start_ts"], 10.0)  # ALERT_AFTER_S

    def test_flapping_grid_is_one_episode(self):
        self.run_seq([(outage(), 10), (GRID, 3), (outage(), 5), (GRID, 3), (outage(), 2), (GRID, 8)])
        self.assertEqual(len(self.store.episodes()), 1)
        self.assertEqual([k for k, _, _ in self.sent()], ["fall", "restore"])

    def test_low_battery_at_start_sends_fall_first(self):
        self.run_seq([(outage(batt=15), 1)])
        self.assertEqual([k for k, _, _ in self.sent()], ["fall", "low"])

    def test_low_battery_flag_counts(self):
        self.run_seq([(reading(batt=35, ac=False, low=True), 1)])
        self.assertEqual([k for k, _, _ in self.sent()], ["fall", "low"])

    def test_no_dashboard_line_unless_configured(self):
        self.run_seq([(outage(), 6)])
        self.assertNotIn("http", self.texts()[0])

    def test_comm_loss_keeps_episode_open(self):
        t = self.run_seq([(outage(), 6), (None, 30)])
        self.assertEqual(self.eng.state(t), "no_comm")
        self.assertIsNotNone(self.store.open_episode())
        self.run_seq([(GRID, 8)], start=t)
        self.assertEqual([k for k, _, _ in self.sent()], ["fall", "restore"])

    def test_restart_mid_outage_resumes(self):
        t = self.run_seq([(outage(), 10)])
        eng2 = um.Engine(self.cfg, self.store)          # container restarted
        self.assertTrue(eng2.resumed)
        self.eng = eng2
        self.run_seq([(GRID, 8)], start=t + 60)
        self.assertEqual([k for k, _, _ in self.sent()], ["fall", "restore"])
        self.assertIn("horário aproximado", self.texts()[-1])
        self.assertEqual(len(self.store.episodes()), 1)

    def test_restart_still_on_battery_no_duplicate(self):
        t = self.run_seq([(outage(), 10)])
        self.eng = um.Engine(self.cfg, self.store)
        self.run_seq([(outage(), 10), (GRID, 8)], start=t + 5)
        self.assertEqual([k for k, _, _ in self.sent()], ["fall", "restore"])
        self.assertNotIn("aproximado", self.texts()[-1])

    def test_stats_windows(self):
        t = self.run_seq([(outage(), 10), (GRID, 8)])                    # alerted, 20 s
        t = self.run_seq([(outage(), 2), (GRID, 8)], start=t + 3 * 86400)  # blip, 3 days later
        w = self.eng.stats(t)["windows"]
        self.assertEqual((w["24h"]["activations"], w["7d"]["activations"], w["all"]["alerted"]), (1, 2, 1))
        self.assertAlmostEqual(w["all"]["longest_s"], 20.0)
        self.assertEqual(self.eng.stats(t)["last"]["alerted"], False)


class ModeTest(Base):
    def test_shadow_goes_to_smoketests(self):
        self.cfg.mode = "shadow"
        self.run_seq([(outage(), 6)])
        self.assertEqual(self.sent(), [("fall", "smoke@g.us", "pending")])
        self.assertTrue(self.texts()[0].startswith("[sombra] "))

    def test_log_mode_sends_nothing(self):
        self.cfg.mode = "log"
        self.run_seq([(outage(), 6)])
        self.assertEqual(self.sent(), [("fall", None, "skipped")])


class OutboxTest(Base):
    def test_retry_in_order_then_deliver(self):
        self.run_seq([(outage(batt=15), 1)])           # fall + low queued
        calls, up = [], {"ok": False}

        def send(jid, text):
            calls.append(text.split("*")[1])
            return (up["ok"], None if up["ok"] else "timeout")
        box = um.Outbox(self.cfg, self.store, send=send)
        now = T0 + 10
        box.run_once(now)                              # fails → backoff 10 s
        box.run_once(now + 5)                          # still backing off → no call
        self.assertEqual(len(calls), 1)
        up["ok"] = True
        box.run_once(now + 11)
        box.run_once(now + 12)
        self.assertEqual([c.split(" — ")[1] for c in calls],
                         ["Falta de energia no canteiro", "Falta de energia no canteiro", "Bateria do nobreak abaixo de 20 %"])
        self.assertEqual([s for _, _, s in self.sent()], ["sent", "sent"])

    def test_gives_up_after_a_day(self):
        self.run_seq([(outage(), 6)])
        box = um.Outbox(self.cfg, self.store, send=lambda j, t: (False, "down"))
        box.run_once(T0 + um.GIVE_UP_S + 100)
        self.assertEqual(self.sent()[0][2], "failed")


class FormatTest(unittest.TestCase):
    def test_dur(self):
        self.assertEqual([um.dur(s) for s in (5, 59, 60, 754, 3600, 3900)],
                         ["5 s", "59 s", "1 min", "12 min", "1 h 00 min", "1 h 05 min"])


class StoreFileTest(unittest.TestCase):
    def test_persists_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ups.db")
            s = um.Store(path)
            cfg = um.Config({"MODE": "log"})
            e = um.Engine(cfg, s)
            e.step(outage(), T0)
            s.db.close()
            s2 = um.Store(path)
            self.assertIsNotNone(s2.open_episode())
            s2.db.close()


if __name__ == "__main__":
    unittest.main()
