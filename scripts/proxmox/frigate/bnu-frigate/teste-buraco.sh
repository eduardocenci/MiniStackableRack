#!/bin/bash
# teste-buraco.sh — bench test of canteiro-mosaico.py away from production: synthetic lenses,
# software codecs, two local mediamtx (one plays Frigate's go2rtc, one plays canteiro-hls).
#
#   ./teste-buraco.sh [workdir]        (Linux, as root for `ss -p`; ~8 min)
#
# Needs: ffmpeg (with libx264 + drawtext), python3, curl, iproute2 and a mediamtx binary
# (MEDIAMTX=/path/to/mediamtx). ffmpeg < 5 names the RTSP socket timeout `-stimeout`
# (SRC_TIMEOUT_OPT=-stimeout); ffmpeg 7, as in the LXC, `-timeout` (the default).
#
# Timeline (holes of 75 s, longer than anything the 10 s publisher timeout forgives):
#   A  fixed lens STALLS (publisher SIGSTOPped: connection up, no packets - the uplink hole)
#   B  both lenses END (publishers killed: EOF) and stay away
#   C  both lenses STALL
#   D  the "canteiro-hls" mediamtx restarts (what the watchdog's heal does)
# monitor.csv: one line every 2 s - HLS media sequence of mosaico-tv, bytes an RTSP reader of
# mosaico-tv has received, and the composer's own state. snap-*.jpg: the picture in each phase.
# Pass = the sequence and the bytes never stop in A-C (the reader is the same connection from
# start to the end of C), 0 encoder restarts before D, and the lenses come back by themselves.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
T=${1:-/tmp/mosaico-teste}
MTX=${MEDIAMTX:-/opt/mtx/mediamtx}
FF=${FFMPEG:-ffmpeg}
HOLE=${HOLE:-75}
rm -rf "$T"; mkdir -p "$T/run" "$T/www"; cd "$T" || exit 1
cat > src.yml <<EOF
logLevel: warn
rtspAddress: :18554
rtspTransports: [tcp]
rtmp: no
hls: no
webrtc: no
srt: no
moq: no
paths:
  all_others:
EOF
cat > dst.yml <<EOF
logLevel: info
rtspAddress: :18555
rtspTransports: [tcp]
rtmp: no
webrtc: no
srt: no
moq: no
hls: yes
hlsAddress: :18888
hlsVariant: fmp4
hlsSegmentCount: 7
hlsSegmentDuration: 4s
hlsAlwaysRemux: yes
paths:
  mosaico:
  mosaico-tv:
EOF
PIDS=()
cleanup() { touch "$T/stop"; kill "${PIDS[@]}" 2>/dev/null; pkill -f "$T/" 2>/dev/null; pkill -f 'rtsp://127.0.0.1:1855[45]' 2>/dev/null; }
trap cleanup EXIT
"$MTX" "$T/src.yml" > src.log 2>&1 & PIDS+=($!)
dst_loop() { while [ ! -e "$T/stop" ]; do "$MTX" "$T/dst.yml" >> dst.log 2>&1; sleep 1; done; }
dst_loop & PIDS+=($!)
"$FF" -v error -f lavfi -i "color=c=0x203040:s=1920x540,drawtext=text='PAINEL DE TESTE':fontsize=90:fontcolor=white:x=(w-tw)/2:y=(h-th)/2" -frames:v 1 -y www/painel.png
(cd www && python3 -m http.server 18557 >/dev/null 2>&1) & PIDS+=($!)
lens() {   # name lavfi-source : a publisher that comes back 2 s after it dies, like the real feed
  while [ ! -e "$T/stop" ]; do
    if [ ! -e "$T/off-$1" ]; then
      "$FF" -v error -re -f lavfi -i "$2=size=2304x1296:rate=15" -c:v libx264 -preset ultrafast -tune zerolatency \
        -b:v 1400k -g 60 -pix_fmt yuv420p -rtsp_transport tcp -f rtsp "rtsp://127.0.0.1:18554/$1" &
      echo $! > "$T/pid-$1"; wait $!
    fi
    sleep 2
  done
}
lens canteiro_fixa testsrc2 & PIDS+=($!)
lens canteiro testsrc & PIDS+=($!)
sleep 4
MOSAICO_HW=0 FFMPEG=$FF RUN_DIR=$T/run CINZA=$HERE/canteiro-mosaico-cinza.py \
  SRC_FIXA=rtsp://127.0.0.1:18554/canteiro_fixa SRC_PT=rtsp://127.0.0.1:18554/canteiro \
  OUT_BASE=rtsp://127.0.0.1:18555 PAINEL_URL=http://127.0.0.1:18557/painel.png \
  python3 "$HERE/canteiro-mosaico.py" > composer.log 2>&1 & COMPOSER=$!; PIDS+=($COMPOSER)
