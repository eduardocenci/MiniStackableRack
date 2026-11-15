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

Segment names are the LXC's wall clock at the moment the segment was opened;
the mosaic runs ~2-4 s behind the camera, so callers pad the window.
"""
import json
import os
import subprocess
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

_rec = {"proc": None, "starts": 0}
_enc = threading.Lock()   # one encode at a time


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def recorder():
    BUF_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostdin",
               "-rtsp_transport", "tcp", "-timeout", "15000000", "-i", SRC,
               "-map", "0:v", "-c", "copy", "-f", "segment", "-segment_time", str(SEG_S),
               "-segment_format", "mpegts", "-reset_timestamps", "1", "-strftime", "1",
               str(BUF_DIR / "%s.ts")]
        _rec["starts"] += 1
        _rec["proc"] = subprocess.Popen(cmd)
        rc = _rec["proc"].wait()
        log(f"recorder saiu (rc={rc}) — de novo em 5 s")
        time.sleep(5)


def segments():
    out = []
    for p in BUF_DIR.glob("*.ts"):
        try:
            out.append((int(p.stem), p))
        except ValueError:
            pass
    return sorted(out)


def janitor():
    while True:
        lim = time.time() - KEEP_MIN * 60
        for t, p in segments():
            if t < lim:
                try:
                    p.unlink()
                except OSError:
                    pass
        time.sleep(60)


def clip(start, end):
    """bytes of the mp4 for [start, end] or None when the buffer has nothing there."""
    segs = segments()
    pick = [(t, p) for i, (t, p) in enumerate(segs)
            if t < end and (segs[i + 1][0] if i + 1 < len(segs) else t + SEG_S + 5) > start
            and p.stat().st_size > 20000]
    if not pick:
        return None
    ss = max(0.0, start - pick[0][0])
    dur = end - max(start, pick[0][0])
    if dur < 1:
        return None
    kbps = int(max(500, min(1200, MAX_MB * 8000 / dur * 0.94)))
    with tempfile.TemporaryDirectory(dir=BUF_DIR) as td:
        lst = Path(td) / "l.txt"
        lst.write_text("".join(f"file '{p}'\n" for _, p in pick))
        out = Path(td) / "o.mp4"
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-init_hw_device", f"vaapi=va:{VAAPI}", "-filter_hw_device", "va",
               "-hwaccel", "vaapi", "-hwaccel_device", "va",
               "-f", "concat", "-safe", "0", "-i", str(lst),
               "-ss", f"{ss:.2f}", "-t", f"{dur:.2f}",
               "-vf", f"fps={OUT_FPS},scale={OUT_W}:{OUT_H},format=nv12,hwupload",
               "-c:v", "h264_vaapi", "-profile:v", "high", "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.3)}k",
               "-g", str(OUT_FPS * 2), "-an", "-movflags", "+faststart", str(out)]
        with _enc:
            t0 = time.time()
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not out.exists() or out.stat().st_size < 10000:
            log(f"clip {start:.0f}-{end:.0f} falhou rc={r.returncode}: {r.stderr[-400:]}")
            return None
        data = out.read_bytes()
        log(f"clip {start:.0f}-{end:.0f} ({dur:.0f} s, {len(pick)} seg, {kbps} kbps) -> "
            f"{len(data) / 1e6:.1f} MB em {time.time() - t0:.1f} s")
        return data


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
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
                "recorder_vivo": bool(p and p.poll() is None), "recorder_partidas": _rec["starts"]}).encode())
        if u.path == "/clip.mp4":
            q = parse_qs(u.query)
            try:
                start, end = float(q["start"][0]), float(q["end"][0])
            except (KeyError, ValueError):
                return self._send(400, b'{"erro":"start/end (epoch)"}')
            if not 1 <= end - start <= MAX_CLIP_S:
                return self._send(400, b'{"erro":"janela invalida"}')
            try:
                data = clip(start, end)
            except Exception as ex:  # noqa: BLE001
                log(f"clip erro: {type(ex).__name__}: {ex}")
                data = None
            if not data:
                return self._send(404, b'{"erro":"sem mosaico no buffer para a janela"}')
            return self._send(200, data, "video/mp4")
        return self._send(404, b'{"erro":"rota"}')


if __name__ == "__main__":
    threading.Thread(target=recorder, daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()
    log(f"canteiro-clipe :{PORT} src={SRC} buf={BUF_DIR} keep={KEEP_MIN}min allow={sorted(ALLOW)}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
