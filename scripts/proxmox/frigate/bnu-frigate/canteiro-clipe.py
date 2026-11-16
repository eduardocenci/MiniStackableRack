#!/usr/bin/env python3
"""canteiro-clipe — rolling buffer of the canteiro MOSAIC + clips on demand.

Authoritative copy: scripts/proxmox/frigate/bnu-frigate/canteiro-clipe.py
Live copy:          bnu Frigate LXC 105:/usr/local/bin/canteiro-clipe.py
                    (unit: canteiro-clipe.service, same folder)

Why: the canteiro-vigia (bnu-raspberrypi) sends, after the photo, a VIDEO of
each detection stretch in the same layout as the live mosaic [fixed lens | PT
lens] (pedido Eduardo 2026-11-14). Frigate's recordings cannot give that: they
are kept by motion/event per lens (~2-3 h/day each), so the lens that did not
see the event often has nothing for that minute — and composing two HEVC
2304x1296 clips on the Pi would take minutes. So this service keeps the mosaic
that canteiro-mosaico.service already encodes:

  1. recorder: one ffmpeg, `-c copy` (no decode, ~0 % CPU), reads
     rtsp://10.1.1.123:8555/mosaico (canteiro-hls on bnu-raspberrypi) and
     writes 10 s MPEG-TS segments named <epoch>.ts into BUF_DIR; restarted
     5 s after any exit; segments older than KEEP_MIN are deleted.
     ~3 Mbps -> ~22 MB/min -> ~1.4 GB for the default 60 min.
  2. HTTP (PORT, only ALLOW clients): GET /clip.mp4?start=<epoch>&end=<epoch>
     -> the segments covering the window, trimmed and re-encoded on the GPU
     (VAAPI) to H.264 OUT_W x OUT_H at a bitrate that keeps the file under
     MAX_MB (WhatsApp) — a 2 min clip takes a few seconds. 404 when the buffer
     has nothing for the window (composer down, window older than KEEP_MIN).
     GET /health -> JSON (segments, oldest/newest epoch, recorder alive).
  3. FALLBACKS (2026-11-15/16): the buffer only has what the live mosaic
     published. On 2026-11-15 12:06 BRT the vigia asked for 92 s around cars
     in front of the site and the group got a 4 s video: one second into the
     event the fixed lens' MAIN stream stopped delivering video (audio kept
     coming for 113 s), the composer stalled, exited and stayed out for over a
     minute. Frigate's own recording of that lens has the same hole - it is
     the same stream. What never stopped was the SUB stream (640x480, the one
     Frigate detects on - that is why the events kept coming). So:
       a. sub recorders: one more `ffmpeg -c copy` per lens (SUBS, from
          Frigate's go2rtc on 127.0.0.1:8554 - no extra pull from the site)
          into BUF_DIR/sub-<lens>/<epoch>.ts, same KEEP_MIN (~10 MB/min for
          the two together);
       b. when the mosaic clip covers less than COVER_MIN of the window, the
          same window is cut from the lenses' Frigate recordings
          (REC_DIR/<UTC date>/<hour>/<camera>/MM.SS.mp4 - main streams, full
          quality, only when motion/event kept them) and, if that is short
          too, from the sub buffers - always in the mosaic layout
          [fixed | PT], or the one lens that has footage. Coverage counts
          PER LENS (video seconds inside the window, from the files'
          metadata): the first source where every lens covers the window
          goes out; none does -> the one with most lens-seconds. (A source
          that only has the lens that saw nothing must not beat one that
          has both.)
     Response headers say what was sent: X-Clip-Fonte (mosaico | gravacao |
     sub), X-Clip-Lentes (fixa,pt), X-Clip-Segundos (real length),
     X-Clip-Pedido (asked length) - the vigia writes the caption from them.
     `canteiro-clipe.py --clip <start> <end> <out.mp4>` cuts one clip without
     the server (diagnosis, re-sending a clip by hand).

Segment names are the LXC's wall clock at the moment the segment was opened;
the mosaic runs ~2-4 s behind the camera, so callers pad the window.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

E = lambda k, d: os.environ.get(k, d).strip()  # noqa: E731
FFMPEG = E("FFMPEG", "/usr/lib/ffmpeg/7.0/bin/ffmpeg")
SRC = E("SRC", "rtsp://10.1.1.123:8555/mosaico")
BUF_DIR = Path(E("BUF_DIR", "/var/lib/canteiro-clipe"))
KEEP_MIN = int(E("KEEP_MIN", "60"))
SEG_S = int(E("SEG_S", "10"))
PORT = int(E("PORT", "8556"))
ALLOW = {x.strip() for x in E("ALLOW", "127.0.0.1,10.1.1.123").split(",") if x.strip()}
OUT_W, OUT_H, OUT_FPS = int(E("OUT_W", "1920")), int(E("OUT_H", "540")), int(E("OUT_FPS", "15"))
MAX_MB = float(E("MAX_MB", "14"))
MAX_CLIP_S = int(E("MAX_CLIP_S", "600"))
VAAPI = E("VAAPI", "/dev/dri/renderD128")
FFPROBE = E("FFPROBE", str(Path(FFMPEG).with_name("ffprobe")))
REC_DIR = Path(E("REC_DIR", "/media/frigate/recordings"))
# left | right of the mosaic: Frigate camera=name in X-Clip-Lentes
LENSES = [tuple(x.split("=")) for x in E("LENSES", "canteiro_fixa=fixa,canteiro=pt").split(",") if "=" in x]
COVER_MIN = float(E("COVER_MIN", "0.85"))   # clip shorter than this share of the window -> try the next source
# lens name=RTSP of its SUB stream (Frigate's go2rtc); "" turns the sub buffers off
SUBS = [tuple(x.split("=", 1)) for x in E(
    "SUBS", "fixa=rtsp://127.0.0.1:8554/canteiro_fixa_sub,pt=rtsp://127.0.0.1:8554/canteiro_sub").split(",")
    if "=" in x]

_rec = {"proc": None, "starts": 0}   # the mosaic recorder
_sub = {n: {"proc": None, "starts": 0} for n, _ in SUBS}
_enc = threading.Lock()   # one encode at a time


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def sub_dir(nome):
    return BUF_DIR / f"sub-{nome}"


def recorder(src=SRC, pasta=BUF_DIR, st=_rec, nome="mosaico"):
    pasta.mkdir(parents=True, exist_ok=True)
    while True:
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "warning" if st is _rec else "error", "-nostdin",
               "-rtsp_transport", "tcp", "-timeout", "15000000", "-i", src,
               "-map", "0:v", "-c", "copy", "-f", "segment", "-segment_time", str(SEG_S),
               "-segment_format", "mpegts", "-reset_timestamps", "1", "-strftime", "1",
               str(pasta / "%s.ts")]
        st["starts"] += 1
        st["proc"] = subprocess.Popen(cmd)
        rc = st["proc"].wait()
        log(f"recorder {nome} saiu (rc={rc}) — de novo em 5 s" if st is not _rec
            else f"recorder saiu (rc={rc}) — de novo em 5 s")
        time.sleep(5)


def segments(pasta=BUF_DIR):
    out = []
    for p in pasta.glob("*.ts"):
        try:
            out.append((int(p.stem), p))
        except ValueError:
            pass
    return sorted(out)


def cobrem(segs, start, end, minimo=2000):
    """[(epoch, seconds, path)] of the buffer segments that overlap [start, end].

    A segment runs until the next one starts (SEG_S when the next one is missing or late).
    """
    out = []
    for i, (t, p) in enumerate(segs):
        d = min(segs[i + 1][0] - t, SEG_S + 5) if i + 1 < len(segs) else SEG_S
        try:
            if t < end and t + d > start and p.stat().st_size > minimo:
                out.append((t, d, p))
        except OSError:
            pass
    return out


def cobertura(fs, start, end):
    """Seconds of [start, end] that the files [(epoch, seconds, path)] have footage for."""
    return sum(max(0.0, min(t + d, end) - max(t, start)) for t, d, _ in fs)


def janitor():
    while True:
        lim = time.time() - KEEP_MIN * 60
        for pasta in [BUF_DIR] + [sub_dir(n) for n, _ in SUBS]:
            for t, p in segments(pasta):
                if t < lim:
                    try:
                        p.unlink()
                    except OSError:
                        pass
        time.sleep(60)


def probe_s(path, video=False):
    """Duration in seconds of a media file - of its VIDEO stream when video=True (0.0 when unreadable).

    The two differ when it matters: Frigate's file of 2026-11-15 12:06 BRT has 113 s of audio
    and 1.5 s of video.
    """
    sel = ["-select_streams", "v:0", "-show_entries", "stream=duration"] if video else \
        ["-show_entries", "format=duration"]
    try:
        r = subprocess.run([FFPROBE, "-v", "error", *sel, "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip().split("\n")[0].strip(",") or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


def _encode(inputs, vf, dur, out, complex_=False):
    """One VAAPI H.264 encode under MAX_MB; returns (bytes | None, kbps, seconds it took)."""
    kbps = int(max(500, min(1200, MAX_MB * 8000 / dur * 0.94)))
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-init_hw_device", f"vaapi=va:{VAAPI}", "-filter_hw_device", "va", *inputs,
           *(["-filter_complex", vf, "-map", "[o]"] if complex_ else ["-vf", vf]),
           "-t", f"{dur:.2f}",
           "-c:v", "h264_vaapi", "-profile:v", "high", "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.3)}k",
           "-g", str(OUT_FPS * 2), "-an", "-movflags", "+faststart", str(out)]
    with _enc:
        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not out.exists() or out.stat().st_size < 10000:
        log(f"encode falhou rc={r.returncode}: {r.stderr[-400:]}")
        return None, kbps, time.time() - t0
    return out.read_bytes(), kbps, time.time() - t0


def clip_mosaico(start, end, td):
    """(bytes, real seconds) from the mosaic buffer, or (None, 0)."""
    segs = segments()
    pick = [(t, p) for i, (t, p) in enumerate(segs)
            if t < end and (segs[i + 1][0] if i + 1 < len(segs) else t + SEG_S + 5) > start
            and p.stat().st_size > 20000]
    if not pick:
        return None, 0.0
    ss = max(0.0, start - pick[0][0])
    dur = end - max(start, pick[0][0])
    if dur < 1:
        return None, 0.0
    lst = Path(td) / "m.txt"
    lst.write_text("".join(f"file '{p}'\n" for _, p in pick))
    out = Path(td) / "m.mp4"
    data, kbps, took = _encode(
        ["-hwaccel", "vaapi", "-hwaccel_device", "va", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-ss", f"{ss:.2f}"],
        f"fps={OUT_FPS},scale={OUT_W}:{OUT_H},format=nv12,hwupload", dur, out)
    if not data:
        return None, 0.0
    real = probe_s(out)
    log(f"mosaico {start:.0f}-{end:.0f} ({dur:.0f} s pedidos, {real:.0f} s reais, {len(pick)} seg, {kbps} kbps) -> "
        f"{len(data) / 1e6:.1f} MB em {took:.1f} s")
    return data, real


def rec_files(cam, start, end):
    """[(epoch, seconds, path)] of Frigate's recording files of `cam` that overlap [start, end]."""
    cand = []
    h = int(start - 900) // 3600 * 3600
    while h < end:
        d = REC_DIR / time.strftime("%Y-%m-%d/%H", time.gmtime(h)) / cam
        for p in d.glob("*.mp4"):
            try:
                mm, sec = p.stem.split(".")
                cand.append((h + int(mm) * 60 + int(sec), p))
            except ValueError:
                pass
        h += 3600
    out = []
    for t, p in sorted(cand):
        if t >= end or t < start - 900:
            continue
        d = probe_s(p, video=True)
        if d > 0 and t + d > start:
            out.append((t, d, p))
    return out


