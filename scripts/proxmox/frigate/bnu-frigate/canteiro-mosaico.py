#!/usr/bin/env python3
"""canteiro-mosaico — composes THE live view of the ARA canteiro and never stops publishing.

    fixed lens (left, street side) | PT lens (right, parked at rest)      -> path `mosaico`     2560x720
    the same picture at 1920x540 over the canteiro-painel data band        -> path `mosaico-tv`  1920x1080

Authoritative copy: scripts/proxmox/frigate/bnu-frigate/canteiro-mosaico.py
Live copy:          bnu Frigate LXC 105:/usr/local/bin/canteiro-mosaico.py
                    (started by canteiro-mosaico.sh / canteiro-mosaico.service, same folder)

WHY THREE PROCESSES AND A PUMP (2026-11-18)
Until 2026-11-18 one ffmpeg read both lenses and `hstack`ed them. A filter graph only emits a
frame when EVERY input has one, so a hole in the canteiro feed (Starlink uplink, camera off its
LAN) stopped the output; after 10 s without RTP mediamtx closed both publisher sessions, the
script restarted ffmpeg, the new one got no frame either ("No filtered frames for output
stream") and so on in ~70 s cycles until the canteiro-watchdog healed go2rtc + canteiro-hls:
20 drops of the TV picture in 15 h on 2026-11-16/17, 1 to 7 min each. No ffmpeg option fixes
that inside one process: a stalled live input cannot be told apart from a slow one, and an
input that ended is never reopened.

Now the output does not depend on any input being alive:

  feeder (ffmpeg, one per lens)   RTSP -> decode (VAAPI) -> fps=15,scale=1280x720 -> raw yuv420p
                                  on a pipe. Dies on EOF / 10 s without packets; restarted here
                                  3 s later, for ever, without touching anything else.
  pump (this process)             a 15 fps clock. Every tick it takes the next frame of each lens
                                  from a small jitter queue - or, when the queue is empty, the
                                  LAST frame again - plus the last good panel picture, and
                                  writes ONE raw frame to the encoder. A lens with no new frame
                                  for 5 s is dimmed and labelled "SEM SINAL · último quadro
                                  HH:MM:SS" (Brasília); it clears by itself with the next frame.
  encoder (ffmpeg)                reads that single raw stream from stdin, cuts it back into the
                                  two lenses and the panel, stacks, encodes twice (h264_vaapi)
                                  and publishes to canteiro-hls. It has no network input and no
                                  clock of its own, so nothing can stall or desynchronise it.

The raw frame is 1280x2712 yuv420p: lens L (1280x720), lens R (1280x720), the panel folded
into 1280x1080 (its left 1280 px over its right 640 px) and the thumbnail block (1280x192, see
below) one under the other. Same width = the
planes are simply concatenated (no pixel work in Python, ~73 MB/s through the pipe), and one
stream = every filter in the encoder sees the same timestamps: there is no second input to
drift against (the old `-re -f image2` panel input and the setpts wall-clock alignment of the
lenses are gone with the problem they solved).

What each failure does now:
  one lens / both lenses stop      the picture keeps going out; that half holds its last frame,
                                   dims after 5 s; back by itself when the feeder gets frames
  canteiro-hls restarts (heal)     the encoder loses its publishers -> only the encoder is
                                   restarted (5 s; no RTSP input to reopen, no keyframe to wait)
  encoder stops reading its pipe   3 s write timeout -> encoder restarted
  decoder paints a lens grey       canteiro-mosaico-cinza.py on that lens' own frames, 3 checks
                                   30 s apart -> only that lens' feeder is restarted (max 3/h)
  a child survives SIGKILL         (amdgpu hang, 2026-11-17: every ffmpeg that opened renderD128
                                   sat in state D) nothing new is started in its place while it
                                   lives - one stuck process per role instead of one every 90 s

Things kept from the single-process composer, with their reasons:
  - decode on the GPU, scale + stack on the CPU: the all-GPU graph (scale_vaapi + hstack_vaapi)
    runs out of VAAPI surfaces on this Mesa 22.3 ("Cannot allocate memory" after 11 frames).
  - `mosaico-tv` exists because the Neo QLED's Cast receiver refuses H.264 2560 px wide.
  - the panel PNG is fetched every 30 s; only a complete PNG replaces the last good one;
    unreachable = the last picture stays; never fetched = a black band.
  - a publisher, not a go2rtc `exec:` source (Frigate refuses exec sources).

ÚLTIMAS DETECÇÕES ON THE TV (2026-11-23, pedido Eduardo)
`mosaico-tv` carries, at the bottom right of each lens, the last three Frigate detections of
that lens as small moving pictures - what /live shows as GIFs over the video. Only the TV
output: `mosaico` also feeds the canteiro-vigia videos and the watchdog frame, which must stay
clean. canteiro-painel (bnu-raspberrypi) draws them (PIL lives there): /det/tv.json lists six
keys, /det/tv/<key>.yuv is a loop of raw I420 frames 172x96 with border, age and icon already
on it. Here a poll thread fetches what changed (every 15 s) and an 8 fps thread copies the
current frame of each loop into the thumbnail block: 96 rows of picture (three thumbnails at
x=0, three at x=640) over 96 rows of mask (235 where there is a thumbnail, 16 elsewhere). The
encoder cuts the two groups, uses the mask as alpha and overlays them on the TV branch - so an
empty slot, an unreachable panel (5 min) or MINIATURAS_URL="" is simply nothing drawn.

SUBSTREAM FALLBACK (2026-11-27)
The iM9 stops sending the VIDEO of one stream - nearly always the main of the fixed lens, by
day - for 0.5 to 6 min and keeps its audio, so the session never times out anywhere (92 holes
>= 30 s of the fixed lens in three days against none over 12 s of the PT lens). Reopening the
camera session does not bring the video back (tried on 2026-11-27 from the relay: the new
session is audio-only too); the substream of the same lens goes on. So a lens whose main has
had no frame for 8 s gets a second feeder on its substream (software decode, 640x480 stretched
to 1280x720 - the same field of view) and shows it, labelled "BAIXA RESOLUÇÃO · desde
HH:MM:SS" (the time of the last main frame), until the main has delivered frames steadily for 10 s. The substream
feeder stays up 2 min more (the holes come in clusters) and is then stopped. `live` and the
journal lines ("no frame for 5 s" / "frames again after N s") keep describing the MAIN, so the
hole statistics stay comparable; "showing the substream" / "back on the main" are extra lines.
No substream either (the camera is off the LAN) = the held, dimmed frame as before.

State for humans: /run/canteiro-painel/mosaico-estado.json (rewritten every 5 s).
MOSAICO_HW=0 swaps VAAPI for software decode + libx264 (tests away from the LXC).
Stdlib only: it runs on the Frigate LXC's system python (3.11; written to run on 3.8+).
"""
import collections
import fcntl
import json
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

