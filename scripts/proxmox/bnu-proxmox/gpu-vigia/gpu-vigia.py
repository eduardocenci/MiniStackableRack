#!/usr/bin/env python3
"""gpu-vigia - watches bnu-proxmox for the amdgpu hang and says so on WhatsApp.

Authoritative copy: scripts/proxmox/bnu-proxmox/gpu-vigia/gpu-vigia.py
Live copy:          bnu-proxmox:/usr/local/bin/gpu-vigia.py (gpu-vigia.timer, every 60 s)

Why: on 2026-11-17 15:54 EST the Radeon 680M's VCN page-faulted and the kernel's own GPU reset
deadlocked. From then on every ffmpeg that opened /dev/dri/renderD128 sat in uninterruptible
sleep (state D), immune to SIGKILL; canteiro-mosaico (Restart=always, Frigate LXC 105) piled up
~570 of them, load 590, and /live had no mosaic for 16 h before a human noticed. Nothing watched
for it: canteiro-watchdog only restarts go2rtc + canteiro-hls, which cannot help here.

What one run (one tick) does:
  1. HANG  - reads /proc SAMPLES times, SAMPLE_GAP apart. A thread is "stuck" when it is in
             state D in every sample and belongs to a COMMS process (ffmpeg), or is any process
             whose wait channel matches WCHAN_RE (amdgpu / drm / dma_fence). Stuck threads in
             TICKS_TO_ALERT consecutive ticks = hang: runs STOP_CMD once (stops the pile-up) and
             alerts NOTIFY_JID. It never reboots and never resets the GPU - amdgpu_gpu_recover
             was tried on 2026-11-18 and only blocks behind the dead reset; the cure is a host
             reboot, which is a human decision.
  2. STILL - one reminder every REMIND_HOURS while the hang lasts.
  3. BACK  - first tick with nothing stuck after a hang: says how long it lasted. If the host
             was not rebooted in between, runs START_CMD (after a reboot the unit starts itself).
  4. NOTE  - ring timeouts the kernel recovered from by itself ("device wedged, but recovered
             through reset", seen 2026-11-18 12:27 EST) are reported, at most once per
             NOTE_HOURS, with no action: they are the early sign of the same VCN trouble.

Messages that WAHA refuses stay in the state file and are retried on the next tick (WAHA lives
in LXC 101 on this same host and may be starved during a hang).

Env (file /etc/gpu-vigia.env, mode 600): WAHA_URL, WAHA_KEY, WAHA_SESSION, NOTIFY_JID; optional
STATE_DIR, SAMPLES, SAMPLE_GAP, TICKS_TO_ALERT, REMIND_HOURS, NOTE_HOURS, COMMS, WCHAN_RE,
STOP_CMD, START_CMD, HOST_LABEL, TZ_NAME.

Usage:
  gpu-vigia.py                 one tick (what the timer runs)
  gpu-vigia.py --dry-run       one tick, printing what it would do; sends nothing, runs no
                               command, writes no state
  gpu-vigia.py --test-alert    sends one test line to NOTIFY_JID
Test hooks: PROC_ROOT (fake /proc), KLOG_FILE (fake `journalctl -k -o short-unix` output).
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

ENV = os.environ
WAHA_URL = ENV.get("WAHA_URL", "http://10.1.1.126:3000")
WAHA_KEY = ENV.get("WAHA_KEY", "")
WAHA_SESSION = ENV.get("WAHA_SESSION", "default")
NOTIFY_JID = ENV.get("NOTIFY_JID", "")
STATE_DIR = ENV.get("STATE_DIR", "/var/lib/gpu-vigia")
PROC_ROOT = ENV.get("PROC_ROOT", "/proc")
KLOG_FILE = ENV.get("KLOG_FILE", "")
SAMPLES = int(ENV.get("SAMPLES", "5"))
SAMPLE_GAP = float(ENV.get("SAMPLE_GAP", "1.0"))
TICKS_TO_ALERT = int(ENV.get("TICKS_TO_ALERT", "2"))
REMIND_HOURS = float(ENV.get("REMIND_HOURS", "6"))
NOTE_HOURS = float(ENV.get("NOTE_HOURS", "1"))
COMMS = {c for c in ENV.get("COMMS", "ffmpeg").split(",") if c}
WCHAN_RE = re.compile(ENV.get("WCHAN_RE", r"amdgpu|drm_|dma_fence"))
STOP_CMD = ENV.get("STOP_CMD", "pct exec 105 -- systemctl stop --no-block canteiro-mosaico")
START_CMD = ENV.get("START_CMD", "pct exec 105 -- systemctl start --no-block canteiro-mosaico")
HOST_LABEL = ENV.get("HOST_LABEL", "bnu-proxmox")
TZ = ZoneInfo(ENV.get("TZ_NAME", "America/Sao_Paulo"))

STATE_FILE = os.path.join(STATE_DIR, "state.json")
RING_TIMEOUT = re.compile(r"amdgpu .*ring (\S+) timeout")
RECOVERED = re.compile(r"amdgpu .*(recovered through reset|reset succeeded)")
PAGE_FAULT = re.compile(r"amdgpu .*page fault")
OUTBOX_MAX = 20


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def parse_stat(text):
    """/proc/<pid>/stat -> (comm, state, starttime); comm may hold spaces and parentheses."""
    left, _, right = text.rpartition(")")
    fields = right.split()
    if not left or len(fields) < 20:
        return None
    return left.split("(", 1)[-1], fields[0], fields[19]


def sample():
    """One look at /proc: {(pid, tid, starttime): (comm, wchan)} for the threads in state D."""
    found = {}
    try:
        pids = [p for p in os.listdir(PROC_ROOT) if p.isdigit()]
    except OSError:
        return found
    for pid in pids:
        base = os.path.join(PROC_ROOT, pid)
        main = parse_stat(read(os.path.join(base, "stat")))
        if not main:
            continue
        comm = main[0]
        if comm in COMMS:
            # the hang parks worker threads (ffmpeg:cs0) as well as the main one
            try:
                tids = os.listdir(os.path.join(base, "task"))
            except OSError:
                tids = [pid]
            for tid in tids:
                st = parse_stat(read(os.path.join(base, "task", tid, "stat"))) if tid != pid else main
                if st and st[1] == "D":
                    wchan = read(os.path.join(base, "task", tid, "wchan")) or read(os.path.join(base, "wchan"))
                    found[(pid, tid, st[2])] = (comm, wchan.strip())
        elif main[1] == "D":
            wchan = read(os.path.join(base, "wchan")).strip()
            if WCHAN_RE.search(wchan):
                found[(pid, pid, main[2])] = (comm, wchan)
    return found


def stuck_now():
    """Threads in state D in every one of SAMPLES looks - a busy disk never holds that."""
    held = None
    for i in range(SAMPLES):
        if i:
            time.sleep(SAMPLE_GAP)
        seen = sample()
        held = seen if held is None else {k: v for k, v in held.items() if k in seen}
        if not held:
            break
    return held or {}


def kernel_lines(since):
    """amdgpu kernel lines newer than `since` (epoch): [(epoch, text)]."""
    if KLOG_FILE:
        raw = read(KLOG_FILE)
    else:
        try:
            raw = subprocess.run(
                ["journalctl", "-k", "-o", "short-unix", "--no-pager", "--since", f"@{int(since)}"],
                capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            raw = ""
    out = []
    for line in raw.splitlines():
        if "amdgpu" not in line:
            continue
        stamp, _, text = line.partition(" ")
        try:
            ts = float(stamp)
        except ValueError:
            continue
        if ts > since:
            out.append((ts, text))
    return out


def hhmm(ts, with_date=False):
    return datetime.datetime.fromtimestamp(ts, TZ).strftime("%d/%m %H:%M" if with_date else "%H:%M")


def span(seconds):
    minutes = max(1, round(seconds / 60))
    return f"{minutes} min" if minutes < 120 else f"{minutes / 60:.1f} h".replace(".", ",")


def load1():
    return (read(os.path.join(PROC_ROOT, "loadavg")).split() or ["?"])[0]


def boot_id():
    return read(os.path.join(PROC_ROOT, "sys/kernel/random/boot_id")).strip()


def describe(stuck):
    procs = {}
    for (pid, _tid, _start), (comm, _wchan) in stuck.items():
        procs.setdefault(comm, set()).add(pid)
    return ", ".join(f"{comm} ×{len(pids)}" for comm, pids in sorted(procs.items()))


def send(text):
    body = json.dumps({"session": WAHA_SESSION, "chatId": NOTIFY_JID, "text": text}).encode()
    req = urllib.request.Request(
        f"{WAHA_URL}/api/sendText", data=body,
        headers={"Content-Type": "application/json", "X-Api-Key": WAHA_KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def run_cmd(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=45)
        return r.returncode == 0, (r.stderr or r.stdout).strip()[:200]
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)[:200]


def tick(state, now, dry=False):
    """Advances the state by one look at the host. Returns (state, actions for the log)."""
    actions = []
    outbox = state.setdefault("outbox", [])

    def say(text):
        actions.append(f"msg: {text}")
        outbox.append(text)

    def do(cmd):
        if dry:
            actions.append(f"would run: {cmd}")
            return True
        ok, detail = run_cmd(cmd)
        actions.append(f"ran: {cmd} -> {'ok' if ok else 'FAILED ' + detail}")
        return ok

    stuck = stuck_now()
    klog = kernel_lines(state.get("klog_ts") or now)
    if klog:
        state["klog_ts"] = max(ts for ts, _ in klog)
    else:
        state.setdefault("klog_ts", now)
    rings = sorted({m.group(1) for _, t in klog for m in [RING_TIMEOUT.search(t)] if m})
    recovered = any(RECOVERED.search(t) for _, t in klog)
    if any(PAGE_FAULT.search(t) for _, t in klog):
        state["fault_ts"] = min(ts for ts, t in klog if PAGE_FAULT.search(t))

    hang = state.get("hang_since")
    if stuck:
        state["streak"] = state.get("streak", 0) + 1
        state.setdefault("first_stuck", now)
        state["peak"] = max(state.get("peak", 0), len({k[0] for k in stuck}))
        if not hang and state["streak"] >= TICKS_TO_ALERT:
            # the kernel's page fault, when this run saw it, dates the hang better than we do
            fault = state.get("fault_ts")
            since = fault if fault and now - fault < 1800 else state["first_stuck"]
            state.update(hang_since=since, hang_boot=boot_id(), last_remind=now)
            stopped = do(STOP_CMD)
            say(f"🧊 *GPU do {HOST_LABEL} travada* desde {hhmm(since)} — processos presos em "
                f"estado D: {describe(stuck)}; carga {load1()}. O /live e o mosaico da TV ficam "
                f"fora. " + ("`canteiro-mosaico` parado para não empilhar processos. "
                             if stopped else "NÃO consegui parar o `canteiro-mosaico`. ")
                + "Só um reinício do host destrava — precisa do OK do Eduardo.")
        elif hang and now - state.get("last_remind", hang) >= REMIND_HOURS * 3600:
            state["last_remind"] = now
            say(f"🧊 GPU do {HOST_LABEL} segue travada há {span(now - hang)} (desde "
                f"{hhmm(hang, True)}) — {describe(stuck)}; carga {load1()}. Falta reiniciar o host.")
    else:
        state["streak"] = 0
        state.pop("first_stuck", None)
        if hang:
            rebooted = boot_id() != state.get("hang_boot")
            tail = "host reiniciado."
            if not rebooted:
                tail = ("sem reinício do host; `canteiro-mosaico` religado."
                        if do(START_CMD) else "sem reinício do host; NÃO consegui religar o `canteiro-mosaico`.")
            say(f"✅ *GPU do {HOST_LABEL} destravada* — ficou ~{span(now - hang)} travada "
                f"({hhmm(hang, True)}–{hhmm(now)}); {tail}")
            for key in ("hang_since", "hang_boot", "last_remind", "peak", "fault_ts"):
                state.pop(key, None)
        elif rings or recovered:
            state["note_rings"] = sorted(set(state.get("note_rings", [])) | set(rings))
            state.setdefault("note_ts", klog[0][0])

    # a recovered reset is only worth a line when nothing stayed stuck
    if state.get("note_ts") and not stuck and not state.get("hang_since"):
        if now - state.get("last_note", 0) >= NOTE_HOURS * 3600:
            names = ", ".join(state.get("note_rings") or ["anel não identificado"])
            say(f"⚠️ GPU do {HOST_LABEL}: timeout de anel às {hhmm(state['note_ts'])} ({names}) "
                f"— o kernel recuperou por reset, nada ficou preso. Sem ação.")
            state["last_note"] = now
            state.pop("note_ts", None)
            state.pop("note_rings", None)
    elif stuck or state.get("hang_since"):
        state.pop("note_ts", None)
        state.pop("note_rings", None)

    if not dry:
        while outbox:
            try:
                send(outbox[0])
            except Exception as e:  # noqa: BLE001 - keep the message for the next tick
                actions.append(f"send failed, kept {len(outbox)} for the next tick: {e}")
                break
            outbox.pop(0)
        del outbox[:-OUTBOX_MAX]
    state["last_tick"] = now
    state["stuck"] = len({k[0] for k in stuck})
    return state, actions


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def main(argv):
    if "--test-alert" in argv:
        print("HTTP", send(f"🧪 gpu-vigia do {HOST_LABEL}: teste de mensagem ({hhmm(time.time())})."))
        return 0
    dry = "--dry-run" in argv
    state, actions = tick(load_state(), time.time(), dry=dry)
    for line in actions:
        print(line)
    if dry:
        print(json.dumps(state, ensure_ascii=False))
    else:
        save_state(state)
        if state.get("hang_since") or actions:
            print(f"stuck={state['stuck']} streak={state.get('streak')} hang_since={state.get('hang_since')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
