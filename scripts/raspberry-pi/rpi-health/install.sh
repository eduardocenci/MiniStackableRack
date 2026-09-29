#!/bin/bash
# Install or update rpi-health on this Pi (run as root, from the folder that
# holds rpi-health.sh + rpi-health.service). Idempotent: re-running restarts
# the service with the new script. deploy.py ships this folder and runs it.
set -eu
cd "$(dirname "$0")"
[[ -d /var/lib/prometheus/node-exporter ]] || {
    echo "prometheus-node-exporter is not installed (no /var/lib/prometheus/node-exporter) — apt install it first" >&2
    exit 1
}
command -v badblocks >/dev/null || { echo "badblocks (e2fsprogs) missing — apt install e2fsprogs" >&2; exit 1; }
install -m 0755 rpi-health.sh /usr/local/sbin/rpi-health
install -m 0644 rpi-health.service /etc/systemd/system/rpi-health.service
install -m 0755 rpi-sd-scan.sh /usr/local/sbin/rpi-sd-scan
install -m 0644 rpi-sd-scan.service rpi-sd-scan.timer /etc/systemd/system/
mkdir -p /var/lib/rpi-health
systemctl daemon-reload
systemctl enable --quiet rpi-health.service rpi-sd-scan.timer
systemctl restart rpi-health.service
systemctl start rpi-sd-scan.timer
# First install: take the baseline scan now (≈ 15 min in the background, idle
# priority); afterwards the monthly timer owns it.
if [[ ! -e /var/lib/rpi-health/scan ]]; then
    systemctl start --no-block rpi-sd-scan.service && echo "baseline SD scan started in the background (journalctl -u rpi-sd-scan)"
fi
sleep 4
systemctl --no-pager --lines=4 status rpi-health.service || true
echo "--- rpi_ metrics now served on :9100"
curl -s localhost:9100/metrics | grep -E '^rpi_(health|pmic|throttled_raw|throttle_events_24h|sd_info|sd_kernel_errors|sd_fs_errors|sd_scan_timestamp)' || echo "(none yet — see journalctl -u rpi-health)"