E = os.environ.get
FFMPEG = E("FFMPEG", "/usr/lib/ffmpeg/7.0/bin/ffmpeg")
HW = E("MOSAICO_HW", "1") != "0"
DRI = E("MOSAICO_DRI", "/dev/dri/renderD128")
SRC = [("fixa", E("SRC_FIXA", "rtsp://127.0.0.1:8554/canteiro_fixa")),
       ("pt", E("SRC_PT", "rtsp://127.0.0.1:8554/canteiro"))]
# the same lens' substream (640x480, the same picture squeezed to 4:3): shown, stretched back to
# 16:9, while the main has no video - see SUBSTREAM FALLBACK in the docstring. "" = off.
SRC_ALT = {"fixa": E("SRC_FIXA_ALT", "rtsp://127.0.0.1:8554/canteiro_fixa_sub"),
           "pt": E("SRC_PT_ALT", "rtsp://127.0.0.1:8554/canteiro_sub")}
ALT_AFTER_S = float(E("ALT_AFTER_S", "8"))    # main without a frame for this long -> substream
ALT_BACK_S = float(E("ALT_BACK_S", "10"))     # main frames steady for this long -> back to it
ALT_LINGER_S = float(E("ALT_LINGER_S", "120"))  # keep the substream feeder warm after going back
OUT = E("OUT_BASE", "rtsp://10.1.1.123:8555")          # + /mosaico and /mosaico-tv
PAINEL_URL = E("PAINEL_URL", "http://10.1.1.123:8557/painel.png")
RUN = E("RUN_DIR", "/run/canteiro-painel")
CINZA = E("CINZA", "/usr/local/bin/canteiro-mosaico-cinza.py")
FONT = E("FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
GREY_MAX = int(E("GREY_MAX", "3"))        # feeder restarts per hour for a grey lens
GREY_LIMIT = float(E("GREY_LIMIT", "0.30"))
STALE_S = float(E("STALE_S", "5"))        # no new frame for this long = "SEM SINAL"
SRC_TIMEOUT_S = int(E("SRC_TIMEOUT_S", "10"))
SRC_TIMEOUT_OPT = E("SRC_TIMEOUT_OPT", "-timeout")   # ffmpeg < 5 calls it -stimeout

FPS = 15
W, H = 1280, 720
YS, CS = W * H, W * H // 4
FRAME = YS + 2 * CS
PW, PH = 1280, 1080                       # the 1920x540 panel folded: 1280x540 over 640x540(+pad)
PYS, PCS = PW * PH, PW * PH // 4
PFRAME = PYS + 2 * PCS
TW, TH, TPITCH = 172, 96, 180               # thumbnail, and the step between two of a group
TGW = 2 * TPITCH + TW                       # a group of three: 532 px
TBH = 2 * TH                                # block: picture rows over mask rows
TYS, TCS = W * TBH, W * TBH // 4
TFRAME = TW * TH * 3 // 2
TSLOTS = 6                                  # 0-2 left lens (fixa), 3-5 right lens (PT)
MINIATURAS_URL = E("MINIATURAS_URL", "http://10.1.1.123:8557/det").rstrip("/")   # "" = off
MINIATURAS_STALE_S = float(E("MINIATURAS_STALE_S", "300"))
Q_HIGH = 8        # frames (~0.5 s) the jitter queue may hold before playing ~12 % faster
Q_SNAP = 45       # 3 s: beyond this drop to the newest frames at once
BRT = timezone(timedelta(hours=-3))       # the LXC clock is US Eastern; the label is Brasília
STOP = threading.Event()
mono = time.monotonic


def log(msg):
    print("canteiro-mosaico: " + msg, file=sys.stderr, flush=True)


def grow_pipe(fd):
    for size in (4 << 20, 1 << 20):
        try:
            fcntl.fcntl(fd, 1031, size)   # F_SETPIPE_SZ
            return
        except OSError:
            continue


def reap(proc, wait=10):
    """Kill and collect. True = gone; False = it survived SIGKILL (state D)."""
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=wait)
        return True
    except subprocess.TimeoutExpired:
        return False


