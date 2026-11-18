"""Tests for gpu-vigia.py against a fake /proc and a fake kernel log (no host needed).

Run: python -m pytest scripts/proxmox/bnu-proxmox/gpu-vigia/test_gpu_vigia.py
"""
import importlib.util
import pathlib

import pytest

HERE = pathlib.Path(__file__).parent
T0 = 1_795_000_000.0  # any epoch
KERNEL = "proxmox kernel: amdgpu 0000:04:00.0:"


def stat_line(pid, comm, state, start=100):
    return f"{pid} ({comm}) {state} " + " ".join(["0"] * 18) + f" {start} 0 0\n"


def put(proc, pid, comm, state, wchan="0", threads=()):
    base = proc / str(pid)
    (base / "task" / str(pid)).mkdir(parents=True, exist_ok=True)
    (base / "stat").write_text(stat_line(pid, comm, state))
    (base / "wchan").write_text(wchan)
    (base / "task" / str(pid) / "stat").write_text(stat_line(pid, comm, state))
    (base / "task" / str(pid) / "wchan").write_text(wchan)
    for tid, tstate in threads:
        (base / "task" / str(tid)).mkdir(parents=True, exist_ok=True)
        (base / "task" / str(tid) / "stat").write_text(stat_line(tid, f"{comm}:cs0", tstate))
        (base / "task" / str(tid) / "wchan").write_text(wchan)


