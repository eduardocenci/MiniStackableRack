"""Substream fallback of canteiro-mosaico.py, driven without ffmpeg (the pump side only).

python -m pytest scripts/proxmox/frigate/bnu-frigate/test_canteiro_mosaico_sub.py -q
"""
import importlib.util
import os
import sys
import tempfile
import types

try:
    import fcntl  # noqa: F401
except ImportError:                      # Windows: the pump side under test never touches it
    sys.modules["fcntl"] = types.ModuleType("fcntl")
os.environ["RUN_DIR"] = tempfile.mkdtemp(prefix="mosaico-test-")
spec = importlib.util.spec_from_file_location(
    "mosaico", os.path.join(os.path.dirname(__file__), "canteiro-mosaico.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

DT = 1.0 / m.FPS


def frame(v):
    return bytearray([v]) * m.FRAME


class Sim:
    def __init__(self):
        self.l = m.Lens("fixa", "rtsp://x/main")
        self.l.proc = None
        self.t = 1000.0
        self.logs = []
        m.log = self.logs.append

    def run(self, seconds, main=None, sub=None):
        """main / sub = luma value of the frames arriving in this stretch (None = no frames)."""
        shown = None
        for _ in range(int(seconds * m.FPS)):
            self.t += DT
            if main is not None:
                self.l.q.append((frame(main), 1795700000.0 + self.t))
            if sub is not None and self.l.alt_want:
                self.l.altq.append(frame(sub))
            shown = self.l.tick(self.t)
        return bytes(shown[0][:1])[0]

    def label(self):
        return self.l.label.strip()


def test_main_gone_shows_substream_and_returns_after_steady_main():
    s = Sim()
    assert s.run(3, main=200, sub=90) == 200 and s.label() == ""
    assert not s.l.alt_want
    # main stops: held + SEM SINAL after 5 s, substream wanted after 8 s and shown when it delivers
    s.run(7, main=None, sub=90)
    assert s.label().startswith("SEM SINAL") and not s.l.alt_on
    assert s.run(3, main=None, sub=90) == 90
    assert s.l.alt_on and s.label().startswith("BAIXA RESOLUÇÃO") and not s.l.live
    # main comes back: the substream stays for 10 s of steady main, then the main is shown
    assert s.run(5, main=200, sub=90) == 90 and s.l.live and s.label().startswith("BAIXA RESOLUÇÃO")
    assert s.run(6, main=200, sub=90) == 200
    assert not s.l.alt_on and s.label() == ""
    text = "\n".join(s.logs)
    assert "showing the substream" in text and "back on the main" in text
    assert "frames again after" in text          # the MAIN's own hole is still logged
    # the substream feeder lingers, then is released
    assert s.l.alt_want
    s.run(m.ALT_LINGER_S + 2, main=200)
    assert not s.l.alt_want


def test_a_burst_of_main_frames_does_not_end_the_fallback():
    s = Sim()
    s.run(3, main=200)
    s.run(12, main=None, sub=90)
    assert s.l.alt_on
    s.run(4, main=10, sub=90)                    # e.g. the 6 s offline card of a reconnect
    assert s.run(3, main=None, sub=90) == 90 and s.l.alt_on
    assert s.label().startswith("BAIXA RESOLUÇÃO")


def test_no_substream_either_keeps_the_held_frame():
    s = Sim()
    s.run(3, main=200)
    shown = s.run(30, main=None, sub=None)
    assert s.l.alt_want and not s.l.alt_on
    assert s.label().startswith("SEM SINAL") and shown != 200      # dimmed copy


def test_substream_stops_while_shown():
    s = Sim()
    s.run(3, main=200)
    s.run(12, main=None, sub=90)
    assert s.l.alt_on
    s.run(m.STALE_S + 1, main=None, sub=None)
    assert not s.l.alt_on and s.label().startswith("SEM SINAL")
    assert s.run(2, main=None, sub=90) == 90 and s.l.alt_on        # and comes back


def test_fallback_off_without_url():
    s = Sim()
    s.l.alt_url = ""
    s.run(3, main=200)
    s.run(30, main=None, sub=90)
    assert not s.l.alt_want and s.label().startswith("SEM SINAL")
