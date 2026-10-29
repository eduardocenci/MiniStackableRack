#!/bin/sh
# mia-docker (LXC 102): Docker Engine from Docker's official apt repo (same tooling as bnu LXC 101).
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg >/dev/null
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable" > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
systemctl enable --now docker >/dev/null 2>&1
docker --version
docker compose version
docker run --rm hello-world 2>&1 | grep -E "Hello from Docker|Error|denied" | head -3
