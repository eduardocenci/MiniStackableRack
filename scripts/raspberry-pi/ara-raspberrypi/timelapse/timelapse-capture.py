#!/usr/bin/env python3
"""timelapse-capture — daily construction-timelapse frames of the ARA canteiro.

Runs ENTIRELY on ara-raspberrypi2 (supercronic inside the canteiro-timelapse
container — see ../docker/canteiro-timelapse/): frame grabs hit the LOCAL
mediamtx relay (127.0.0.1:8554); internet (Starlink) is only involved in the
20:00 rclone upload, never in capture.

THE CAMERA IS NEVER MOVED (decisão Eduardo 14/11/2026). Until then every
solar window walked the PT lens through posicao2/posicao3 and back, with a
visual re-anchor of the guard before and after (homing, sweeps, a reference
library of the shed's pillar). Eduardo re-aimed the two lenses so that,
side by side, they cover "almost 180°":

  canteiro-alt  = fixed lens (channel 2) — looks LEFT, to the street
  canteiro      = PT lens    (channel 1) — parked at its rest position,
                  looking RIGHT. Auto-tracking is still the firmware's: it
                  follows a person/vehicle and returns to the rest position
                  ~1 min after losing the target. This script sends no PTZ
                  command at all (canteiro-ptz stays in the image only as a
                  manual tool).

The mosaic [lentefixa | posicao1] is what every consumer shows since then:
the bnu before/after grids (canteiro-sunset-compare), the /live page and
the TV (canteiro-mosaico on bnu).

Grabs are keyframe-only (-skip_frame nokey): HEVC mid-GOP grabs from the
relay produce gray garbage (see ../ptz/README.md).

Drive/outbox layout:
  lentefixa/<janela>/YYYY-MM-DD_HHMM.jpg  fixed lens — LEFT half of the mosaic
  posicao1/<janela>/...                   PT lens at rest — RIGHT half (the
                                          folder keeps its name: until
                                          13/11/2026 it held the PT lens at
                                          the old guard framing)
  trabalho/YYYY-MM/...                    presence log (PT lens), every
                                          15 min 07:00-18:00
  <pos>/Luminancia125/YYYY-MM-DD_HHMM.jpg copy of the sunset window whose
                                          posicao1 luminance is closest to
                                          125 (chosen at the end of `sunset`;
                                          the bnu grids read from here)
  posicao2/ posicao3/                     HISTORY ONLY (27/08–13/11/2026):
                                          the PT lens panned right/left

<janela> = nascer-do-sol / nascer-do-sol-mais-10min / nascer-do-sol-mais-20min
and por-do-sol-menos-20min / -menos-10min / por-do-sol / -mais-10min /
-mais-20min.

Subcommands
  trabalho   one PT frame -> outbox/trabalho/YYYY-MM/
  sunset     today's sunset windows T-20 T-10 T T+10 T+20 (NOAA, coords of
             the Céu Azul aerodrome 26°33'41"S 48°41'46"W, UTC-3 fixed);
             per window: lentefixa + posicao1, shot together. The 16:40
             start covers the earliest T-20 (17:09, June). Ends with
             `luminancia`.
  luminancia [--day YYYY-MM-DD] [--root DIR]
             picks, among today's sunset windows, the one whose posicao1
             frame has mean luminance closest to 125 and copies that window's
             frame of both lenses into <pos>/Luminancia125/ (same file
             name) — on the local outbox, so the 20:00 upload carries it.
  luminancia --backfill YYYY-MM-DD YYYY-MM-DD
             same, retroactively and directly on the Drive (server-side
             copies); skips days already present in posicao1/Luminancia125.
  sunrise    same for the sunrise windows T T+10 T+20; the 05:00 start
             covers the earliest sunrise of the year (~05:15, December).
  sunrise/sunset --resume
             relaunched by the entrypoint when the container (re)starts
             inside a sequence's span (shed power cut 27/10/2026 ~18:27:
             the sunset run died after its first window, no Luminancia125
             that day, no Dia de Trabalho grid on 27 AND 28/10 — the grid
             uses the previous day as baseline). Windows whose posicao1
             frame is already in the outbox are skipped; windows the
             outage really lost are reported (alert); the luminance choice
             then sees the whole day. A flock on /var/lib/timelapse/state
             keeps a cron start and a resume from running twice.
  inprogress prints "sunrise", "sunset" or nothing: whether a sequence
             would be running now (used by the entrypoint to decide the
             resume and to keep today's solar frames local on the
             upload-on-start).
  alerttest  sends a test failure alert to the SmokeTests group and exits 1.
"""
import glob
import math
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime

