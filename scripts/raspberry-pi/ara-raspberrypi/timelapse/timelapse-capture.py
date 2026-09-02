#!/usr/bin/env python3
"""timelapse-capture — daily construction-timelapse frames of the ARA canteiro.

Runs ENTIRELY on ara-raspberrypi (supercronic inside the canteiro-timelapse
container — see ../docker/canteiro-timelapse/; until 2026-08-29 it was four
systemd timers): frame grabs hit the LOCAL mediamtx relay (127.0.0.1:8554)
and PTZ moves go from the Pi to the camera over the house LAN via
`canteiro-ptz` — internet (Starlink) is only involved in the 20:00 rclone
upload, never in capture or movement.

  canteiro      = PT lens   (channel 1 — auto-tracking; the firmware returns
                  it to the guard position ~1 min after losing a tracked
                  person/vehicle, but NOT after manual ONVIF moves — probed
                  27/08/2026, still parked 95 s after a move)
  canteiro-alt  = fixed lens (channel 2 — pulled on demand)

Grabs are keyframe-only (-skip_frame nokey): HEVC mid-GOP grabs from the
relay produce gray garbage (see ../ptz/README.md).

Drive/outbox layout (decisão Eduardo 27/08/2026; posicao3 added 30/08/2026):
  posicao1/<janela>/YYYY-MM-DD_HHMM.jpg   PT lens at the guard (main, T+0:00)
  posicao2/<janela>/...                   PT lens panned RIGHT (calibrated,
                                          ~T+0:40)
  posicao3/<janela>/...                   PT lens panned LEFT (mirror of
                                          posicao2, ~T+2:00)
  lentefixa/<janela>/...                  fixed lens, shot with the main
  trabalho/YYYY-MM/...                    presence log, every 15 min 07:00-18:00

<janela> = nascer-do-sol / nascer-do-sol-mais-10min / nascer-do-sol-mais-20min
and por-do-sol-menos-20min / -menos-10min / por-do-sol / -mais-10min /
-mais-20min.

Posições 2 and 3 are dead-reckoned from the guard with burst RECIPES
calibrated by Eduardo (pos2 27/08/2026, pos3 = mirror to the other side
30/08/2026) — this firmware has no usable ONVIF presets
(MaximumNumberOfPresets=0, GetStatus lies; ../ptz/README.md), but every
excursion starts from the same guard position, so replaying the exact bursts
reproduces the framing. After each shot the mirrored reverse recipe walks
the lens back to the guard (residual ±3-4% per round trip; the firmware
guard-return wipes it at the first tracked person of the day).

Subcommands
  trabalho   one PT frame -> outbox/trabalho/YYYY-MM/
  sunset     today's sunset windows T-20 T-10 T T+10 T+20 (NOAA, coords of
             the Céu Azul aerodrome 26°33'41"S 48°41'46"W, UTC-3 fixed);
             per window: posicao1 + lentefixa, wait 30 s, then for each of
             posicao2/posicao3: recipe -> shot -> reverse. The 16:40 start
             covers the earliest T-20 (17:09, June).
  sunrise    same for the sunrise windows T T+10 T+20; the 05:00 start
             covers the earliest sunrise of the year (~05:15, December).
  pos2test / pos3test
             recipe -> snap to /tmp/pos<N>test.jpg -> reverse, no outbox
             writes — health/re-calibration check.
  reanchor [--dry]
             visual re-anchor of the guard: correlates the shed's Y-post ROI
             of a fresh snap against the reference library
             /var/lib/timelapse/ref/posicao1-*.jpg (dawn/day/dusk — the one
             with the sharpest peak wins, i.e. the closest lighting) and
             nudges the lens until the offset is within tolerance (--dry
             only measures; a correction that does not shrink the offset is
             undone). Runs automatically ~60 s before EVERY window and at
             the end of each sequence: the excursions drift 150-300 px per
             window (tilt always downwards — gravity; pan by gear backlash
             on reversal — measured 01/09/2026) and the firmware's guard
             baseline walks on busy days (chained auto-tracking, 31/08).
"""
import math
import os
import subprocess
import sys
import time
from datetime import date, datetime

BASE = "/var/lib/timelapse"
OUTBOX = os.path.join(BASE, "outbox")
RELAY_PT = "rtsp://127.0.0.1:8554/canteiro"
RELAY_FIXA = "rtsp://127.0.0.1:8554/canteiro-alt"
PTZ = "/usr/local/bin/canteiro-ptz"

