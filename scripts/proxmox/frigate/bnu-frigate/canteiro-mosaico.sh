#!/bin/bash
# canteiro-mosaico — composes THE live view of the ARA canteiro: fixed lens
# (left, street side) | PT lens (right, parked at its rest position), side by
# side — the "almost 180°" view (pedido Eduardo 2026-11-14).
#
# Authoritative copy: scripts/proxmox/frigate/bnu-frigate/canteiro-mosaico.sh
# Live copy:          bnu Frigate LXC 105:/usr/local/bin/canteiro-mosaico.sh
#                     (unit: canteiro-mosaico.service, same folder)
#
# Runs in the Frigate LXC because that is where the GPU is (Ryzen 6800H,
# VAAPI) and where both HEVC mains already arrive for `record` — it reads
# Frigate's own go2rtc producers, so the canteiro's Starlink carries nothing
# extra. Decodes 2x HEVC 2304x1296 on the GPU, scales + hstacks on the CPU
# and encodes H.264 2560x720 15 fps ~3 Mbps on the GPU: ~27 % of one core
# (measured). The all-GPU graph (scale_vaapi + hstack_vaapi) runs out of
# surfaces on this Mesa 22.3 ("Cannot allocate memory" after 11 frames).
# Timestamps are the streams' own (each RTSP input starts at 0 when opened), NOT
# -use_wallclock_as_timestamps: ffmpeg 7 chokes the demuxer whose timestamps run
# ahead, and with read-time stamps every choke became latency that never drained
# (left half 15-30 s behind the right one, measured 2026-11-14). With the real
# clocks the halves stay a constant ~1-3 s apart (the time input 1 takes to open).
#
# Publishes to canteiro-hls (mediamtx) on bnu-raspberrypi, path `mosaico` —
# a publisher, not a go2rtc `exec:` source, for two reasons: Frigate refuses
# exec sources unless GO2RTC_ALLOW_ARBITRARY_EXEC is set (left alone), and a
# publisher that dies is a clean EOF for mediamtx's readers instead of the
# timestamp jump go2rtc hands its consumers when it swaps a producer.
# Second output, path `mosaico-tv`: the same picture at 1920x540 ~2 Mbps for the
# TV cast — the Neo QLED's Google Cast receiver refuses H.264 2560 px wide
# (2026-11-14, seen on the physical TV: player `idle`, no request made).
# Any input EOF/stall (15 s) ends ffmpeg; systemd restarts it 5 s later.
# A dead OUTPUT does not: with two outputs, ffmpeg 7 logs "Broken pipe" on both
# muxers and stays alive publishing nothing (seen 2026-11-14 and 15, every time
# the watchdog's auto-heal restarted canteiro-hls: mosaic down until a manual
# restart). So ffmpeg runs in the background and this script watches its two
# publisher connections to mediamtx: fewer than two for 30 s in a row (after
# 60 s of start-up grace) = kill it and let systemd start a fresh one.
F=/usr/lib/ffmpeg/7.0/bin/ffmpeg
IN="-hwaccel vaapi -hwaccel_device va -rtsp_transport tcp -timeout 15000000 -analyzeduration 1000000"
"$F" -hide_banner -loglevel warning -nostdin \
  -init_hw_device vaapi=va:/dev/dri/renderD128 -filter_hw_device va \
  $IN -i rtsp://127.0.0.1:8554/canteiro_fixa \
  $IN -i rtsp://127.0.0.1:8554/canteiro \
  -filter_complex "[0:v]fps=15,scale=1280:720[l];[1:v]fps=15,scale=1280:720[r];[l][r]hstack=inputs=2,split=2[a][b];[a]format=nv12,hwupload[v];[b]scale=1920:540,format=nv12,hwupload[t]" \
  -map "[v]" -c:v h264_vaapi -profile:v high -b:v 3M -maxrate 4M -g 30 -an \
  -rtsp_transport tcp -f rtsp rtsp://10.1.1.123:8555/mosaico \
  -map "[t]" -c:v h264_vaapi -profile:v high -b:v 2M -maxrate 3M -g 30 -an \
  -rtsp_transport tcp -f rtsp rtsp://10.1.1.123:8555/mosaico-tv &
PID=$!
trap 'kill $PID 2>/dev/null' EXIT TERM INT
sleep 60 & wait -n $PID $! && true
bad=0
while kill -0 $PID 2>/dev/null; do
  n=$(ss -Htnp state established dst 10.1.1.123:8555 2>/dev/null | grep -c "pid=$PID,")
  if [ "$n" -ge 2 ]; then bad=0; else bad=$((bad + 1)); fi
  if [ "$bad" -ge 3 ]; then
    echo "canteiro-mosaico: $n of 2 publisher connections for 30 s - restarting" >&2
    exit 1
  fi
  sleep 10 & wait -n $PID $! && true
done
wait $PID
exit 1