BASE = "/var/lib/timelapse"
# Alerta de falha no WhatsApp (pedido Eduardo 04/09/2026): qualquer execucao
# que termine com rc != 0 manda um resumo ao grupo Casa SmokeTests via o
# WAHA de bnu (LAN-only la; alcancado pela tailnet atraves do relay socat
# registrado no architecture.yaml). Config no env do container
# (~/canteiro-timelapse/env/alerts.env, NAO commitado): ALERT_WAHA_URL,
# ALERT_WAHA_KEY, ALERT_WAHA_SESSION, ALERT_CHAT_JID. Sem config = so loga.
ALERT_WAHA_URL = os.environ.get("ALERT_WAHA_URL", "")
ALERT_WAHA_KEY = os.environ.get("ALERT_WAHA_KEY", "")
ALERT_WAHA_SESSION = os.environ.get("ALERT_WAHA_SESSION", "default")
ALERT_CHAT_JID = os.environ.get("ALERT_CHAT_JID", "")
FAILURES = []   # razoes acumuladas na execucao (viram o corpo do alerta)


def fail(msg):
    """Registra uma falha (stderr + lista para o alerta)."""
    print(msg, file=sys.stderr)
    FAILURES.append(msg)


def send_alert(text):
    """Best-effort: nunca derruba a execucao por causa do alerta."""
    if not (ALERT_WAHA_URL and ALERT_WAHA_KEY and ALERT_CHAT_JID):
        print("alerta nao configurado (ALERT_*): " + text.replace("\n", " | "), file=sys.stderr)
        return False
    import json
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{ALERT_WAHA_URL}/api/sendText",
            data=json.dumps({"session": ALERT_WAHA_SESSION, "chatId": ALERT_CHAT_JID,
                             "text": text}).encode(),
            headers={"Content-Type": "application/json", "X-Api-Key": ALERT_WAHA_KEY})
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"alerta enviado, HTTP {r.status}")
            return True
    except Exception as e:
        print(f"alerta FALHOU: {e}", file=sys.stderr)
        return False

OUTBOX = os.path.join(BASE, "outbox")
STATE = os.path.join(BASE, "state")   # locks das sequencias (bind mount, sobrevive ao restart)
# Spans das sequencias (hora local) para `inprogress`: do minuto do cron ate a
# ultima janela + folga. Fora disso, nada a retomar.
SUNRISE_CRON_MIN = 5 * 60            # 0 5 * * *
SUNSET_CRON_MIN = 16 * 60 + 40       # 40 16 * * *
SEQ_TAIL_MIN = 5                     # T+20 + grabs (com retries) + luminancia
RELAY_PT = "rtsp://127.0.0.1:8554/canteiro"
RELAY_FIXA = "rtsp://127.0.0.1:8554/canteiro-alt"

# Tentativas por foto. A câmera some da LAN por ~40 s várias vezes ao dia
# (relay: "dial tcp 192.168.1.56:554: no route to host"; 3 quedas em
# 10/09/2026) — o grab das 15:15 morreu com 3 × 5 s e a câmera voltou 12 s
# depois. 6 × 10 s cobre ~60–90 s de queda; GRAB_BUDGET_S limita o pior
# caso (relay travado, ffmpeg estourando os 75 s) ao mesmo teto de antes
# (~4 min), para não empurrar as janelas solares.
GRAB_TRIES = 6
GRAB_RETRY_S = 10
GRAB_BUDGET_S = 240

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