# Lote 56, Cond. Aeronáutico Céu Azul, Araquari SC — aerodrome coordinates
LAT = -(26 + 33 / 60 + 41 / 3600)
LON = -(48 + 41 / 60 + 46 / 3600)
TZ_H = -3.0  # America/Sao_Paulo, no DST

SUNSET_WINDOWS = [
    (-20, "por-do-sol-menos-20min"),
    (-10, "por-do-sol-menos-10min"),
    (0, "por-do-sol"),
    (10, "por-do-sol-mais-10min"),
    (20, "por-do-sol-mais-20min"),
]
SUNRISE_WINDOWS = [
    (0, "nascer-do-sol"),
    (10, "nascer-do-sol-mais-10min"),
    (20, "nascer-do-sol-mais-20min"),
]

# Burst recipes from the guard (pos2 calibrated 27/08/2026, pos3 = mirror to
# the other side 30/08/2026 — Eduardo). Originally 2 x 0.5 s of pan; merged
# into ONE 1.1 s burst on 01/09/2026 (Eduardo): fewer Stop events = fewer
# backlash/latency glitches, and 1.1 s (not 1.0) compensates the one
# "latency quantum" the second burst used to add — measured against the
# 2x0.5 s framing: pos2 (-24,-24) px, pos3 (-84,-56) px. The reverse is the
# mirrored sequence with inverted signs. +vx pans right, +vy tilts up.
RECIPES = [
    ("posicao2", [(0.4, 0.0, 1.1), (0.0, 0.4, 0.2)]),
    ("posicao3", [(-0.4, 0.0, 1.1), (0.0, 0.4, 0.2)]),
]

# --- Re-âncora visual da guarda (posição 1) --------------------------------
# Elemento de referência: o pilar em Y do galpão (decisão Eduardo 31/08/2026
# — a obra evolui, o pilar não). ROI no frame de referência; a correlação de
# fase roda sobre magnitude de gradiente, então o pilar (bordas fortes)
# domina mesmo com o fundo mudando.
REF_DIR = "/var/lib/timelapse/ref"   # posicao1-*.jpg: madrugada/dia/crepúsculo —
                                     # usa-se a de maior pico (casa a iluminação)
ROI = (300, 50, 500, 850)        # x, y, w, h do pilar no frame de referência
SEARCH_MARGIN = 700              # px de busca ao redor da ROI (pilar ainda inteiro a +-600 px)
DOWNSCALE = 4                    # correlação em 1/4 da resolução
# Lei de controle (medida 01/09/2026, ~40 bursts): o motor tem um QUANTUM
# minimo de ~280 px de pan por comando — v<=0.12 nao move; v=0.15..0.25 ->
# ~280-380 px; v=0.4 -> ~400 px; duracao <0.25 s e <Timeout> do ONVIF nao
# modulam (latencia/rampa dominam). Precisao possivel: +-140 px. Por isso
# so corrige acima de ~150 px (pan) / 100 px (tilt, quantum ~180) e usa
# velocidade alta (0.4) so para erros grandes.
CORR_BURST_S = 0.2
PAN_QUANTUM_PX = 280.0           # menor passo de pan executavel (v 0.15-0.2)
PAN_BIG_PX = 350.0               # acima disso usa v 0.4 (~400 px)
TILT_QUANTUM_PX = 180.0          # tilt a v 0.4, 0.2 s
REANCHOR_TOL_PX = 80             # |offset| aceitável (~3.5% do FOV)
PAN_CORRECT_MIN_PX = 200         # quantum varia 280-400 px: abaixo de 200 a correcao e cara ou coroa
TILT_CORRECT_MIN_PX = 100
REANCHOR_MAX_ITER = 5
PSR_MIN = 12.0                   # PSR minimo p/ uma ref entrar na votacao (bons: 22-113; lixo: 6-17)
PSR_STRONG = 30.0                # uma unica ref acima disso ja vale sozinha
AGREE_PX = 80                    # duas refs concordando dentro disso = medicao valida
PAN_LONG_PX = 600                # a partir daqui a correcao e um burst longo proporcional
SWEEP_WAIT_S = 75                # antes de varrer: o firmware devolve a camera ~1 min apos perder o alvo
SWEEP_NETS = (0.3, -0.3, 0.6, -0.6, 0.9, -0.9, 1.2, -1.2)   # deslocamento liquido (s a vel 0.4) de cada passo da varredura
STALL_PX = 20                    # eixo que se moveu menos que isso apos um burst = stall; aceita e nao insiste
SECONDARY_DELAY_S = 30   # wait after the main shots before moving
SETTLE_S = 2             # settle after arriving, before shooting


