#!/bin/bash
# rpi-sd-scan — read-only surface scan of the SD card the root filesystem is on.
#
# Authoritative copy: scripts/raspberry-pi/rpi-health/ (MiniStackableRack repo)
# Live copy:          /usr/local/sbin/rpi-sd-scan, run monthly by rpi-sd-scan.timer
#                     (RandomizedDelaySec 6 h, Persistent) or by hand:
#                     sudo systemctl start rpi-sd-scan.service   (≈ 15 min for 32 GB)
#
# Reads every block of the card once — badblocks in its default read-only mode,
# nothing is written, safe on the mounted root — at idle I/O priority, counts
# the blocks the card could not deliver and measures the sustained read rate: a
# card that is wearing out gets slower and starts failing reads before the
# filesystem notices. Result → $STATE/scan as one line
#   "<epoch> <ok 0/1> <bad blocks> <bytes per second> <duration s>"
# (history appended to $STATE/scan.log), which rpi-health exports as
# rpi_sd_scan_* on the next cycle. A card with bad blocks turns the dashboard
# LED amber and, through globalnet's fleet alerts, warns the site's WhatsApp
# group that the card should be replaced.
set -u
STATE=${STATE_DIR:-/var/lib/rpi-health}
OUT=$STATE/scan.out
mkdir -p "$STATE"

maj_min=$(findmnt -n -o MAJ:MIN / | tr -d '[:space:]')   # findmnt pads the column
part=$(basename "$(readlink -f "/sys/dev/block/$maj_min")")
disk=$(basename "$(readlink -f "/sys/class/block/$part/..")")
dev=/dev/$disk
[[ -b $dev ]] || { echo "rpi-sd-scan: cannot resolve the root disk ($dev)"; exit 1; }
bytes=$(( $(cat "/sys/class/block/$disk/size") * 512 ))

start=$(date +%s)
echo "rpi-sd-scan: reading $dev ($(( bytes / 1000000000 )) GB) read-only at idle priority"
ionice -c3 nice -n19 badblocks -s -v -b 4096 "$dev" > "$OUT" 2>&1
end=$(date +%s); dur=$(( end - start )); (( dur > 0 )) || dur=1
# badblocks ends with "Pass completed, N bad blocks found. (r/w/c errors)"
bad=$(tr '\r' '\n' < "$OUT" | grep -oE 'Pass completed, [0-9]+ bad blocks' | grep -oE '[0-9]+' | tail -1)
if [[ -n $bad ]]; then ok=1; else ok=0; bad=0; fi
rate=$(( bytes / dur ))
echo "$end $ok $bad $rate $dur" > "$STATE/scan"
echo "$end $ok $bad $rate $dur" >> "$STATE/scan.log"
echo "rpi-sd-scan: $( (( ok )) && echo "done" || echo "INCOMPLETE" ) — $bad bad blocks, $(( rate / 1000000 )) MB/s, $(( dur / 60 )) min"
(( ok )) || { tail -3 "$OUT"; exit 1; }