def write_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _dim_tables():
    y = bytes(max(16, min(235, int(16 + (v - 16) * 0.40))) for v in range(256))
    c = bytes(int(128 + (v - 128) * 0.35) for v in range(256))
    return y, c


DIM_Y, DIM_C = _dim_tables()
BLANK = (bytes([26]) * YS, bytes([128]) * CS, bytes([128]) * CS)


class Lens:
    """One lens: its feeder process, a jitter queue and the frame being shown."""

    def __init__(self, name, url):
        self.name, self.url = name, url
        self.q = collections.deque()
        self.lock = threading.Lock()
        self.proc = None
        self.stuck = None              # a feeder that survived SIGKILL
        self.started = 0.0
        self.restarts = 0
        self.raw = None                # (y, u, v) of the last real frame, undimmed
        self.shown = BLANK
        self.new_mono = None           # when the shown frame was last replaced by a new one
        self.new_wall = None
        self.live = False
        self.since = mono()            # start of the current live / not-live stretch
        self.acc = 0
        self.grey = 0
        self.label = None
        self.label_path = os.path.join(RUN, "sem-sinal-%s.txt" % name)
        self.set_label(" ")
        # substream fallback
        self.alt_url = SRC_ALT.get(name) or ""
        self.altq = collections.deque()
        self.alt_proc = None
        self.alt_want = False          # the alt feeder should be running
        self.alt_until = 0.0           # ...at least until this instant (linger)
        self.alt_on = False            # the substream is what is being shown
        self.alt_new = None            # when the last substream frame was shown
        self.alt_shows = 0
        self.main_run = None           # start of the current steady run of main frames
        self.main_last = None

    # ---- feeder side (own thread) -------------------------------------------------------
    def cmd(self):
        c = [FFMPEG, "-hide_banner", "-loglevel", E("FEEDER_LOGLEVEL", "error"), "-nostdin"]
        if HW:
            c += ["-init_hw_device", "vaapi=va:" + DRI, "-hwaccel", "vaapi", "-hwaccel_device", "va"]
        c += ["-rtsp_transport", "tcp", SRC_TIMEOUT_OPT, str(SRC_TIMEOUT_S * 1000000),
              "-analyzeduration", "1000000", "-i", self.url, "-an",
              "-vf", "fps=%d,scale=%d:%d" % (FPS, W, H), "-pix_fmt", "yuv420p",
              "-f", "rawvideo", "pipe:1"]
        return c

    def feeder_loop(self):
        said_stuck = 0.0
        while not STOP.is_set():
            if self.stuck is not None:
                if self.stuck.poll() is None:
                    if mono() - said_stuck > 600:
                        said_stuck = mono()
                        log("lens %s: feeder pid %d survived SIGKILL - not starting another "
                            "while it lives (GPU/driver?)" % (self.name, self.stuck.pid))
                    STOP.wait(10)
                    continue
                self.stuck = None
            try:
                p = subprocess.Popen(self.cmd(), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, bufsize=0)
            except OSError as e:
                log("lens %s: cannot start feeder: %s" % (self.name, e))
                STOP.wait(10)
                continue
            grow_pipe(p.stdout.fileno())
            self.started = mono()
            self.proc = p
            while True:
                buf = bytearray(FRAME)
                mv = memoryview(buf)
                off = 0
                while off < FRAME:
                    n = p.stdout.readinto(mv[off:])
                    if not n:
                        break
                    off += n
                if off < FRAME:
                    break
                with self.lock:
                    self.q.append((buf, time.time()))
                    while len(self.q) > 2 * Q_SNAP:      # the pump is not ticking: bound memory
                        self.q.popleft()
            self.proc = None
            if not reap(p):
                self.stuck = p
            self.restarts += 1
            STOP.wait(3)

    def alt_cmd(self):
        return [FFMPEG, "-hide_banner", "-loglevel", E("FEEDER_LOGLEVEL", "error"), "-nostdin",
                "-rtsp_transport", "tcp", SRC_TIMEOUT_OPT, str(SRC_TIMEOUT_S * 1000000),
                "-analyzeduration", "1000000", "-i", self.alt_url, "-an",
                "-vf", "fps=%d,scale=%d:%d" % (FPS, W, H), "-pix_fmt", "yuv420p",
                "-f", "rawvideo", "pipe:1"]

    def alt_loop(self):
        """The substream feeder: runs only while the pump wants it (software decode, no GPU)."""
        while not STOP.is_set():
            if not self.alt_want:
                STOP.wait(0.5)
                continue
            try:
                p = subprocess.Popen(self.alt_cmd(), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, bufsize=0)
            except OSError as e:
                log("lens %s: cannot start the substream feeder: %s" % (self.name, e))
                STOP.wait(10)
                continue
            grow_pipe(p.stdout.fileno())
            self.alt_proc = p
            while self.alt_want:
                buf = bytearray(FRAME)
                mv = memoryview(buf)
                off = 0
                while off < FRAME:
                    n = p.stdout.readinto(mv[off:])
                    if not n:
                        break
                    off += n
                if off < FRAME:
                    break
                with self.lock:
                    self.altq.append(buf)
                    while len(self.altq) > Q_HIGH:       # always the newest: no backlog to play
                        self.altq.popleft()
            self.alt_proc = None
            reap(p)
            with self.lock:
                self.altq.clear()
            STOP.wait(2)

    def kill_feeder(self):
        p = self.proc
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass

    def kill_alt(self):
        self.alt_want = False
        p = self.alt_proc
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass

    # ---- pump side (main thread) --------------------------------------------------------
    def set_label(self, text):
        if text != self.label:
            self.label = text
            write_atomic(self.label_path, text.encode("utf-8"))

    def tick(self, now):
        item = None
        with self.lock:
            n = len(self.q)
            if n > Q_SNAP:                               # a burst after a long gap: jump to now
                for _ in range(n - 2):
                    self.q.popleft()
            if self.q:
                item = self.q.popleft()
                if len(self.q) > Q_HIGH:                 # running late: skip 1 frame in 8
                    self.acc += 1
                    if self.acc >= 8:
                        self.acc = 0
                        item = self.q.popleft()
                else:
                    self.acc = 0
        if item is not None:
            buf, wall = item
            mv = memoryview(buf)
            self.raw = (mv[:YS], mv[YS:YS + CS], mv[YS + CS:])
            if self.main_last is None or now - self.main_last > 1.0:
                self.main_run = now                      # a new steady run of main frames
            self.main_last = now
            self.new_mono, self.new_wall = now, wall
            if self.alt_on and now - self.main_run >= ALT_BACK_S:
                self.alt_on = False
                self.alt_until = now + ALT_LINGER_S
                log("lens %s: back on the main" % self.name)
                self.set_label(" ")
            if not self.alt_on:
                self.shown = self.raw
            if not self.live:
                if self.since is not None:
                    log("lens %s: frames again after %.0f s" % (self.name, now - self.since))
                self.live, self.since = True, now
                if not self.alt_on:
                    self.set_label(" ")
        elif self.live and now - self.new_mono > STALE_S:
            self.live, self.since = False, self.new_mono
            when = datetime.fromtimestamp(self.new_wall, BRT)
            log("lens %s: no frame for %.0f s - holding the last one (%s BRT)"
                % (self.name, STALE_S, when.strftime("%H:%M:%S")))
            if not self.alt_on:
                self.hold(when)
        elif not self.live and self.raw is None and self.label == " " and now - self.since > 20:
            # (20 s, not STALE_S: an ordinary start waits up to a 4 s GOP for its first frame)
            self.set_label("SEM SINAL")
        elif (not self.live and self.raw is not None
              and now - self.new_mono > 43200 and "/" not in self.label):
            when = datetime.fromtimestamp(self.new_wall, BRT)        # held for 12 h: add the date
            self.set_label("SEM SINAL  ·  último quadro " + when.strftime("%d/%m %H:%M:%S"))
        self.tick_alt(now)
        # a feeder that is up but delivers nothing (hung decode): the RTSP timeout does not see it
        p = self.proc
        if p is not None and now - self.started > 30 and now - (self.new_mono or self.started) > 30 \
                and not self.q:
            self.started = now
            log("lens %s: feeder up 30 s without a frame - restarting it" % self.name)
            self.kill_feeder()
        return self.shown

    def hold(self, when):
        y, u, v = self.raw
        self.shown = (bytes(y).translate(DIM_Y), bytes(u).translate(DIM_C),
                      bytes(v).translate(DIM_C))
        self.set_label("SEM SINAL  ·  último quadro " + when.strftime("%H:%M:%S"))

    def tick_alt(self, now):
        """The substream while the main has no video (see SUBSTREAM FALLBACK in the docstring)."""
        if not self.alt_url:
            return
        main_gone = (not self.live and self.raw is not None and self.new_mono is not None
                     and now - self.new_mono > ALT_AFTER_S)
        if main_gone:
            self.alt_want = True
            self.alt_until = now + ALT_LINGER_S
        elif self.alt_want and not self.alt_on and now > self.alt_until:
            self.kill_alt()
        if not (main_gone or self.alt_on):
            return
        buf = None
        with self.lock:
            if self.altq:
                buf = self.altq.popleft()
        if buf is not None:
            mv = memoryview(buf)
            self.shown = (mv[:YS], mv[YS:YS + CS], mv[YS + CS:])
            self.alt_new = now
            if not self.alt_on:
                self.alt_on = True
                self.alt_shows += 1
                when = datetime.fromtimestamp(self.new_wall, BRT).strftime("%H:%M:%S")
                log("lens %s: showing the substream (main without video since %s BRT)"
                    % (self.name, when))
                self.set_label("BAIXA RESOLUÇÃO  ·  desde " + when)
        elif self.alt_on and now - self.alt_new > STALE_S:
            self.alt_on = False                          # the substream stopped too
            log("lens %s: the substream stopped too" % self.name)
            if self.live:
                self.shown = self.raw
                self.set_label(" ")
            else:
                self.hold(datetime.fromtimestamp(self.new_wall, BRT))


