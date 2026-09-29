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
install -m 0755 rpi-health.sh /usr/local/sbin/rpi-health
install -m 0644 rpi-health.service /etc/systemd/system/rpi-health.service
mkdir -p /var/lib/rpi-health
systemctl daemon-reload
systemctl enable --quiet rpi-health.service
systemctl restart rpi-health.service
sleep 4
systemctl --no-pager --lines=4 status rpi-health.service || true
echo "--- rpi_ metrics now served on :9100"
curl -s localhost:9100/metrics | grep -E '^rpi_(health|pmic|throttled_raw|throttle_events_24h)' || echo "(none yet — see journalctl -u rpi-health)"
