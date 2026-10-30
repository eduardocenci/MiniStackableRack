#!/bin/sh
# make-offline-card.sh — regenerate offline.mp4, the "sem sinal" card that
# canteiro-hls (mediamtx alwaysAvailable) plays on repeat while the camera
# source is down. Run ON bnu-raspberrypi from ~/canteiro-hls (uses the pinned
# go2rtc image only for its ffmpeg — no new image pulled):
#
#   sh make-offline-card.sh && docker compose up -d --force-recreate
#
# The card MUST carry the same tracks as the camera, or mediamtx refuses the
# source ("wants to publish [H265 MPEG-4 Audio], but stream expects [...]"):
#   - H265 Main, 2304x1296, yuv420p, tagged hvc1 — the iM9 main stream; same
#     resolution so readers never see a size change at the switch
#   - AAC-LC 16 kHz mono (silent) — the camera's audio. The built-in offline
#     segment (alwaysAvailableTracks) cannot do this: mediamtx 1.20.1 rejects
#     MPEG4Audio below 22050 Hz, hence a file.
# 1 s long, one keyframe per loop (15 fps, keyint 15), so the switch back to
# the live camera can happen on any loop boundary.
set -eu
cd "$(dirname "$0")"
docker run --rm --user "$(id -u):$(id -g)" --entrypoint ffmpeg \
  -v "$PWD":/w -v /usr/share/fonts/truetype/dejavu:/fonts:ro \
  alexxit/go2rtc:1.9.14 -hide_banner -loglevel error -y \
  -f lavfi -i "color=c=0x1b1f24:s=2304x1296:r=15:d=1" \
  -f lavfi -i "anullsrc=r=16000:cl=mono" \
  -vf "drawtext=fontfile=/fonts/DejaVuSans-Bold.ttf:text='Sem sinal da câmera do canteiro':fontcolor=white:fontsize=96:x=(w-text_w)/2:y=h/2-130,drawtext=fontfile=/fonts/DejaVuSans.ttf:text='reconectando sozinha — a imagem volta assim que a câmera responder':fontcolor=0xb8c0cc:fontsize=52:x=(w-text_w)/2:y=h/2+40" \
  -c:v libx265 -preset medium -profile:v main -pix_fmt yuv420p -tag:v hvc1 \
  -x265-params "keyint=15:min-keyint=15:scenecut=0:level-idc=5.0:log-level=error" \
  -c:a aac -b:a 24k -ar 16000 -ac 1 \
  -t 1 -shortest -movflags +faststart /w/offline.mp4
docker run --rm --entrypoint ffprobe -v "$PWD":/w alexxit/go2rtc:1.9.14 -v error \
  -show_entries stream=codec_name,codec_tag_string,profile,width,height,sample_rate,channels:format=duration \
  -of compact /w/offline.mp4
