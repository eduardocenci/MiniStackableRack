"""python -m pytest scripts/raspberry-pi/ara-raspberrypi/canteiro-relay-vigia/ -q"""
import importlib.util
import os

spec = importlib.util.spec_from_file_location(
    "vigia", os.path.join(os.path.dirname(__file__), "canteiro-relay-vigia.py"))
vigia = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vigia)

VIDEO, AUDIO = 150_000, 3_800      # bytes per second


def run(p, rates, t=1000.0, b=0, ready=True):
    """feed 5 s ticks with the given byte rates; -> (events with their time, t, b)"""
    out = []
    for r in rates:
        t += 5
        b += int(r * 5)
        out += [(t, k, d) for k, d in p.tick(t, ready, b)]
    return out, t, b


def test_healthy_stream_is_left_alone():
    p = vigia.Path(20)
    p.tick(1000.0, True, 0)
    ev, _, _ = run(p, [VIDEO] * 200 + [105_000] * 200)
    assert ev == []


def test_audio_only_is_reopened_after_15_s_and_return_is_logged():
    p = vigia.Path(20)
    p.tick(1000.0, True, 0)
    ev, t, b = run(p, [VIDEO] * 4 + [AUDIO] * 3)
    kinds = [k for _, k, _ in ev]
    assert kinds == ["parou", "reabrir"]
    assert ev[0][2]["so_audio"] is True and ev[0][2]["antes_kB_s"] == 150.0
    # the path is reopened: its counter starts again, then the video is back
    p.tick(t + 5, True, 0)
    ev2, _, _ = run(p, [VIDEO], t=t + 5, b=0)
    assert [k for _, k, _ in ev2] == ["voltou"]
    assert ev2[0][2]["reaberturas"] == 1 and 20 <= ev2[0][2]["s"] <= 30


def test_short_hole_under_15_s_is_ignored():
    p = vigia.Path(20)
    p.tick(1000.0, True, 0)
    ev, _, _ = run(p, [VIDEO] * 3 + [0, 0] + [VIDEO] * 3)
    assert ev == []


def test_grace_then_second_reopen_and_hourly_cap():
    p = vigia.Path(20)
    p.tick(1000.0, True, 0)
    ev, _, _ = run(p, [VIDEO] + [AUDIO] * 2000)        # video never comes back
    kicks = [t for t, k, _ in ev if k == "reabrir"]
    assert [k for _, k, _ in ev].count("parou") == 1
    assert kicks[1] - kicks[0] >= vigia.GRACE_S
    first_hour = [k for k in kicks if k - kicks[0] < 3600]
    assert len(first_hour) == vigia.MAX_H


def test_not_ready_path_is_left_to_mediamtx():
    p = vigia.Path(20)
    p.tick(1000.0, True, 0)
    ev, _, _ = run(p, [0] * 50, ready=False)
    assert ev == []


def test_sub_stream_floor():
    p = vigia.Path(7)
    p.tick(1000.0, True, 0)
    ev, _, _ = run(p, [14_500] * 100)                  # quietest night sub measured
    assert ev == []
    ev, _, _ = run(p, [AUDIO] * 4, t=2000.0, b=10**7)
    assert [k for _, k, _ in ev][:2] == ["parou", "reabrir"]


def test_minute_row():
    m = vigia.Minute()
    assert m.row() is None
    for r in (150.0, 4.0, None):
        m.add(r is not None, r)
    assert m.row() == {"pronto": 0.67, "kB_s": 51.3, "min": 4.0, "max": 150.0}


def test_parse_ping():
    out = ("5 packets transmitted, 3 received, 40% packet loss, time 801ms\n"
           "rtt min/avg/max/mdev = 1.983/8.625/39.548/11.763 ms")
    assert vigia.parse_ping(out) == {"perda": 40, "rtt": 8.6, "rtt_max": 39.5}
    lost = vigia.parse_ping("5 packets transmitted, 0 received, 100% packet loss, time 4100ms")
    assert lost == {"perda": 100, "rtt": None, "rtt_max": None}


def test_parse_wifi_keeps_the_camera_line():
    raw = ('{"wifiGetClients":{"clients":[{"ipAddress":"192.168.1.9"},{"ipAddress":"192.168.1.56",'
           '"signalStrength":-41,"snr":53,"iface":"RF_2GHZ","associatedTimeS":49428,'
           '"txStats":{"rateMbps":72,"mcs":7,"throughputMbpsLast15sAvg":4.0992584},'
           '"rxStats":{"rateMbps":86,"mcs":8}}]}}')
    w = vigia.parse_wifi(raw, "192.168.1.56")
    assert w == {"sinal_dBm": -41, "snr": 53, "banda": "2GHZ", "tx_Mbps": 72, "tx_mcs": 7,
                 "rx_Mbps": 86, "rx_mcs": 8, "vazao_Mbps": 4.1, "associado_s": 49428}
    assert vigia.parse_wifi(raw, "192.168.1.200") is None