def compor(lentes, start, end, td, fonte):
    """(bytes, real seconds) - the window in the mosaic layout from per-lens file lists.

    lentes = [(lens name, [(epoch the file starts, seconds, path), ...])] in mosaic order, only
    lenses with files.
    """
    start = max(start, min(fs[0][0] for _, fs in lentes))   # no lens has the head of the window: skip it
    dur = end - start
    if dur < 1:
        return None, 0.0
    w, h = (OUT_W // len(lentes), OUT_H) if len(lentes) > 1 else (1280, 720)
    inputs, chains = [], []
    for i, (nome, fs) in enumerate(lentes):
        lst = Path(td) / f"{fonte}{i}.txt"
        lst.write_text("".join(f"file '{p}'\n" for _, _, p in fs))
        ss = max(0.0, start - fs[0][0])
        lead = max(0.0, fs[0][0] - start)      # this lens only has footage `lead` s into the window
        inputs += ["-hwaccel", "vaapi", "-hwaccel_device", "va", "-f", "concat", "-safe", "0",
                   "-ss", f"{ss:.2f}", "-i", str(lst)]
        pad = f",tpad=start_duration={lead:.2f}:color=black" if lead >= 0.5 else ""
        chains.append(f"[{i}:v]fps={OUT_FPS},scale={w}:{h},setsar=1{pad}[v{i}]")
    if len(lentes) > 1:
        vf = ";".join(chains) + ";" + "".join(f"[v{i}]" for i in range(len(lentes))) + \
            f"hstack=inputs={len(lentes)},format=nv12,hwupload[o]"
    else:
        vf = chains[0].replace("[v0]", ",format=nv12,hwupload[o]")
    out = Path(td) / f"{fonte}.mp4"
    data, kbps, took = _encode(inputs, vf, dur, out, complex_=True)
    if not data:
        return None, 0.0
    real = probe_s(out)
    log(f"{fonte} {start:.0f}-{end:.0f} ({dur:.0f} s pedidos, {real:.0f} s reais, "
        f"lentes {'+'.join(n for n, _ in lentes)}, {kbps} kbps) -> {len(data) / 1e6:.1f} MB em {took:.1f} s")
    return data, real


def lentes_gravacao(start, end):
    """Per-lens files of the window in the lenses' own Frigate recordings (main streams)."""
    return [(nome, fs) for nome, fs in ((nome, rec_files(cam, start, end)) for cam, nome in LENSES) if fs]


def lentes_sub(start, end):
    """Per-lens files of the window in the sub-stream buffers (640x480 - what Frigate detects on)."""
    return [(nome, fs) for nome, fs in
            ((nome, cobrem(segments(sub_dir(nome)), start, end)) for nome, _ in SUBS) if fs]


def clip(start, end):
    """(mp4 bytes, info) for [start, end], or (None, {}) when no source has footage for it.

    The mosaic buffer goes out when it covers COVER_MIN of the window. Otherwise the Frigate
    recordings and the sub buffers are weighed by lens-seconds of footage (see the docstring, 3b)
    and the winner is encoded - if it has more than the mosaic clip did.
    """
    pedido = end - start
    n = max(1, len(LENSES))
    with tempfile.TemporaryDirectory(dir=BUF_DIR) as td:
        data, real = clip_mosaico(start, end, td)
        info = {"fonte": "mosaico", "lentes": [x for _, x in LENSES], "segundos": real, "pedido": pedido}
        if data and real >= pedido * COVER_MIN:
            return data, info
        cands = []                                     # (lens-seconds, source, lenses)
        for fonte, f in (("gravacao", lentes_gravacao), ("sub", lentes_sub)):
            try:
                lentes = f(start, end)
            except Exception as ex:  # noqa: BLE001 - a fallback never takes the mosaic clip down with it
                log(f"{fonte} erro: {type(ex).__name__}: {ex}")
                continue
            if lentes:
                cands.append((sum(cobertura(fs, start, end) for _, fs in lentes), fonte, lentes))
        cheios = [c for c in cands if c[0] >= n * pedido * COVER_MIN]
        ordem = cheios[:1] + sorted((c for c in cands if c not in cheios[:1]), key=lambda c: -c[0])
        for seg_lente, fonte, lentes in ordem:
            if seg_lente <= n * real + 4:              # no more footage than the mosaic clip already has
                break
            try:
                d, r = compor(lentes, start, end, td, fonte)
            except Exception as ex:  # noqa: BLE001
                log(f"{fonte} erro: {type(ex).__name__}: {ex}")
                continue
            if d:
                seg = min(r, max(cobertura(fs, start, end) for _, fs in lentes))
                log(f"clip {start:.0f}-{end:.0f}: mosaico cobre {real:.0f} de {pedido:.0f} s -> {fonte} "
                    f"({seg:.0f} s, {'+'.join(x for x, _ in lentes)})")
                return d, {"fonte": fonte, "lentes": [x for x, _ in lentes], "segundos": seg, "pedido": pedido}
        if data:
            log(f"clip {start:.0f}-{end:.0f}: mosaico cobre {real:.0f} de {pedido:.0f} s e nenhuma outra fonte tem mais")
        return (data, info) if data else (None, {})


def sub_health(nome):
    segs, p = segments(sub_dir(nome)), _sub[nome]["proc"]
    return {"segmentos": len(segs), "mais_antigo": segs[0][0] if segs else None,
            "mais_novo": segs[-1][0] if segs else None,
            "recorder_vivo": bool(p and p.poll() is None), "recorder_partidas": _sub[nome]["starts"]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.client_address[0] not in ALLOW:
            return self._send(403, b'{"erro":"cliente nao permitido"}')
        u = urlparse(self.path)
        if u.path == "/health":
            segs = segments()
            p = _rec["proc"]
            return self._send(200, json.dumps({
                "segmentos": len(segs), "mais_antigo": segs[0][0] if segs else None,
                "mais_novo": segs[-1][0] if segs else None, "agora": int(time.time()),
                "recorder_vivo": bool(p and p.poll() is None), "recorder_partidas": _rec["starts"],
                "sub": {n: sub_health(n) for n, _ in SUBS}}).encode())
        if u.path == "/clip.mp4":
            q = parse_qs(u.query)
            try:
                start, end = float(q["start"][0]), float(q["end"][0])
            except (KeyError, ValueError):
                return self._send(400, b'{"erro":"start/end (epoch)"}')
            if not 1 <= end - start <= MAX_CLIP_S:
                return self._send(400, b'{"erro":"janela invalida"}')
            try:
                data, info = clip(start, end)
            except Exception as ex:  # noqa: BLE001
                log(f"clip erro: {type(ex).__name__}: {ex}")
                data, info = None, {}
            if not data:
                return self._send(404, b'{"erro":"sem mosaico, gravacao nem sub para a janela"}')
            return self._send(200, data, "video/mp4", {
                "X-Clip-Fonte": info["fonte"], "X-Clip-Lentes": ",".join(info["lentes"]),
                "X-Clip-Segundos": f"{info['segundos']:.1f}", "X-Clip-Pedido": f"{info['pedido']:.1f}"})
        return self._send(404, b'{"erro":"rota"}')


if __name__ == "__main__":
    if sys.argv[1:2] == ["--clip"]:      # --clip <start> <end> <out.mp4>: one clip, no server, no recorder
        d, i = clip(float(sys.argv[2]), float(sys.argv[3]))
        if d:
            Path(sys.argv[4]).write_bytes(d)
        print(json.dumps(i) if d else "sem video para a janela")
        sys.exit(0 if d else 1)
    threading.Thread(target=recorder, daemon=True).start()
    for n, url in SUBS:
        threading.Thread(target=recorder, args=(url, sub_dir(n), _sub[n], f"sub-{n}"), daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()
    log(f"canteiro-clipe :{PORT} src={SRC} buf={BUF_DIR} keep={KEEP_MIN}min allow={sorted(ALLOW)} sub={[n for n, _ in SUBS]}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
