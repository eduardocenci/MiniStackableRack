#!/usr/bin/env python3
"""canteiro-mosaico-cinza — is one half of the mosaic a flat grey slab?

Reads ONE raw yuv444p frame of W x H (640x180) on stdin — the mosaic scaled down by
ffmpeg - and prints, per half, the share of 32x30 cells that are "decoder grey": luma near
mid-scale, neutral chroma and almost no texture. That is what a hardware decoder that lost its
reference frames paints, and no real scene does over a large area (measured 15/11/2026: a stuck
half scores 0.67-0.82 for hours, a healthy one 0.00).

Exit 0 = healthy, 1 = at least one half is grey (>= LIMIT of its cells), 2 = no verdict.
Stdlib only: it runs on the Frigate LXC's system python.

Authoritative copy: scripts/proxmox/frigate/bnu-frigate/canteiro-mosaico-cinza.py
Live copy:          bnu Frigate LXC 105:/usr/local/bin/canteiro-mosaico-cinza.py
"""
from __future__ import annotations   # the bench test runs it on python 3.8

import sys

W, H, CW, CH = 640, 180, 32, 30
LIMIT = 0.30          # share of a half's cells
Y_LO, Y_HI = 112, 144  # mean luma band of the slab
Y_SPREAD = 14          # max - min luma inside a cell (after dropping the 2 % extremes)
C_DEV = 5              # mean chroma distance from neutral (128)


def cell_stats(plane: bytes, x0: int, y0: int) -> tuple[float, int]:
    vals = []
    for y in range(y0, y0 + CH):
        row = y * W + x0
        vals.extend(plane[row:row + CW])
    vals.sort()
    k = len(vals) // 50
    return sum(vals) / len(vals), vals[-1 - k] - vals[k]


def shares(buf: bytes) -> list[float]:
    y, u, v = buf[:W * H], buf[W * H:2 * W * H], buf[2 * W * H:3 * W * H]
    out = []
    for half in (0, 1):
        grey = total = 0
        for cy in range(0, H, CH):
            for cx in range(half * W // 2, (half + 1) * W // 2, CW):
                total += 1
                ym, ys = cell_stats(y, cx, cy)
                if not (Y_LO <= ym <= Y_HI) or ys > Y_SPREAD:
                    continue
                um, _ = cell_stats(u, cx, cy)
                vm, _ = cell_stats(v, cx, cy)
                if abs(um - 128) <= C_DEV and abs(vm - 128) <= C_DEV:
                    grey += 1
        out.append(grey / total)
    return out


def main() -> int:
    buf = sys.stdin.buffer.read()
    if len(buf) < 3 * W * H:
        print(f"cinza: short frame ({len(buf)} bytes)")
        return 2
    s = shares(buf)
    print(f"cinza: fixa={s[0]:.2f} pt={s[1]:.2f}")
    return 1 if max(s) >= LIMIT else 0


if __name__ == "__main__":
    sys.exit(main())