@pytest.fixture
def vigia(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("boot-a\n")
    (proc / "loadavg").write_text("590.12 400.0 200.0 3/900 1\n")
    klog = tmp_path / "klog"
    klog.write_text("")
    monkeypatch.setenv("PROC_ROOT", str(proc))
    monkeypatch.setenv("KLOG_FILE", str(klog))
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SAMPLE_GAP", "0")
    spec = importlib.util.spec_from_file_location("gpu_vigia", HERE / "gpu-vigia.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.sent, mod.ran = [], []
    monkeypatch.setattr(mod, "send", lambda text: mod.sent.append(text) or 201)
    monkeypatch.setattr(mod, "run_cmd", lambda cmd: (mod.ran.append(cmd) or True, ""))
    mod.proc, mod.klog = proc, klog
    return mod


def test_healthy_host_is_silent(vigia):
    put(vigia.proc, 10, "ffmpeg", "S", "anon_pipe_read", threads=[(11, "R")])
    put(vigia.proc, 20, "kworker/1:2", "D", "io_schedule")  # disk I/O, not the GPU
    state = {}
    for i in range(3):
        state, _ = vigia.tick(state, T0 + 60 * i)
    assert vigia.sent == [] and vigia.ran == [] and state["streak"] == 0


def test_hang_alerts_once_stops_piling_and_reminds(vigia):
    put(vigia.proc, 10, "ffmpeg", "D", "amdgpu_info_ioctl")
    put(vigia.proc, 12, "ffmpeg", "S", "x", threads=[(13, "D")])  # only a worker thread stuck
    vigia.klog.write_text(f"{T0 + 5} {KERNEL} [mmhub] page fault (src_id:0)\n")
    state, _ = vigia.tick({"klog_ts": T0}, T0 + 30)
    assert vigia.sent == [], "one tick is not a hang yet"
    state, _ = vigia.tick(state, T0 + 90)
    assert len(vigia.sent) == 1 and "travada" in vigia.sent[0] and "ffmpeg ×2" in vigia.sent[0]
    assert "590.12" in vigia.sent[0]
    assert vigia.ran == [vigia.STOP_CMD]
    assert state["hang_since"] == T0 + 5, "dated by the kernel's page fault"
    for i in range(2, 30):
        state, _ = vigia.tick(state, T0 + 30 + 60 * i)
    assert len(vigia.sent) == 1 and vigia.ran == [vigia.STOP_CMD], "no repeat inside REMIND_HOURS"
    state, _ = vigia.tick(state, T0 + 90 + 6 * 3600)
    assert len(vigia.sent) == 2 and "segue travada" in vigia.sent[1]


def test_recovery_after_reboot_and_without(vigia):
    put(vigia.proc, 10, "ffmpeg", "D", "dma_fence_wait_any_timeout")
    state = {}
    for i in range(2):
        state, _ = vigia.tick(state, T0 + 60 * i)
    assert len(vigia.sent) == 1
    # host rebooted: new boot id, the stuck process is gone
    put(vigia.proc, 10, "ffmpeg", "S")
    (vigia.proc / "sys/kernel/random/boot_id").write_text("boot-b\n")
    state, _ = vigia.tick(state, T0 + 3600)
    assert "destravada" in vigia.sent[-1] and "host reiniciado" in vigia.sent[-1]
    assert vigia.ran == [vigia.STOP_CMD] and "hang_since" not in state
    # second hang that clears by itself: the unit we stopped is started again
    put(vigia.proc, 10, "ffmpeg", "D", "amdgpu_info_ioctl")
    for i in range(2):
        state, _ = vigia.tick(state, T0 + 7200 + 60 * i)
    put(vigia.proc, 10, "ffmpeg", "S")
    state, _ = vigia.tick(state, T0 + 7400)
    assert "sem reinício" in vigia.sent[-1] and vigia.ran[-1] == vigia.START_CMD


def test_a_process_that_changes_is_not_stuck(vigia):
    put(vigia.proc, 10, "ffmpeg", "D", "io_schedule")
    calls = {"n": 0}
    real = vigia.sample

    def flapping():
        calls["n"] += 1
        return real() if calls["n"] % 2 else {}

    vigia.sample = flapping
    state = {}
    for i in range(4):
        state, _ = vigia.tick(state, T0 + 60 * i)
    assert vigia.sent == []


def test_non_ffmpeg_needs_a_gpu_wait_channel(vigia):
    put(vigia.proc, 30, "frigate.detecto", "D", "drm_sched_entity_flush")
    state = {}
    for i in range(2):
        state, _ = vigia.tick(state, T0 + 60 * i)
    assert len(vigia.sent) == 1 and "frigate.detecto ×1" in vigia.sent[0]


def test_recovered_reset_is_one_note_per_hour(vigia):
    lines = [
        f"{T0 + 1} {KERNEL} ring vcn_dec_0 timeout, signaled seq=1, emitted seq=4",
        f"{T0 + 2} {KERNEL} Ring vcn_dec_0 reset succeeded",
        f"{T0 + 2} {KERNEL} [drm] device wedged, but recovered through reset",
        f"{T0 + 2} {KERNEL} ring vcn_enc_0.0 timeout, signaled seq=1, emitted seq=1",
    ]
    vigia.klog.write_text("\n".join(lines) + "\n")
    state, _ = vigia.tick({"klog_ts": T0}, T0 + 30)
    assert len(vigia.sent) == 1 and "vcn_dec_0, vcn_enc_0.0" in vigia.sent[0]
    assert "Sem ação" in vigia.sent[0]
    state, _ = vigia.tick(state, T0 + 90)
    assert len(vigia.sent) == 1, "the same kernel lines are not reported twice"
    vigia.klog.write_text(f"{T0 + 600} {KERNEL} ring vcn_dec_0 timeout, x\n")
    state, _ = vigia.tick(state, T0 + 660)
    assert len(vigia.sent) == 1, "second note inside the hour waits"
    state, _ = vigia.tick(state, T0 + 3700)
    assert len(vigia.sent) == 2


def test_first_run_ignores_old_kernel_lines(vigia):
    vigia.klog.write_text(f"{T0 - 5000} {KERNEL} ring vcn_dec_0 timeout, x\n")
    vigia.tick({}, T0)
    assert vigia.sent == []


def test_failed_send_is_retried(vigia):
    put(vigia.proc, 10, "ffmpeg", "D", "amdgpu_info_ioctl")
    ok = {"v": False}

    def flaky(text):
        if not ok["v"]:
            raise OSError("waha down")
        vigia.sent.append(text)

    vigia.send = flaky
    state = {}
    for i in range(3):
        state, _ = vigia.tick(state, T0 + 60 * i)
    assert vigia.sent == [] and len(state["outbox"]) == 1
    ok["v"] = True
    state, _ = vigia.tick(state, T0 + 240)
    assert len(vigia.sent) == 1 and state["outbox"] == []


def test_dry_run_sends_and_runs_nothing(vigia):
    put(vigia.proc, 10, "ffmpeg", "D", "amdgpu_info_ioctl")
    state = {"streak": 1, "first_stuck": T0}
    state, actions = vigia.tick(state, T0 + 60, dry=True)
    assert vigia.sent == [] and vigia.ran == []
    assert any(a.startswith("would run") for a in actions)
    assert any(a.startswith("msg:") for a in actions)