sleep 20
# one RTSP reader for the whole test: if the publisher ever went away, this would end
"$FF" -v error -rtsp_transport tcp -i rtsp://127.0.0.1:18555/mosaico-tv -c copy -f mpegts -y reader.ts & READER=$!; PIDS+=($READER)
T0=$(date +%s)
monitor() {
  echo "t,phase,hls_seq,reader_bytes,reader_alive,composer_alive,encoder_starts,fixa_live,pt_live,fixa_label,pt_label" > monitor.csv
  while [ ! -e "$T/stop" ]; do
    m=$(curl -s -m 3 "http://127.0.0.1:18888/mosaico-tv/index.m3u8?cookieCheck=1" | grep -m1 '\.m3u8')
    seq=$(curl -s -m 3 "http://127.0.0.1:18888/mosaico-tv/$m" | sed -n 's/#EXT-X-MEDIA-SEQUENCE://p')
    st=$(python3 - "$T/run/mosaico-estado.json" <<'PY'
import json, sys
try:
    s = json.load(open(sys.argv[1])); l = s["lentes"]
    print("%d,%d,%d,%s,%s" % (s["encoder"]["starts"], l["fixa"]["live"], l["pt"]["live"], l["fixa"]["label"], l["pt"]["label"]))
except Exception:
    print(",,,,")
PY
)
    echo "$(( $(date +%s) - T0 )),$(cat phase 2>/dev/null),${seq:-},$(stat -c %s reader.ts 2>/dev/null),$(kill -0 $READER 2>/dev/null && echo 1 || echo 0),$(kill -0 $COMPOSER 2>/dev/null && echo 1 || echo 0),$st" >> monitor.csv
    sleep 2
  done
}
snap() { "$FF" -v error -rtsp_transport tcp -i rtsp://127.0.0.1:18555/mosaico-tv -frames:v 1 -q:v 4 -y "snap-$1.jpg"; }
echo start > phase; monitor & PIDS+=($!)
sleep 20; snap 0-normal
echo A-fixa-stall > phase; kill -STOP "$(cat pid-canteiro_fixa)"; sleep $((HOLE - 10)); snap A-fixa-parada; sleep 10
kill -CONT "$(cat pid-canteiro_fixa)"; echo A-volta > phase; sleep 30; snap A-volta
echo B-ambas-eof > phase; touch off-canteiro off-canteiro_fixa; kill "$(cat pid-canteiro)" "$(cat pid-canteiro_fixa)"; sleep $((HOLE - 10)); snap B-ambas-fora; sleep 10
rm -f off-canteiro off-canteiro_fixa; echo B-volta > phase; sleep 30; snap B-volta
echo C-ambas-stall > phase; kill -STOP "$(cat pid-canteiro)" "$(cat pid-canteiro_fixa)"; sleep $((HOLE - 10)); snap C-ambas-paradas; sleep 10
kill -CONT "$(cat pid-canteiro)" "$(cat pid-canteiro_fixa)"; echo C-volta > phase; sleep 30; snap C-volta
echo D-hls-restart > phase; pkill -f "$T/dst.yml"; sleep 40; snap D-volta
echo fim > phase; sleep 4
touch stop
python3 - <<'PY'
import csv
rows = list(csv.DictReader(open("monitor.csv")))
def stretch(rows, key):
    worst = cur = 0; last = None
    for r in rows:
        cur = cur + 2 if r[key] == last else 0
        worst = max(worst, cur); last = r[key]
    return worst
abc = [r for r in rows if not r["phase"].startswith(("D", "fim"))]
print("samples: %d (A-C: %d)" % (len(rows), len(abc)))
print("A-C  longest stretch without a new HLS segment: %d s (segments are 4 s)" % stretch(abc, "hls_seq"))
print("A-C  longest stretch without reader bytes:      %d s" % stretch(abc, "reader_bytes"))
print("A-C  reader alive throughout: %s | composer alive throughout: %s" % (all(r["reader_alive"] == "1" for r in abc), all(r["composer_alive"] == "1" for r in rows)))
print("encoder starts before D: %s | at the end: %s" % (abc[-1]["encoder_starts"], rows[-1]["encoder_starts"]))
for ph in ("A-fixa-stall", "B-ambas-eof", "C-ambas-stall"):
    held = [r for r in rows if r["phase"] == ph and "SEM SINAL" in (r["fixa_label"] + r["pt_label"])]
    back = [r for r in rows if r["phase"] == ph[0] + "-volta" and r["fixa_live"] == "1" and r["pt_live"] == "1"]
    first = [r for r in rows if r["phase"] == ph[0] + "-volta"]
    print("%-14s labelled SEM SINAL: %s | both lenses live again %s s after the feed returned" % (ph, bool(held), int(back[0]["t"]) - int(first[0]["t"]) if back and first else "NEVER"))
PY
echo "--- composer.log"; cat composer.log
