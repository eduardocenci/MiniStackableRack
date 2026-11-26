"""Offline tests of the ups-monitor rule — simulated outages, no NUT server, no WAHA.

    python -m pytest -q test_ups_monitor.py      (or: python test_ups_monitor.py)
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nut_client  # noqa: E402
import ups_monitor as um  # noqa: E402

# Real reply captured from the mia UPS through nut-upsd on 2026-11-26
RAW_VARS = {
    "battery.charge": "100", "battery.charge.low": "10", "battery.runtime": "1560",
    "battery.runtime.low": "300", "battery.voltage": "24.0", "battery.voltage.nominal": "24",
    "device.model": "CP1000PFCLCD", "driver.name": "usbhid-ups", "driver.version": "2.8.0",
    "input.transfer.high": "139", "input.transfer.low": "88", "input.voltage": "118.0",
    "input.voltage.nominal": "120", "output.voltage": "118.0", "ups.load": "20",
    "ups.model": "CP1000PFCLCD", "ups.realpower.nominal": "600", "ups.status": "OL",
    "ups.test.result": "No test initiated",
}
RAW_LIST = ("BEGIN LIST VAR ups\n" + "".join(f'VAR ups {k} "{v}"\n' for k, v in RAW_VARS.items())
            + "END LIST VAR ups\n")

T0 = 1_762_880_000.0  # 2025-11-11 — any fixed epoch works


def reading(vin=118.0, batt=100.0, load=20.0, on_battery=False, low=False, test=False):
    flags = {name: False for _, name in nut_client.STATUS_BITS}
    flags.update(on_line=not on_battery, on_battery=on_battery, low_battery=low, test_active=test)
    return {"input_v": vin, "output_v": 118.0, "load_pct": load, "load_w": round(load * 6),
            "battery_pct": batt, "battery_v": 24.0, "runtime_s": 1560.0, "temperature_c": None,
            "status_byte": "OB DISCHRG" if on_battery else "OL", "flags": flags}


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
    def test_split_dev(self):
        self.assertEqual(nut_client.split_dev("ups@127.0.0.1:3493"), ("ups", "127.0.0.1", 3493))
        self.assertEqual(nut_client.split_dev("ups"), ("ups", "127.0.0.1", 3493))
        self.assertEqual(nut_client.split_dev("ups@mia-raspberrypi"), ("ups", "mia-raspberrypi", 3493))

    def test_parse_real_reply(self):
        v = nut_client.parse_list(RAW_LIST, "ups")
        self.assertEqual(v, RAW_VARS)
        s = nut_client.parse_status(v)
        self.assertEqual((s["input_v"], s["output_v"], s["load_pct"], s["battery_pct"]), (118.0, 118.0, 20.0, 100.0))
        self.assertEqual((s["load_w"], s["runtime_s"], s["status_byte"]), (120, 1560.0, "OL"))
        self.assertTrue(s["flags"]["on_line"] and not s["flags"]["on_battery"])
        self.assertIsNone(s["temperature_c"])
        self.assertEqual(nut_client.parse_info(v)["model"], "CP1000PFCLCD")
        r = nut_client.parse_rating(v)
        self.assertEqual((r["voltage_range"], r["battery_v"], r["realpower_w"]), ("120 V", 24, 600))

    def test_status_tokens(self):
        v = dict(RAW_VARS, **{"ups.status": "OB DISCHRG LB", "input.voltage": "0.0"})
        f = nut_client.parse_status(v)["flags"]
        self.assertTrue(f["on_battery"] and f["low_battery"] and f["discharging"] and not f["on_line"])
        v = dict(RAW_VARS, **{"ups.status": "OB DISCHRG", "ups.test.result": "In progress"})
        self.assertTrue(nut_client.parse_status(v)["flags"]["test_active"])

    def test_errors(self):
        for bad in ("ERR DATA-STALE\n", "ERR DRIVER-NOT-CONNECTED\n", "ERR UNKNOWN-UPS\n",
                    'BEGIN LIST VAR ups\nVAR ups ups.status "OL"\n'):
            with self.assertRaises(nut_client.UpsError):
                nut_client.parse_list(bad, "ups")
        with self.assertRaises(nut_client.UpsError):
            nut_client.parse_status({"ups.status": "OL"})  # no voltage / charge


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
