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