# Luminancia125 (pedido Eduardo 09-11/09/2026): a luz no por do sol depende do
# clima — 08/09 encoberto deu L=125 em T, 09/09 ceu limpo deu L=100 — e as
# grades Dia/Semana/Mes de Trabalho tremiam de brilho entre dias. No fim da
# sequencia do por do sol mede-se a luminancia media (Rec.601, quadro inteiro
# menos LUM_BORDER_PX de borda) da pos1 de cada janela e copia-se, para as duas
# lentes, a foto da janela cujo L fica mais perto do alvo em
# <pos>/Luminancia125/ (mesmo nome de arquivo). Quadro inteiro e nao o pilar:
# a luz do galpao dominaria a ROI do pilar (decisao Eduardo 11/09/2026).
# Nos 9 dias medidos (01-09/09) a regra deu L entre 123 e 126 (sempre T-20,
# T-10 ou T); em T fixo variava de 100 a 128.
LUM_TARGET = 125.0
LUM_FOLDER = "Luminancia125"
LUM_POSITIONS = ["lentefixa", "posicao1"]   # as duas metades do mosaico
LUM_BORDER_PX = 100



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


def _grab_abs(url, dest, tries=GRAB_TRIES):
    """One keyframe JPEG from `url` into an absolute path.

    Retries up to `tries` times, GRAB_RETRY_S apart, but never past
    GRAB_BUDGET_S of wall-clock (see the constants for the why).
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    t0 = time.monotonic()
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
        if attempt == tries:
            break
        if time.monotonic() - t0 > GRAB_BUDGET_S:
            print(f"grab budget {GRAB_BUDGET_S}s exceeded after {attempt} tries {dest}",
                  file=sys.stderr)
            break
        print(f"retry {attempt}/{tries} {dest}", file=sys.stderr)
        time.sleep(GRAB_RETRY_S)
    if os.path.exists(tmp):
        os.remove(tmp)
    print(f"FAILED {dest}", file=sys.stderr)
    return False


def grab(url, rel_dest, tries=GRAB_TRIES):
    return _grab_abs(url, os.path.join(OUTBOX, rel_dest), tries)



def luminance(path):
    """Luminancia media Rec.601 (0-255) do quadro sem LUM_BORDER_PX de borda."""
    import numpy as np
    from PIL import Image
    a = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)
    b = LUM_BORDER_PX
    a = a[b:-b, b:-b]
    return float((0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]).mean())


def _day_files(root, pos, window, day_str):
    return sorted(glob.glob(os.path.join(root, pos, window, f"{day_str}_*.jpg")))


def select_lum_window(root, day_str):
    """Entre as janelas do por do sol com foto de pos1 em `root`, a de L mais
    perto de LUM_TARGET. Devolve (janela, L) ou None."""
    best = None
    for _off, w in SUNSET_WINDOWS:
        fs = _day_files(root, "posicao1", w, day_str)
        if not fs:
            continue
        lum = luminance(fs[-1])
        print(f"luminancia {day_str} {w}: L={lum:.0f}")
        if best is None or abs(lum - LUM_TARGET) < abs(best[1] - LUM_TARGET):
            best = (w, lum)
    return best


def cmd_luminancia(day_str=None, root=None):
    """Copia, para cada posicao, a foto da janela escolhida para
    <root>/<pos>/Luminancia125/ (mesmo nome). Roda no fim do `sunset`, com
    os arquivos ainda locais (o upload das 20:00 esvazia o outbox)."""
    day_str = day_str or datetime.now().strftime("%Y-%m-%d")
    root = root or OUTBOX
    best = select_lum_window(root, day_str)
    if not best:
        fail(f"luminancia {day_str}: nenhuma foto de pos1 do por do sol em {root}")
        return 1
    w, lum = best
    print(f"luminancia {day_str}: escolhida {w} (L={lum:.0f}, alvo {LUM_TARGET:.0f})")
    copied = 0
    for pos in LUM_POSITIONS:
        fs = _day_files(root, pos, w, day_str)
        if not fs:
            print(f"luminancia {day_str}: {pos}/{w} sem foto, pulando", file=sys.stderr)
            continue
        dst = os.path.join(root, pos, LUM_FOLDER)
        os.makedirs(dst, exist_ok=True)
        shutil.copy2(fs[-1], os.path.join(dst, os.path.basename(fs[-1])))
        copied += 1
    if not copied:
        fail(f"luminancia {day_str}: nada copiado")
        return 1
    print(f"luminancia {day_str}: {copied} posicoes copiadas para {LUM_FOLDER}/")
    return 0


def cmd_luminancia_backfill(day_from, day_to):
    """Retroativo, direto no Drive: baixa as pos1 do por do sol do intervalo
    para medir, escolhe a janela de cada dia e copia server-side (rclone
    copyto) as duas lentes para <pos>/Luminancia125/. Idempotente: pula os
    dias que ja tem pos1 na pasta."""
    import tempfile
    from datetime import timedelta
    remote = "ceuazul:Timelapse"
    d0 = datetime.strptime(day_from, "%Y-%m-%d").date()
    d1 = datetime.strptime(day_to, "%Y-%m-%d").date()
    days = []
    d = d0
    while d <= d1:
        days.append(d.isoformat())
        d += timedelta(days=1)
    done = subprocess.run(["rclone", "lsf", f"{remote}/posicao1/{LUM_FOLDER}"],
                          capture_output=True, text=True).stdout.split()
    done_days = {f[:10] for f in done}
    todo = [x for x in days if x not in done_days]
    print(f"luminancia backfill {day_from}..{day_to}: {len(todo)} dias a fazer, "
          f"{len(days) - len(todo)} ja feitos")
    if not todo:
        return 0
    rc = 0
    with tempfile.TemporaryDirectory(prefix="lum-") as tmp:
        for _off, w in SUNSET_WINDOWS:
            args = ["rclone", "copy", f"{remote}/posicao1/{w}",
                    os.path.join(tmp, "posicao1", w), "-q"]
            for x in todo:
                args += ["--include", f"{x}_*.jpg"]
            subprocess.run(args, check=False)
        for ds in todo:
            best = select_lum_window(tmp, ds)
            if not best:
                print(f"luminancia {ds}: sem pos1 no Drive, pulando", file=sys.stderr)
                continue
            w, lum = best
            print(f"luminancia {ds}: escolhida {w} (L={lum:.0f})")
            for pos in LUM_POSITIONS:
                names = subprocess.run(["rclone", "lsf", f"{remote}/{pos}/{w}",
                                        "--include", f"{ds}_*.jpg"],
                                       capture_output=True, text=True).stdout.split()
                if not names:
                    print(f"luminancia {ds}: {pos}/{w} sem foto no Drive, pulando",
                          file=sys.stderr)
                    continue
                name = sorted(names)[-1]
                r = subprocess.run(["rclone", "copyto", f"{remote}/{pos}/{w}/{name}",
                                    f"{remote}/{pos}/{LUM_FOLDER}/{name}"],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    fail(f"luminancia {ds}: copyto {pos} falhou: {r.stderr[-200:]}")
                    rc = 1
    return rc


def cmd_trabalho():
    now = datetime.now()
    mins = now.hour * 60 + now.minute
    if not (6 * 60 + 55 <= mins <= 18 * 60 + 5):  # guard against odd manual runs
        print("outside 07:00-18:00 window, skipping")
        return 0
    stamp = now.strftime("%Y-%m-%d_%H%M")
    ok = grab(RELAY_PT, f"trabalho/{now.strftime('%Y-%m')}/{stamp}.jpg")
    if not ok:
        fail(f"trabalho {stamp}: grab falhou")
    return 0 if ok else 1


def _seq_lock(label):
    """flock nao-bloqueante por sequencia: um cron start e um --resume nunca
    rodam juntos (o lock morre com o processo — sem lock velho apos restart).
    Devolve o fd aberto (manter vivo) ou None se outra instancia esta rodando."""
    import fcntl
    os.makedirs(STATE, exist_ok=True)
    fd = os.open(os.path.join(STATE, f"{label}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.write(fd, f"{os.getpid()} {datetime.now():%Y-%m-%d %H:%M:%S}\n".encode())
    return fd


def _window_done(folder, day_str):
    """Ja existe a pos1 desta janela de hoje no outbox (capturada antes de um
    reinicio)? O upload-on-start do entrypoint deixa os quadros solares de hoje
    no outbox enquanto `inprogress` diz que ha sequencia a retomar."""
    return bool(glob.glob(os.path.join(OUTBOX, "posicao1", folder, f"{day_str}_*.jpg")))


def cmd_inprogress():
    """Imprime a sequencia que estaria rodando agora (sunrise/sunset) ou nada."""
    now = datetime.now()
    mins = now.hour * 60 + now.minute
    T_rise = sunrise_minutes(date.today())
    T_set = sunset_minutes(date.today())
    if SUNRISE_CRON_MIN <= mins <= T_rise + SUNRISE_WINDOWS[-1][0] + SEQ_TAIL_MIN:
        print("sunrise")
    elif SUNSET_CRON_MIN <= mins <= T_set + SUNSET_WINDOWS[-1][0] + SEQ_TAIL_MIN:
        print("sunset")
    return 0


def run_windows(T, windows, label, resume=False):
    """Uma foto de cada lente por janela, sem mover a camera (14/11/2026)."""
    print(f"{label} today: {int(T // 60):02d}:{int(T % 60):02d}"
          + (" (retomada apos reinicio do container)" if resume else ""))
    lock = _seq_lock(label)
    if lock is None:
        print(f"{label}: outra instancia em andamento, saindo", file=sys.stderr)
        return 0
    failures = 0
    day_str = datetime.now().strftime("%Y-%m-%d")
    for off, folder in windows:
        target = (T + off) * 60  # seconds after midnight
        if resume and _window_done(folder, day_str):
            print(f"[{folder}] ja capturada antes do reinicio, pulando")
            continue
        now = datetime.now()
        now_s = now.hour * 3600 + now.minute * 60 + now.second
        if now_s < target:
            time.sleep(target - now_s)
        elif now_s > target + 300:
            fail(f"janela {folder} perdida (>5 min de atraso"
                 + (" — container parado/reiniciado durante a sequencia)" if resume else ")"))
            failures += 1
            continue
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        # as duas metades do mosaico, mesmo carimbo de arquivo
        for url, pos in ((RELAY_FIXA, "lentefixa"), (RELAY_PT, "posicao1")):
            if not grab(url, f"{pos}/{folder}/{stamp}.jpg"):
                fail(f"{pos} {folder}: grab falhou")
                failures += 1
    return 0 if failures == 0 else 1


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "trabalho":
        return cmd_trabalho()
    resume = "--resume" in sys.argv[2:]
    if cmd == "sunset":
        rc = run_windows(sunset_minutes(date.today()), SUNSET_WINDOWS, "sunset", resume)
        return max(rc, cmd_luminancia())   # 1 se a captura OU a selecao falhar
    if cmd == "inprogress":
        return cmd_inprogress()
    if cmd == "luminancia":
        args = sys.argv[2:]
        if args[:1] == ["--backfill"] and len(args) >= 3:
            return cmd_luminancia_backfill(args[1], args[2])
        day = args[args.index("--day") + 1] if "--day" in args else None
        root = args[args.index("--root") + 1] if "--root" in args else None
        return cmd_luminancia(day, root)
    if cmd == "sunrise":
        return run_windows(sunrise_minutes(date.today()), SUNRISE_WINDOWS, "sunrise", resume)
    print(__doc__)
    return 2



def _run_with_alert():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "alerttest":
        fail("teste de alerta (alerttest)")
        rc = 1
    else:
        try:
            rc = main()
        except Exception as e:
            import traceback
            fail("excecao: " + traceback.format_exc().strip().splitlines()[-1])
            rc = 1
    if rc:
        host = os.uname().nodename
        corpo = "\n".join(f"• {f}" for f in FAILURES[-8:]) or "• (sem detalhe registrado)"
        send_alert(f"⚠️ *timelapse ARA* — `{cmd}` terminou com rc={rc} em {host}\n{corpo}")
    return rc


if __name__ == "__main__":
    sys.exit(_run_with_alert())
