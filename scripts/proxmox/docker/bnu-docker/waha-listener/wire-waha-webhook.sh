#!/bin/sh
# Wire the WAHA gateway to waha-listener — run INSIDE LXC 101 (bnu-proxmox).
# Idempotent: exits if the webhook is already configured. Reads the shared
# token from /opt/waha-listener/.env so no secret travels in a command line.
set -e
TOKEN=$(grep '^LISTENER_TOKEN=' /opt/waha-listener/.env | cut -d= -f2)
[ -n "$TOKEN" ] || { echo "LISTENER_TOKEN missing in /opt/waha-listener/.env"; exit 1; }
cd /opt/waha
if grep -q WHATSAPP_HOOK_URL docker-compose.yml; then
    echo "already wired"
else
    sed -i "/TZ: \"America\/Sao_Paulo\"/a\\      WHATSAPP_HOOK_URL: \"http://waha-listener:8000/webhook?token=${TOKEN}\"\\n      WHATSAPP_HOOK_EVENTS: \"message\"" docker-compose.yml
    echo "wired:"
    grep -c WHATSAPP_HOOK docker-compose.yml
fi
docker compose up -d
