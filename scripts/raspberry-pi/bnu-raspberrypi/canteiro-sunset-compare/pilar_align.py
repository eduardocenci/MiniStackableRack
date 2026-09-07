#!/usr/bin/env python3
"""pilar_align — mede o deslocamento do pilar do galpão numa foto pos1 e diz
quanto recortar para trazer o pilar de volta à posição de referência.

Por que existe: mesmo com a re-âncora da câmera (ara), a posição de guarda
varia até ~170 px de um dia para o outro — a baseline do tracking caminha
durante o dia e o passo mínimo do motor (~280 px de pan) não corrige
resíduos abaixo disso (medido 03-06/09/2026). No timelapse e nas grades do
WhatsApp isso vira um tremor visível entre dias. Como o offset de cada frame
é medível com precisão de ~4 px, o alinhamento por software (deslocar+recortar)
zera o tremor sem tocar na câmera.

Este módulo é a MESMA medição do `timelapse-capture` do ara
(scripts/raspberry-pi/ara-raspberrypi/timelapse/): correlação de fase (FFT
do numpy) sobre a magnitude do gradiente da ROI do pilar, validade por
concordância entre as 4 referências `posicao1-*.jpg`. As constantes abaixo
são cópia da doutrina do ara (fonte da verdade lá; mudam raramente) — se a
ROI ou as referências mudarem lá, atualize aqui. As referências vêm do
backup no Drive `ceuazul:Timelapse/ref/` (o ara não é alcançável do bnu).

`sx>0` = conteúdo à direita da referência; `sy>0` = conteúdo abaixo. Para
alinhar, recorta-se a janela de referência deslocada de `(sx, sy)` na foto.
"""
import glob
import os

# --- doutrina do pilar (espelho do timelapse-capture do ara) ---
ROI = (300, 50, 500, 850)   # x, y, w, h do pilar no frame de referência (2304x1296)
SEARCH_MARGIN = 700         # px de busca ao redor da ROI
DOWNSCALE = 4               # correlação em 1/4 da resolução
PSR_MIN = 4.0               # filtro anti-ruído (de dia o PSR de medições certas cai a 5-13)
PSR_STRONG = 30.0           # uma única ref acima disso vale sozinha
AGREE_PX = 80               # duas refs concordando dentro disso = medição válida
TILT_PLAUSIBLE_PX = 400     # |tilt| acima disso só com PSR forte (falsos acordos perto da parede)
TILT_PLAUSIBLE_PSR = 15
ALIGN_MAX_PX = 350          # offset maior que isso: não confia, não desloca (protege o enquadramento)


def available():
    """numpy + pillow presentes? (sem eles o alinhamento é pulado)."""
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:
        return False


def measure_offset(ref_path, cur_path):
    """(sx, sy, peak, psr) em px full-res, medido na ROI do pilar. sx>0 =
    conteúdo à direita (câmera pan-esquerda); sy>0 = conteúdo abaixo. psr =
    pico sobre o desvio dos sidelobes (confiança invariante à iluminação)."""
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


def measure_valid(cur_path, ref_dir):
    """Mede contra todas as `posicao1-*.jpg` em `ref_dir`. Validade vem da
    CONCORDÂNCIA entre referências (>= 2 dentro de AGREE_PX), não do PSR
    absoluto. Devolve a mediana do grupo concordante: (sx, sy, psr_max, ref)
    ou None."""
    cands = []
    for ref in sorted(glob.glob(os.path.join(ref_dir, "posicao1-*.jpg"))):
        sx, sy, pk, psr = measure_offset(ref, cur_path)
        if psr >= PSR_MIN:
            cands.append((psr, sx, sy, os.path.basename(ref)))
    cands.sort(reverse=True)
    best_group = []
    for i, (psr, sx, sy, name) in enumerate(cands):
        group = [c for c in cands if abs(c[1] - sx) <= AGREE_PX and abs(c[2] - sy) <= AGREE_PX]
        if len(group) >= 2 and len(group) > len(best_group):
            best_group = group
    if best_group:
        xs = sorted(c[1] for c in best_group)
        ys = sorted(c[2] for c in best_group)
        mid = len(xs) // 2
        if abs(ys[mid]) > TILT_PLAUSIBLE_PX and best_group[0][0] < TILT_PLAUSIBLE_PSR:
            return None
        return xs[mid], ys[mid], best_group[0][0], best_group[0][3]
    if cands and cands[0][0] >= PSR_STRONG:
        psr, sx, sy, name = cands[0]
        return sx, sy, psr, name
    return None


def sane_offset(measured):
    """Recebe o retorno de measure_valid (ou None) e devolve (sx, sy) para
    aplicar, ou None quando não se deve deslocar (medição inválida ou grande
    demais para ser confiável)."""
    if not measured:
        return None
    sx, sy = int(round(measured[0])), int(round(measured[1]))
    if max(abs(sx), abs(sy)) > ALIGN_MAX_PX:
        return None
    return sx, sy
