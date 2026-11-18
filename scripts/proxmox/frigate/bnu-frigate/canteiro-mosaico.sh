#!/bin/bash
# canteiro-mosaico - THE live view of the ARA canteiro: fixed lens (left, street side) | PT lens
# (right, parked at rest), side by side (pedido Eduardo 2026-11-14), plus `mosaico-tv` with the
# canteiro-painel data band under it (2026-11-15).
#
# Authoritative copy: scripts/proxmox/frigate/bnu-frigate/canteiro-mosaico.sh
# Live copy:          bnu Frigate LXC 105:/usr/local/bin/canteiro-mosaico.sh
#                     (unit: canteiro-mosaico.service, same folder)
#
# Since 2026-11-18 the composer itself is canteiro-mosaico.py (same folder; design and reasons in
# its docstring and in the README): one feeder ffmpeg per lens, a 15 fps pump that repeats the
# last frame of a lens that stopped, and an encoder ffmpeg with no network input - so a hole in
# the canteiro feed no longer takes the picture down. Until then this file WAS the composer: one
# ffmpeg with both lenses in one filter graph, which stops emitting when either input stops
# (git log has it, with the history of its fixes).
#
# This wrapper only keeps the unit's ExecStart and the diagnosis below stable:
#   canteiro-mosaico.sh --cinza   one grey-half check against what is being PUBLISHED (the newest
#                                 complete segment canteiro-clipe keeps of `mosaico`); prints the
#                                 shares, exit 1 = a half is grey, 2 = no verdict. The running
#                                 composer checks each lens on its own frames, without clipe.
F=${FFMPEG:-/usr/lib/ffmpeg/7.0/bin/ffmpeg}
CLIPE=${CLIPE_DIR:-/var/lib/canteiro-clipe}
CINZA=${CINZA:-/usr/local/bin/canteiro-mosaico-cinza.py}
if [ "$1" = "--cinza" ]; then
  seg=$(ls -1 "$CLIPE"/*.ts 2>/dev/null | tail -n 2 | head -n 1)
  [ -n "$seg" ] || { echo "cinza: no verdict (no segment in $CLIPE)"; exit 2; }
  msg=$("$F" -v error -i "$seg" -frames:v 1 -vf scale=640:180 -pix_fmt yuv444p -f rawvideo - 2>/dev/null | python3 "$CINZA"); rc=$?
  echo "${msg:-cinza: no verdict} (segment $(basename "$seg"), rc=$rc)"
  exit $rc
fi
exec /usr/bin/python3 "${MOSAICO_PY:-/usr/local/bin/canteiro-mosaico.py}"
