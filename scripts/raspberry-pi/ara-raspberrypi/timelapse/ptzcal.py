#!/usr/bin/env python3
"""Calibracao dos fins de curso da lente PT (roda DENTRO do canteiro-timelapse).

  ptzcal.py tilt  <home_vy> <home_s> <step_vy> <step_s> <max>   fim de curso de tilt -> passos de volta
  ptzcal.py pan   <home_vx> <home_s> <step_vx> <step_s> <max>   fim de curso de pan  -> passos de volta
  ptzcal.py push  <vx> <vy> <s>                                   empurrao extra no fim de curso + deslocamento global
  ptzcal.py steps <vx> <vy> <s> <n> <tag>                         n bursts, mede apos cada um
  ptzcal.py snap  <tag>                                           so mede
  ptzcal.py home                                                  executa a receita HOME_* / TRAVEL_* do script
  ptzcal.py reanchor [home]                                       cmd_reanchor completo, sem alerta

Cada passo grava /tmp/cal/<tag>.jpg e loga a medicao valida + a crua por ref.
TC=<caminho> carrega outro timelapse-capture (ex.: um patch em /tmp) no lugar
do da imagem. MEXE A CAMERA: so fora de 05:00-07:00 / 16:40-19:30 e com o
canteiro vazio (o auto-tracking leva a lente atras de quem passar).
Uso (recalibracao de 10/11/2026, ver README):
  docker cp ptzcal.py canteiro-timelapse:/tmp/ptzcal.py
  docker exec canteiro-timelapse python3 /tmp/ptzcal.py tilt 1.0 12 -0.4 0.3 6
  docker exec canteiro-timelapse python3 /tmp/ptzcal.py pan 1.0 25 -0.4 0.5 8
Leitura: confira os quadros /tmp/cal/*.jpg a olho — longe da guarda as refs
podem concordar num falso (artefato do OSD, alias com outro pilar em Y).
"""
import glob
import os
import sys
import time
from importlib.machinery import SourceFileLoader

tc = SourceFileLoader("tc", os.environ.get("TC", "/usr/local/bin/timelapse-capture")).load_module()
OUT = "/tmp/cal"
os.makedirs(OUT, exist_ok=True)


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def snap(tag):
    p = f"{OUT}/{tag}.jpg"
    if not tc._grab_abs(tc.RELAY_PT, p, tries=4):
        log(f"{tag}: GRAB FALHOU")
        return None
    m = tc._measure_valid(p)
    raw = []
    for ref in sorted(glob.glob(os.path.join(tc.REF_DIR, "posicao1-*.jpg"))):
        sx, sy, pk, psr = tc._measure_offset(ref, p)
        raw.append(f"{os.path.basename(ref)[9:-4]}({sx:+d},{sy:+d},{psr:.0f})")
    v = f"({m[0]:+d},{m[1]:+d}) psr={m[2]:.1f}" if m else "INVALIDA"
    log(f"{tag}: {v}  raw: {' '.join(raw)}")
    return m


def global_shift(a, b):
    """Deslocamento global (px full-res) de b em relacao a a — quadro inteiro."""
    import numpy as np
    from PIL import Image

    def prep(p):
        im = Image.open(p).convert("L")
        im = im.resize((im.width // 4, im.height // 4))
        x = np.asarray(im, dtype=np.float64)
        gy, gx = np.gradient(x)
        g = np.hypot(gx, gy)
        return g - g.mean()

    A, B = prep(a), prep(b)
    R = np.fft.fft2(B) * np.conj(np.fft.fft2(A))
    R /= np.abs(R) + 1e-9
    r = np.real(np.fft.ifft2(R))
    py, px = np.unravel_index(np.argmax(r), r.shape)
    if py > r.shape[0] // 2:
        py -= r.shape[0]
    if px > r.shape[1] // 2:
        px -= r.shape[1]
    return px * 4, py * 4


def axis(name, hv, hs, sv, ss, mx):
    pan = name == "pan"
    snap(f"{name}_00_start")
    tc.ptz_move(hv if pan else 0, 0 if pan else hv, hs)
    time.sleep(2)
    snap(f"{name}_01_limit")
    hist = []
    for i in range(1, mx + 1):
        tc.ptz_move(sv if pan else 0, 0 if pan else sv, ss)
        time.sleep(1 + tc.SETTLE_S)
        m = snap(f"{name}_step{i:02d}")
        if m:
            off = m[0] if pan else m[1]
            hist.append((i, off))
            # passou da guarda (sinal trocou) ou ja bem perto: para
            if hist and len(hist) >= 2 and (off < 0) != (hist[-2][1] < 0):
                break
            if abs(off) <= tc.REANCHOR_TOL_PX:
                # mede mais um passo para ver o tamanho do passo perto da guarda
                continue
    log(f"{name}: historico {hist}")


def push(vx, vy, s):
    a = f"{OUT}/push_a.jpg"
    b = f"{OUT}/push_b.jpg"
    tc._grab_abs(tc.RELAY_PT, a, tries=4)
    tc.ptz_move(vx, vy, s)
    time.sleep(2)
    tc._grab_abs(tc.RELAY_PT, b, tries=4)
    log(f"push ({vx},{vy},{s}): deslocamento global {global_shift(a, b)}")


def main():
    cmd = sys.argv[1]
    a = sys.argv[2:]
    if cmd in ("tilt", "pan"):
        axis(cmd, float(a[0]), float(a[1]), float(a[2]), float(a[3]), int(a[4]))
    elif cmd == "push":
        push(float(a[0]), float(a[1]), float(a[2]))
    elif cmd == "steps":   # steps <vx> <vy> <s> <n> <tag>: n bursts, mede apos cada
        for i in range(1, int(a[3]) + 1):
            tc.ptz_move(float(a[0]), float(a[1]), float(a[2]))
            time.sleep(1 + tc.SETTLE_S)
            snap(f"{a[4]}_{i:02d}")
    elif cmd == "snap":
        snap(a[0] if a else "snap")
    elif cmd == "home":
        t0 = time.time()
        m = tc._home_and_recover("[cal]")
        log(f"home: {m} em {time.time() - t0:.0f}s")
        snap(f"home_{int(time.time())}")
    elif cmd == "reanchor":   # reanchor [home]: cmd_reanchor do modulo carregado, sem alerta
        t0 = time.time()
        rc = tc.cmd_reanchor(home="home" in a, tag="[cal]")
        log(f"reanchor rc={rc} em {time.time() - t0:.0f}s falhas={tc.FAILURES}")
        snap(f"final_{int(time.time())}")
    elif cmd == "shift":
        log(f"shift {a[0]} -> {a[1]}: {global_shift(a[0], a[1])}")


if __name__ == "__main__":
    main()