def _solar(d):
    """(solar noon, half day-arc) in local-clock minutes (NOAA equations)."""
    a = (14 - d.month) // 12
    y = d.year + 4800 - a
    m = d.month + 12 * a - 3
    jdn = d.day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    jd = jdn + (12 - TZ_H) / 24 - 0.5
    T = (jd - 2451545.0) / 36525.0
    L0 = (280.46646 + T * (36000.76983 + 0.0003032 * T)) % 360
    M = 357.52911 + T * (35999.05029 - 0.0001537 * T)
    e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T)
    Mr = math.radians(M)
    C = (math.sin(Mr) * (1.914602 - T * (0.004817 + 0.000014 * T))
         + math.sin(2 * Mr) * (0.019993 - 0.000101 * T)
         + math.sin(3 * Mr) * 0.000289)
    omega = 125.04 - 1934.136 * T
    lam = L0 + C - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    eps0 = 23 + (26 + (21.448 - T * (46.8150 + T * (0.00059 - T * 0.001813))) / 60) / 60
    eps = eps0 + 0.00256 * math.cos(math.radians(omega))
    decl = math.asin(math.sin(math.radians(eps)) * math.sin(math.radians(lam)))
    yv = math.tan(math.radians(eps / 2)) ** 2
    L0r = math.radians(L0)
    eqtime = 4 * math.degrees(
        yv * math.sin(2 * L0r) - 2 * e * math.sin(Mr)
        + 4 * e * yv * math.sin(Mr) * math.cos(2 * L0r)
        - 0.5 * yv * yv * math.sin(4 * L0r) - 1.25 * e * e * math.sin(2 * Mr))
    latr = math.radians(LAT)
    cosH = (math.cos(math.radians(90.833)) / (math.cos(latr) * math.cos(decl))
            - math.tan(latr) * math.tan(decl))
    H = math.degrees(math.acos(max(-1.0, min(1.0, cosH))))
    return 720 - 4 * LON - eqtime + TZ_H * 60, 4 * H


def sunset_minutes(d):
    noon, half_arc = _solar(d)
    return noon + half_arc


def sunrise_minutes(d):
    noon, half_arc = _solar(d)
    return noon - half_arc


