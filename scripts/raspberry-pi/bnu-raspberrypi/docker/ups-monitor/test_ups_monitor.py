"""Offline tests of the ups-monitor rule — simulated outages, no serial port, no WAHA.

    python -m pytest -q test_ups_monitor.py      (or: python test_ups_monitor.py)
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sms_serial  # noqa: E402
import ups_monitor as um  # noqa: E402

# Real replies captured from the bnu UPS on 2026-11-11
RAW_Q = bytes.fromhex("3d 00 00 08 b9 04 8d 00 9b 02 58 03 e8 01 e3 39 74 0d")
RAW_I = bytes.fromhex("3a 50 52 4d 31 35 30 30 42 69 42 69 57 31 2e 33 d2 0d")
RAW_F = bytes.fromhex("3b 45 42 69 53 31 31 35 30 30 30 20 32 34 36 30 6f 0d")

T0 = 1_762_880_000.0  # 2025-11-11 13:53 BRT — any fixed epoch works


def reading(vin=223.0, batt=100.0, load=15.5, on_battery=False, low=False, test=False):
    flags = {name: False for _, name in sms_serial.STATUS_BITS}
    flags.update(ups_ok=True, beep_on=True, on_battery=on_battery, low_battery=low, test_active=test)
    return {"last_input_v": 0.0, "input_v": vin, "output_v": 116.0, "load_pct": load, "output_hz": 60.0,
            "battery_pct": batt, "temperature_c": 48.0, "status_byte": "0x39", "flags": flags}


GRID = reading()


def outage(batt=98.0):
    return reading(vin=0.0, batt=batt, on_battery=True)


class Base(unittest.TestCase):
    env = {"MODE": "live", "ALERT_JID": "bnu@g.us", "TEST_JID": "smoke@g.us", "TZ": "America/Sao_Paulo"}

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
    def test_frames(self):
        self.assertEqual(sms_serial.frame("Q").hex(" "), "51 ff ff ff ff b3 0d")
        self.assertEqual(sms_serial.frame("I").hex(" "), "49 ff ff ff ff bb 0d")
        self.assertEqual(sms_serial.frame("F").hex(" "), "46 ff ff ff ff be 0d")

    def test_parse_real_replies(self):
        for raw, cmd in ((RAW_Q, "Q"), (RAW_I, "I"), (RAW_F, "F")):
            self.assertTrue(sms_serial.valid(raw, cmd))
        self.assertFalse(sms_serial.valid(RAW_Q[:-3] + b"\x00\x00\x0d", "Q"))
        self.assertFalse(sms_serial.valid(RAW_I, "Q"))  # wrong header
        s = sms_serial.parse_status(RAW_Q)
        self.assertEqual((s["input_v"], s["output_v"], s["load_pct"], s["battery_pct"]), (223.3, 116.5, 15.5, 100.0))
        self.assertEqual((s["output_hz"], s["temperature_c"], s["status_byte"]), (60.0, 48.3, "0x39"))
        self.assertTrue(s["flags"]["ups_ok"] and not s["flags"]["on_battery"])
        self.assertEqual(sms_serial.parse_info(RAW_I), {"model": "PRM1500BiBiW", "firmware": "1.3"})
        self.assertEqual(sms_serial.parse_rating(RAW_F),
                         {"voltage_range": "EBiS115", "current_range": "000", "battery_v": 24, "frequency_hz": 60})


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
        self.assertEqual(self.sent(), [("fall", "bnu@g.us", "pending"), ("low", "bnu@g.us", "pending"),
                                       ("restore", "bnu@g.us", "pending")])
        fall, low, restore = self.texts()
        self.assertIn("Falta de energia", fall)
        self.assertIn("na bateria desde 13:53", fall)
        self.assertIn("abaixo de 20 %", low)
        self.assertIn("min até esgotar", low)
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
        r = reading(vin=0, batt=35, on_battery=True, low=True)
        self.run_seq([(r, 1)])
        self.assertEqual([k for k, _, _ in self.sent()], ["fall", "low"])

    def test_self_test_is_not_an_outage(self):
        self.run_seq([(reading(vin=221, on_battery=True, test=True), 30)])
        self.assertEqual(self.store.episodes(), [])

    def test_brownout_is_an_outage(self):
        self.run_seq([(reading(vin=160, batt=99, on_battery=True), 6)])
        self.assertEqual([k for k, _, _ in self.sent()], ["fall"])
        self.assertIn("Rede elétrica instável", self.texts()[0])
        self.assertIn("160 V", self.texts()[0])

    def test_input_drop_without_flag_is_an_outage(self):
        self.run_seq([(reading(vin=0, batt=99, on_battery=False), 6)])
        self.assertEqual([k for k, _, _ in self.sent()], ["fall"])

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
                         ["Falta de energia", "Falta de energia", "Bateria do nobreak abaixo de 20 %"])
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
