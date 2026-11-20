#!/usr/bin/env bash
# Make this Pi a Tailscale exit node. Idempotent — safe to re-run.
# Run on the Pi as a user with sudo: bash setup.sh
# The admin console must still approve the exit node once (see README.md).
set -euo pipefail

NETDEV=$(ip -o route get 8.8.8.8 | awk '{for (i=1;i<NF;i++) if ($i=="dev") print $(i+1)}')

# 1. Kernel forwarding, persistent. IPv4 is already on (Docker), IPv6 is not.
sudo tee /etc/sysctl.d/99-tailscale.conf >/dev/null <<'EOF'
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
EOF
sudo /usr/sbin/sysctl -p /etc/sysctl.d/99-tailscale.conf

# 2. UDP GRO forwarding on the WAN NIC (Tailscale's recommended throughput
#    tweak for Linux exit nodes), re-applied by NetworkManager on every up.
sudo tee /etc/NetworkManager/dispatcher.d/50-tailscale-gro >/dev/null <<EOF
#!/bin/sh
[ "\$1" = "$NETDEV" ] && [ "\$2" = "up" ] && /usr/sbin/ethtool -K "$NETDEV" rx-udp-gro-forwarding on rx-gro-list off
exit 0
EOF
sudo chmod 755 /etc/NetworkManager/dispatcher.d/50-tailscale-gro
sudo /usr/sbin/ethtool -K "$NETDEV" rx-udp-gro-forwarding on rx-gro-list off

# 3. Advertise. `set` keeps every other pref (unlike `up`).
sudo tailscale set --advertise-exit-node

echo "--- verify"
cat /proc/sys/net/ipv4/ip_forward /proc/sys/net/ipv6/conf/all/forwarding
sudo /usr/sbin/ethtool -k "$NETDEV" | grep -E 'rx-udp-gro-forwarding|rx-gro-list'
sudo tailscale debug prefs | grep -E '"AdvertiseRoutes"' -A3