def _grab_abs(url, dest, tries=3):
    """One keyframe JPEG from `url` into an absolute path."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    for attempt in range(1, tries + 1):
        try:
            r = subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error",
                 "-rtsp_transport", "tcp", "-skip_frame", "nokey",
                 "-i", url, "-frames:v", "1", "-q:v", "2", "-f", "image2",
                 "-y", tmp],
                timeout=75)
            # gray/garbage frames come out tiny; a real 3MP frame is >100 KB
            if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 50_000:
                os.replace(tmp, dest)
                print(f"ok {dest}")
                return True
        except subprocess.TimeoutExpired:
            pass
        print(f"retry {attempt}/{tries} {dest}", file=sys.stderr)
        time.sleep(5)
    if os.path.exists(tmp):
        os.remove(tmp)
    print(f"FAILED {dest}", file=sys.stderr)
    return False


def grab(url, rel_dest, tries=3):
    return _grab_abs(url, os.path.join(OUTBOX, rel_dest), tries)


def ptz_move(vx, vy, dur):
    try:
        r = subprocess.run([PTZ, "move", str(vx), str(vy), str(dur)],
                           capture_output=True, timeout=30)
        ok = r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        ok = False
    if not ok:
        print(f"PTZ move FAILED ({vx},{vy},{dur})", file=sys.stderr)
    return ok


def goto_secondary(recipe):
    """Walk a recipe from the guard; returns the bursts executed (for reversal)."""
    done = []
    for vx, vy, dur in recipe:
        if not ptz_move(vx, vy, dur):
            break
        done.append((vx, vy, dur))
        time.sleep(1)
    time.sleep(SETTLE_S)
    return done


def back_to_guard(done):
    """Mirror the executed bursts back, best-effort even on partial failure."""
    for vx, vy, dur in reversed(done):
        ptz_move(-vx, -vy, dur)
        time.sleep(1)


def _measure_offset(ref_path, cur_path):
    """(sx, sy, peak, psr): deslocamento do conteúdo atual vs referência, em
    px full-res, medido na ROI do pilar. sx>0 = conteúdo à direita (câmera
    pan-esquerda); sy>0 = conteúdo abaixo (câmera tilt-acima). psr = pico
    sobre o desvio dos sidelobes — confiança invariante à iluminação."""
    import numpy as np
    from PIL import Image

    x, y, w, h = ROI
    m = SEARCH_MARGIN
    ref = Image.open(ref_path).convert("L")
    cur = Image.open(cur_path).convert("L")
    W, H = ref.size
    wx0, wy0 = max(0, x - m), max(0, y - m)
    wx1, wy1 = min(W, x + w + m), min(H, y + h + m)
    ref_roi = ref.crop((x, y, x + w, y + h))
    cur_win = cur.crop((wx0, wy0, wx1, wy1))

    def prep(img):
        img = img.resize((img.width // DOWNSCALE, img.height // DOWNSCALE))
        a = np.asarray(img, dtype=np.float64)
        gy, gx = np.gradient(a)
        g = np.hypot(gx, gy)
        return g - g.mean()

    g_ref = prep(ref_roi)
    g_cur = prep(cur_win)
    canvas = np.zeros_like(g_cur)
    ox, oy = (x - wx0) // DOWNSCALE, (y - wy0) // DOWNSCALE
    canvas[oy:oy + g_ref.shape[0], ox:ox + g_ref.shape[1]] = g_ref

    F1 = np.fft.fft2(g_cur)
    F2 = np.fft.fft2(canvas)
    R = F1 * np.conj(F2)
    R /= np.abs(R) + 1e-9
    r = np.real(np.fft.ifft2(R))
    peak = np.unravel_index(np.argmax(r), r.shape)
    mask = np.ones_like(r, dtype=bool)
    py, px = peak
    mask[max(0, py - 3):py + 4, max(0, px - 3):px + 4] = False
    psr = float((r[peak] - r[mask].mean()) / (r[mask].std() + 1e-9))
    sy, sx = peak
    if sy > r.shape[0] // 2:
        sy -= r.shape[0]
    if sx > r.shape[1] // 2:
        sx -= r.shape[1]
    return sx * DOWNSCALE, sy * DOWNSCALE, float(r[peak]), psr


def _measure_valid(cur_path):
    """Mede contra todas as referências posicao1-*.jpg e só aceita quando
    pelo menos duas referências com PSR razoável CONCORDAM (<= AGREE_PX):
    numa medição boa as refs concordam em <30 px; numa ruim divergem por
    centenas (02/09/2026). Retorna (sx, sy, psr, ref) ou None."""
    import glob
    cands = []
    for ref in sorted(glob.glob(os.path.join(REF_DIR, "posicao1-*.jpg"))):
        sx, sy, pk, psr = _measure_offset(ref, cur_path)
        if psr >= PSR_MIN:
            cands.append((psr, sx, sy, os.path.basename(ref)))
    cands.sort(reverse=True)
    for i, (psr, sx, sy, name) in enumerate(cands):
        for psr2, sx2, sy2, _ in cands[i + 1:]:
            if abs(sx - sx2) <= AGREE_PX and abs(sy - sy2) <= AGREE_PX:
                return sx, sy, psr, name
    if cands and cands[0][0] >= PSR_STRONG:   # uma ref so, mas inequivoca
        psr, sx, sy, name = cands[0]
        return sx, sy, psr, name
    return None


def _corr_moves(sx, sy):
    """Bursts de correcao (vx, vy, dur) para o offset medido — um por eixo,
    respeitando o quantum minimo do motor; erros grandes (>= PAN_LONG_PX)
    usam burst longo proporcional (acima de ~0.3 s a duracao volta a
    controlar: ~1250 px/s + quantum)."""
    moves = {}
    if abs(sx) >= PAN_LONG_PX:
        dur = round(0.3 + (abs(sx) - 400) / 1250.0, 2)
        moves["pan"] = (0.4 if sx > 0 else -0.4, 0, max(0.3, min(1.5, dur)))
    elif abs(sx) >= PAN_CORRECT_MIN_PX:
        v = 0.4 if abs(sx) >= PAN_BIG_PX else 0.2
        moves["pan"] = (v if sx > 0 else -v, 0, CORR_BURST_S)
    if abs(sy) >= TILT_CORRECT_MIN_PX:
        moves["tilt"] = (0, -0.4 if sy > 0 else 0.4, CORR_BURST_S)
    return moves


def _snap_measure():
    if not _grab_abs(RELAY_PT, "/tmp/reanchor.jpg"):
        print("reanchor: snap falhou", file=sys.stderr)
        return "fail"
    return _measure_valid("/tmp/reanchor.jpg")


def _recover_sweep(tag):
    """Pilar fora da janela de busca (excursao falhada, tracking...). 1) espera
    o guard-return do firmware (~1 min apos perder o alvo) e re-mede; 2) se
    ainda invalido, varre em pan com deslocamento liquido crescente e
    alternado (SWEEP_NETS, ate +-1.2 s = uma excursao inteira) medindo a
    cada passo; sem sucesso, volta ao ponto de partida."""
    print(f"reanchor{tag}: aguardando {SWEEP_WAIT_S}s (guard-return do firmware) antes de varrer")
    time.sleep(SWEEP_WAIT_S)
    meas = _snap_measure()
    if meas not in (None, "fail"):
        print(f"reanchor{tag}: pilar de volta sem varredura")
        return meas
    net = 0.0
    for target in SWEEP_NETS:
        step = target - net
        ptz_move(0.4 if step > 0 else -0.4, 0, round(abs(step), 2))
        net = target
        time.sleep(1 + SETTLE_S)
        meas = _snap_measure()
        if meas not in (None, "fail"):
            print(f"reanchor{tag}: pilar reencontrado apos varredura (liq. {net:+.2f}s)")
            return meas
        print(f"reanchor{tag}: varredura liq. {net:+.2f}s sem pilar", file=sys.stderr)
    if net:
        ptz_move(-0.4 if net > 0 else 0.4, 0, round(abs(net), 2))   # desfaz a varredura
        time.sleep(1 + SETTLE_S)
    print(f"reanchor{tag}: varredura falhou, posicao restaurada", file=sys.stderr)
    return None


def cmd_reanchor(dry=False, tag=""):
    """Mede o offset da guarda vs referências (com concordância entre refs)
    e corrige com bursts; pilar perdido -> varredura de recuperacao.
    Avalia POR EIXO: eixo que piorou tem a correcao desfeita; eixo que nao
    se moveu (stall) e aceito; dois eixos piorando = aborta."""
    prev, last_moves, stalled = None, {}, set()
    for it in range(1, REANCHOR_MAX_ITER + 1):
        meas = _snap_measure()
        if meas == "fail":
            return 1
        if meas is None:
            print(f"reanchor{tag} it{it}: medicao invalida (refs discordam / PSR baixo)")
            if dry:
                return 1
            meas = _recover_sweep(tag)
            if meas is None:
                return 1
            prev, last_moves = None, {}
        sx, sy, psr, ref_name = meas
        print(f"reanchor{tag} it{it}: offset=({sx:+.0f},{sy:+.0f})px psr={psr:.1f} ref={ref_name}")
        if max(abs(sx), abs(sy)) <= REANCHOR_TOL_PX:
            print("reanchor: dentro da tolerancia")
            return 0
        if dry:
            print("reanchor: dry-run, sem correcao")
            return 0
        if prev is not None:
            worse = []
            for ax, cur, old in (("pan", sx, prev[0]), ("tilt", sy, prev[1])):
                if ax not in last_moves:
                    continue
                if abs(cur - old) < STALL_PX:       # burst nao moveu o motor
                    stalled.add(ax)
                    print(f"reanchor: {ax} nao se moveu (stall), aceito como esta", file=sys.stderr)
                elif abs(cur) > abs(old) + 40:      # piorou: desfaz so este eixo
                    worse.append(ax)
                    vx, vy, dur = last_moves[ax]
                    ptz_move(-vx, -vy, dur)
                    time.sleep(1)
                    print(f"reanchor: {ax} piorou, correcao desfeita", file=sys.stderr)
            if len(worse) == 2 or (worse and len(last_moves) == 1):
                print("reanchor: nao converge — abortando", file=sys.stderr)
                return 1
            if worse:
                time.sleep(SETTLE_S)
                continue  # re-mede antes de tentar de novo
        prev, last_moves = (sx, sy), {}
        for ax, mv in _corr_moves(sx, sy).items():
            if ax in stalled:
                continue
            ptz_move(*mv)
            last_moves[ax] = mv
            time.sleep(1)
        if not last_moves:  # nada corrigivel (abaixo do passo minimo ou stall): aceita
            print("reanchor: residuo nao corrigivel com o passo minimo, aceito")
            return 0
        time.sleep(SETTLE_S)
    print("reanchor: max iteracoes atingido", file=sys.stderr)
    return 1


def cmd_trabalho():
    now = datetime.now()
    mins = now.hour * 60 + now.minute
    if not (6 * 60 + 55 <= mins <= 18 * 60 + 5):  # guard against odd manual runs
        print("outside 07:00-18:00 window, skipping")
        return 0
    stamp = now.strftime("%Y-%m-%d_%H%M")
    ok = grab(RELAY_PT, f"trabalho/{now.strftime('%Y-%m')}/{stamp}.jpg")
    return 0 if ok else 1


def run_windows(T, windows, label):
    print(f"{label} today: {int(T // 60):02d}:{int(T % 60):02d}")
    def reanchor_safely(tag):
        try:
            cmd_reanchor(tag=tag)
        except Exception as e:  # numpy/PIL ausentes ou erro inesperado: segue sem
            print(f"reanchor indisponivel: {e}", file=sys.stderr)

    failures = 0
    for off, folder in windows:
        target = (T + off) * 60  # seconds after midnight
        # re-ancora ~60 s antes de CADA janela: as excursoes derivam ~150-300 px
        # por janela (tilt sempre p/ baixo — gravidade; pan por backlash) e a
        # baseline do firmware anda com tracking encadeado (medido 01/09/2026)
        now = datetime.now()
        now_s = now.hour * 3600 + now.minute * 60 + now.second
        if now_s < target - 60:
            time.sleep(target - 60 - now_s)
        if now_s <= target + 300:
            reanchor_safely(f"[{folder}]")
        now = datetime.now()
        now_s = now.hour * 3600 + now.minute * 60 + now.second
        if now_s < target:
            time.sleep(target - now_s)
        elif now_s > target + 300:
            print(f"window {folder} already >5 min past, skipping", file=sys.stderr)
            failures += 1
            continue
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        if not grab(RELAY_PT, f"posicao1/{folder}/{stamp}.jpg"):
            failures += 1
        grab(RELAY_FIXA, f"lentefixa/{folder}/{stamp}.jpg")
        # posições 2 e 3: move, shoot, walk back — camera ends at the guard
        time.sleep(SECONDARY_DELAY_S)
        for pos_name, recipe in RECIPES:
            done = goto_secondary(recipe)
            if len(done) == len(recipe):
                stamp2 = datetime.now().strftime("%Y-%m-%d_%H%M")
                if not grab(RELAY_PT, f"{pos_name}/{folder}/{stamp2}.jpg"):
                    failures += 1
            else:
                print(f"{pos_name} {folder} skipped (incomplete move)", file=sys.stderr)
                failures += 1
            back_to_guard(done)
            # malha fechada apos CADA volta: a coreografia e reproduzivel ao
            # pixel na maioria dos ciclos, mas ~40% deles perdem um quantum
            # (~150-360 px) na inversao de sentido do pan — backlash
            # intermitente, medido 01/09/2026 (10 ciclos). Medir+corrigir aqui
            # garante o proximo ponto de partida (e a proxima pos1) na guarda.
            time.sleep(SETTLE_S)
            reanchor_safely(f"[{folder}:{pos_name}->guarda]")
    return 0 if failures == 0 else 1


def cmd_postest(pos_name):
    recipe = dict(RECIPES)[pos_name]
    done = goto_secondary(recipe)
    ok = len(done) == len(recipe) and _grab_abs(RELAY_PT, f"/tmp/{pos_name}test.jpg")
    back_to_guard(done)
    print(f"{pos_name} test:", f"ok -> /tmp/{pos_name}test.jpg" if ok else "FAILED")
    return 0 if ok else 1


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "trabalho":
        return cmd_trabalho()
    if cmd == "sunset":
        return run_windows(sunset_minutes(date.today()), SUNSET_WINDOWS, "sunset")
    if cmd == "sunrise":
        return run_windows(sunrise_minutes(date.today()), SUNRISE_WINDOWS, "sunrise")
    if cmd == "pos2test":
        return cmd_postest("posicao2")
    if cmd == "pos3test":
        return cmd_postest("posicao3")
    if cmd == "reanchor":
        return cmd_reanchor(dry="--dry" in sys.argv[2:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