class Panel:
    """The canteiro-painel band: fetched every 30 s, folded to 1280x1080 raw by a one-shot ffmpeg."""

    def __init__(self):
        self.png = os.path.join(RUN, "painel.png")
        self.planes = (bytes([16]) * PYS, bytes([128]) * PCS, bytes([128]) * PCS)
        self.ok_wall = None

    def convert(self):
        try:
            r = subprocess.run(
                [FFMPEG, "-v", "error", "-nostdin", "-i", self.png, "-filter_complex",
                 "[0:v]scale=1920:540,format=yuv420p,split=2[a][b];[a]crop=1280:540:0:0[l];"
                 "[b]crop=640:540:1280:0,pad=1280:540:0:0[r];[l][r]vstack=inputs=2",
                 "-frames:v", "1", "-pix_fmt", "yuv420p", "-f", "rawvideo", "-"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return False
        d = r.stdout
        if len(d) != PFRAME:
            return False
        self.planes = (d[:PYS], d[PYS:PYS + PCS], d[PYS + PCS:])
        return True

    def loop(self):
        if os.path.isfile(self.png) and os.path.getsize(self.png) > 0:
            self.convert()                               # the last good one of a previous run
        while not STOP.is_set():
            try:
                with urllib.request.urlopen(PAINEL_URL, timeout=10) as r:
                    d = r.read()
                if d[:4] == b"\x89PNG" and d[-8:] == b"IEND\xaeB`\x82":
                    write_atomic(self.png, d)
                    if self.convert():
                        self.ok_wall = time.time()
            except Exception:                            # unreachable = the last picture stays
                pass
            STOP.wait(30)


class Thumbs:
    """The last detections of each lens as moving thumbnails (drawn by canteiro-painel)."""

    def __init__(self):
        self.y = bytearray(bytes([16]) * TYS)            # picture rows black, mask rows "nothing"
        self.u = bytearray(bytes([128]) * TCS)
        self.v = bytearray(bytes([128]) * TCS)
        self.planes = (bytes(self.y), bytes(self.u), bytes(self.v))
        self.want = [None] * TSLOTS                      # (key, n, data) per slot, set by poll_loop
        self.have = [None] * TSLOTS
        self.idx = [-1] * TSLOTS
        self.fps = 8
        self.ok_wall = None
        self.said = None

    @staticmethod
    def x0(k):
        return (k % 3) * TPITCH + (0 if k < 3 else W // 2)

    def _get(self, path, timeout):
        with urllib.request.urlopen(MINIATURAS_URL + path, timeout=timeout) as r:
            return r.read()

    def poll_once(self):
        j = json.loads(self._get("/tv.json", 5).decode("utf-8"))
        if (j.get("w"), j.get("h")) != (TW, TH):
            raise ValueError("thumbnail size %sx%s, expected %dx%d" % (j.get("w"), j.get("h"), TW, TH))
        keys = (list(j.get("slots") or []) + [None] * TSLOTS)[:TSLOTS]
        cur = {s[0]: s for s in self.want if s}
        new = []
        for key in keys:
            if key is None or key in cur:
                new.append(cur.get(key))
                continue
            d = self._get("/tv/%s.yuv" % key, 20)
            n = len(d) // TFRAME
            if n < 1 or len(d) != n * TFRAME:
                raise ValueError("loop %s: %d bytes" % (key, len(d)))
            new.append((key, n, d))
        self.fps = max(1, min(15, int(j.get("fps") or 8)))
        self.want = new
        self.ok_wall = time.time()

    def poll_loop(self):
        while not STOP.is_set():
            try:
                self.poll_once()
                if self.said:
                    log("miniaturas: back")
                    self.said = None
            except Exception as e:                       # the panel is away: keep, then clear
                msg = "%s: %s" % (e.__class__.__name__, e)
                if msg != self.said:
                    self.said = msg
                    log("miniaturas: " + msg[:160])
                if any(self.want) and (self.ok_wall is None
                                       or time.time() - self.ok_wall > MINIATURAS_STALE_S):
                    self.want = [None] * TSLOTS
                    log("miniaturas: no list for %.0f s - clearing them" % MINIATURAS_STALE_S)
            STOP.wait(15)

    def _mask(self, k, on):
        x, val = self.x0(k), bytes([235 if on else 16]) * TW
        for r in range(TH, 2 * TH):
            self.y[r * W + x:r * W + x + TW] = val

    def _blit(self, k, data, i):
        x, o = self.x0(k), i * TFRAME
        mv = memoryview(data)
        y = self.y
        for r in range(TH):
            y[r * W + x:r * W + x + TW] = mv[o + r * TW:o + (r + 1) * TW]
        hw, hx, cw = W // 2, x // 2, TW // 2
        for plane, base in ((self.u, o + TW * TH), (self.v, o + TW * TH + cw * (TH // 2))):
            for r in range(TH // 2):
                plane[r * hw + hx:r * hw + hx + cw] = mv[base + r * cw:base + (r + 1) * cw]

    def step(self, t):
        """Bring the block to instant t. True when something changed."""
        changed = False
        want = self.want
        for k in range(TSLOTS):
            s = want[k]
            if s is not self.have[k]:
                self.have[k], self.idx[k] = s, -1
                self._mask(k, s is not None)
                changed = True
            if s is None:
                continue
            i = int(t * self.fps) % s[1]
            if i != self.idx[k]:
                self.idx[k] = i
                self._blit(k, s[2], i)
                changed = True
        if changed:
            self.planes = (bytes(self.y), bytes(self.u), bytes(self.v))
        return changed

    def anim_loop(self):
        while not STOP.wait(1.0 / 16):
            try:
                self.step(mono())
            except Exception as e:                       # never take the pump down for a thumbnail
                log("miniaturas: %s: %s - clearing them" % (e.__class__.__name__, e))
                self.want = [None] * TSLOTS
                STOP.wait(5)


class Encoder:
    def __init__(self, lenses):
        self.lenses = lenses
        self.proc = None
        self.fd = -1
        self.stuck = None
        self.started = 0.0
        self.next_spawn = 0.0
        self.restarts = -1
        self.kill_reason = None        # set by the watch thread, acted on by the pump
        self.said_stuck = 0.0
        m = re.match(r"rtsp://([^/:]+)(?::(\d+))?", OUT)
        self.dst = "%s:%s" % (m.group(1), m.group(2) or "554")

    def cmd(self):
        draw = ""
        for i, l in enumerate(self.lenses):
            draw += (",drawtext=fontfile=%s:textfile=%s:reload=1:x=%d:y=36:fontsize=40:"
                     "fontcolor=white:borderw=3:bordercolor=black" % (FONT, l.label_path, i * W + 36))
        up = "format=nv12,hwupload" if HW else "format=yuv420p"
        t0 = 2 * H + PH                                  # first row of the thumbnail block
        gx, gy = 960 - TGW - 10, 540 - TH - 10           # a group inside its 960x540 half
        graph = ("[0:v]split=8[a][b][c][d][e][f][g][h];"
                 "[a]crop=%d:%d:0:0[l];[b]crop=%d:%d:0:%d[r];" % (W, H, W, H, H)
                 + "[l][r]hstack=inputs=2" + draw + ",split=2[m1][m2];"
                 "[m1]" + up + "[v];[m2]scale=1920:540[m];"
                 "[c]crop=1280:540:0:%d[pl];[d]crop=640:540:0:%d[pr];" % (2 * H, 2 * H + 540)
                 + "[pl][pr]hstack=inputs=2[p];"
                 "[e]crop=%d:%d:0:%d[tl];[f]crop=%d:%d:0:%d,format=gray[al];[tl][al]alphamerge[ol];"
                 % (TGW, TH, t0, TGW, TH, t0 + TH)
                 + "[g]crop=%d:%d:%d:%d[tr];[h]crop=%d:%d:%d:%d,format=gray[ar];[tr][ar]alphamerge[or];"
                 % (TGW, TH, W // 2, t0, TGW, TH, W // 2, t0 + TH)
                 + "[m][ol]overlay=%d:%d[m3];[m3][or]overlay=%d:%d[m4];" % (gx, gy, 960 + gx, gy)
                 + "[m4][p]vstack=inputs=2," + up + "[t]")
        if HW:
            pre = ["-init_hw_device", "vaapi=va:" + DRI, "-filter_hw_device", "va"]
            enc = ["-c:v", "h264_vaapi", "-profile:v", "high", "-b:v", "3M", "-maxrate", "4M",
                   "-g", "30", "-an"]
        else:
            pre = []
            enc = ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                   "-b:v", "3M", "-maxrate", "4M", "-bufsize", "4M", "-g", "30", "-an"]
        c = [FFMPEG, "-hide_banner", "-loglevel", E("ENCODER_LOGLEVEL", "warning"), "-nostdin"] + pre
        c += ["-f", "rawvideo", "-pix_fmt", "yuv420p", "-video_size", "%dx%d" % (W, 2 * H + PH + TBH),
              "-framerate", str(FPS), "-i", "pipe:0", "-filter_complex", graph]
        for label, path in (("[v]", "mosaico"), ("[t]", "mosaico-tv")):
            c += ["-map", label] + enc + ["-rtsp_transport", "tcp", "-f", "rtsp", OUT + "/" + path]
        return c

    def drop(self, why, now):
        p, self.proc, self.fd = self.proc, None, -1
        if p is None:
            return
        log("encoder: %s - restarting it" % why)
        if not reap(p, 5):
            self.stuck = p
        try:
            p.stdin.close()
        except OSError:
            pass
        self.next_spawn = now + 5

    def ensure(self, now):
        if self.proc is not None:
            if self.kill_reason:
                self.drop(self.kill_reason, now)
            elif self.proc.poll() is not None:
                self.drop("exited with code %s" % self.proc.returncode, now)
        self.kill_reason = None
        if self.proc is not None or now < self.next_spawn:
            return
        if self.stuck is not None:
            if self.stuck.poll() is None:
                if now - self.said_stuck > 600:
                    self.said_stuck = now
                    log("encoder: pid %d survived SIGKILL - not starting another while it lives "
                        "(GPU/driver?)" % self.stuck.pid)
                self.next_spawn = now + 10
                return
            self.stuck = None
        try:
            p = subprocess.Popen(self.cmd(), stdin=subprocess.PIPE, bufsize=0)
        except OSError as e:
            log("encoder: cannot start: %s" % e)
            self.next_spawn = now + 10
            return
        self.fd = p.stdin.fileno()
        grow_pipe(self.fd)
        os.set_blocking(self.fd, False)
        self.proc, self.started = p, now
        self.restarts += 1

    def write(self, bufs, now):
        """One raw frame to the encoder; it may take 3 s at most."""
        if self.proc is None:
            return
        deadline = mono() + 3
        try:
            for b in bufs:
                mv = memoryview(b)
                while mv:
                    try:
                        mv = mv[os.write(self.fd, mv):]
                    except BlockingIOError:
                        left = deadline - mono()
                        if left <= 0 or not select.select([], [self.fd], [], left)[1]:
                            self.drop("did not read its input for 3 s", now)
                            return
        except OSError as e:
            self.drop("input pipe closed (%s)" % e.strerror, now)

    def publishers(self):
        p = self.proc
        if p is None:
            return None
        try:
            out = subprocess.run(["ss", "-Htnp", "state", "established", "dst", self.dst],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=10).stdout.decode("ascii", "replace")
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.count("pid=%d," % p.pid)


def grey_once(lenses):
    """Shares of decoder-grey cells per lens, from the frames in hand (None = no verdict)."""
    planes = []
    for l in lenses:
        planes.append(l.raw if l.raw is not None else BLANK)
    tile = b"".join(bytes(planes[i][k]) for k in range(3) for i in range(2))
    try:
        r = subprocess.run(
            [FFMPEG, "-v", "error", "-nostdin", "-f", "rawvideo", "-pix_fmt", "yuv420p",
             "-video_size", "%dx%d" % (W, 2 * H), "-i", "pipe:0", "-filter_complex",
             "[0:v]split=2[a][b];[a]crop=%d:%d:0:0[l];[b]crop=%d:%d:0:%d[r];"
             "[l][r]hstack=inputs=2,scale=640:180" % (W, H, W, H, H),
             "-frames:v", "1", "-pix_fmt", "yuv444p", "-f", "rawvideo", "-"],
            input=tile, stdout=subprocess.PIPE, timeout=30)
        c = subprocess.run([sys.executable, CINZA], input=r.stdout, stdout=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"fixa=([\d.]+) pt=([\d.]+)", c.stdout.decode("ascii", "replace"))
    return (float(m.group(1)), float(m.group(2))) if m else None


def watch_loop(lenses, enc, panel, stats, thumbs):
    """Every 5 s: the encoder's publisher connections and the state file. Every 30 s: grey."""
    bad = 0
    tick = 0
    grey_log = os.path.join(RUN, "cinza-restarts")
    while not STOP.wait(5):
        tick += 1
        now = mono()
        n = enc.publishers()
        if n is not None and now - enc.started > 20:
            bad = 0 if n >= 2 else bad + 1
            if bad >= 2:
                bad = 0
                enc.kill_reason = "%d of 2 publisher connections for 10 s" % n
        else:
            bad = 0
        if tick % 6 == 0 and os.path.isfile(CINZA):
            live = [l.live for l in lenses]
            shares = grey_once(lenses) if any(live) else None
            if shares:
                for l, was_live, s in zip(lenses, live, shares):
                    l.grey = l.grey + 1 if (was_live and l.live and s >= GREY_LIMIT) else 0
                    if l.grey < 3:
                        continue
                    t = time.time()
                    try:
                        recent = [x for x in open(grey_log).read().split() if float(x) > t - 3600]
                    except (OSError, ValueError):
                        recent = []
                    if len(recent) < GREY_MAX:
                        write_atomic(grey_log, ("\n".join(recent + ["%d" % t]) + "\n").encode())
                        log("lens %s: grey for 90 s (%.2f of its cells) - restarting its feeder"
                            % (l.name, s))
                        l.grey = 0
                        l.kill_feeder()
                    elif l.grey == 3:
                        log("lens %s: grey (%.2f) but %d restarts in the last hour - leaving it"
                            % (l.name, s, GREY_MAX))
        state = {
            "ts": int(time.time()), "uptime_s": int(now - stats["t0"]), "frames": stats["frames"],
            "encoder": {"up": enc.proc is not None, "publishers": n, "starts": enc.restarts + 1,
                        "stuck": enc.stuck is not None},
            "painel_age_s": None if panel.ok_wall is None else int(time.time() - panel.ok_wall),
            "miniaturas": {"slots": sum(1 for x in thumbs.have if x),
                           "age_s": None if thumbs.ok_wall is None else int(time.time() - thumbs.ok_wall),
                           "erro": thumbs.said},
            "lentes": {l.name: {
                "live": l.live, "queue": len(l.q), "feeder_up": l.proc is not None,
                "feeder_exits": l.restarts, "stuck": l.stuck is not None,
                "last_frame_age_s": None if l.new_mono is None else round(now - l.new_mono, 1),
                "fonte": "sub" if l.alt_on else "principal", "sub_feeder_up": l.alt_proc is not None,
                "sub_vezes": l.alt_shows,
                "label": l.label.strip()} for l in lenses},
        }
        try:
            write_atomic(os.path.join(RUN, "mosaico-estado.json"), json.dumps(state).encode())
        except OSError:
            pass


def main():
    os.makedirs(RUN, exist_ok=True)
    lenses = [Lens(n, u) for n, u in SRC]
    panel = Panel()
    enc = Encoder(lenses)
    thumbs = Thumbs()
    stats = {"t0": mono(), "frames": 0}

    def stop(*_):
        STOP.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    threads = [threading.Thread(target=l.feeder_loop, daemon=True) for l in lenses]
    threads += [threading.Thread(target=l.alt_loop, daemon=True) for l in lenses if l.alt_url]
    threads.append(threading.Thread(target=panel.loop, daemon=True))
    threads.append(threading.Thread(target=watch_loop, args=(lenses, enc, panel, stats, thumbs), daemon=True))
    if MINIATURAS_URL:
        threads.append(threading.Thread(target=thumbs.poll_loop, daemon=True))
        threads.append(threading.Thread(target=thumbs.anim_loop, daemon=True))
    for t in threads:
        t.start()
    log("pump up: %s | %s -> %s/mosaico(-tv), %s" % (SRC[0][1], SRC[1][1], OUT,
                                                    "vaapi" if HW else "software"))
    nxt = mono()
    try:
        while not STOP.is_set():
            now = mono()
            if now < nxt:
                time.sleep(nxt - now)
                now = mono()
            elif now - nxt > 0.5:        # we were held up: do not burst, just carry on from here
                nxt = now
            nxt += 1.0 / FPS
            a = lenses[0].tick(now)
            b = lenses[1].tick(now)
            p = panel.planes
            t = thumbs.planes
            enc.ensure(now)
            enc.write((a[0], b[0], p[0], t[0], a[1], b[1], p[1], t[1], a[2], b[2], p[2], t[2]), now)
            stats["frames"] += 1
    finally:
        STOP.set()
        for l in lenses:
            l.kill_feeder()
            l.kill_alt()
        if enc.proc is not None:
            reap(enc.proc, 3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
