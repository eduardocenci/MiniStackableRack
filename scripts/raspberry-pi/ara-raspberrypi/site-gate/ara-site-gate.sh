#!/bin/bash
# ara-site-gate — keep the ara stack dormant until this Pi is on the ara house LAN.
#
# Why: the replacement Pi (ara-raspberrypi2, 2026-09-28) was staged on the mia
# LAN with every image built/pulled but no container ever started — run
# off-site, netoverview would learn a foreign LAN into the presence DB (20:00
# obra report, bnu Frigate gate) and canteiro-timelapse would page SmokeTests
# all day for a camera that isn't there. At boot and every 2 min
# (ara-site-gate.timer) this checks for the ara LAN — a default route via
# 192.168.1.1 AND a Starlink gRPC port answering (router 192.168.1.1:9000 or
# dish 192.168.100.1:9200) — and only then: brings the six stacks up, installs
# the fleet netoverview update cron, re-seeds the hand-set netoverview
# nicknames, writes the armed marker, disables its own timer and posts one
# line to Casa SmokeTests. From then on the containers' restart policies
# (unless-stopped) own them — a power cut in the shed needs nothing from here.
#
# Authoritative copy: scripts/raspberry-pi/ara-raspberrypi/site-gate/ara-site-gate.sh
# Live copy:          /usr/local/sbin/ara-site-gate (+ ara-site-gate.{service,timer})
#
# Usage: ara-site-gate           check; arm if on the ara LAN
#        ara-site-gate --check   print the decision only, change nothing
#        ara-site-gate --force   arm WITHOUT the LAN check — staging rehearsal
#                                only, with the Pi OFF the tailnet; reset
#                                afterwards per README.md ("re-stage")
set -u

MARK=/var/lib/ara-site/armed
H=/home/eduardocenci
# start order: relay first (the timelapse grabs from it), netoverview before
# starlink-names (names are written through its API)
STACKS="canteiro-relay dvrip-bridge starlink-proxy netoverview starlink-names canteiro-timelapse"
# fleet standard line (scripts/raspberry-pi/README.md) — installed only on
# arming, so its `up -d` can never start netoverview off-site
CRON='*/5 * * * * cd ~/netoverview && docker compose pull -q && docker compose up -d && docker image prune -f >/dev/null'
# hand-set nicknames that died with the old Pi's netoverview DB (REMOTE_ACCESS.md
# ARA section, starlink-names/README.md); starlink-names re-derives the rest
NICKNAMES="74:24:9f:c5:b2:5f|Roteador Starlink
54:ba:d9:bd:34:e3|Câmera do canteiro (iM9)
22:17:94:50:d1:b3|iPhone (Jorge)"

log() { echo "ara-site-gate: $*"; }
tcp_open() { timeout 3 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }

at_ara() {
    if ! ip -4 route show default | grep -q ' via 192\.168\.1\.1 '; then
        log "not on the ara LAN (default via: $(ip -4 route show default | awk '{print $3}' | paste -sd' '))"
        return 1
    fi
    if tcp_open 192.168.1.1 9000 || tcp_open 192.168.100.1 9200; then
        return 0
    fi
    log "default route is 192.168.1.1 but no Starlink gRPC answers (router :9000, dish :9200) - not arming"
    return 1
}

compose_file() {
    local f
    for f in "$H/$1/compose.yml" "$H/$1/docker-compose.yml"; do
        [ -f "$f" ] && { echo "$f"; return; }
    done
}

notify() {  # best-effort; same WAHA path and creds as the timelapse failure alerts
    local envf="$H/canteiro-timelapse/env/alerts.env" url key sess jid body
    v() { sed -n "s/^$1=//p" "$envf" 2>/dev/null | head -n1; }
    url=$(v ALERT_WAHA_URL); key=$(v ALERT_WAHA_KEY); sess=$(v ALERT_WAHA_SESSION); jid=$(v ALERT_CHAT_JID)
    if [ -z "$url" ] || [ -z "$key" ] || [ -z "$jid" ]; then
        log "notify: alerts.env incomplete, skipped"
        return 0
    fi
    body=$(jq -n --arg s "${sess:-default}" --arg c "$jid" --arg t "$1" '{session:$s, chatId:$c, text:$t}')
    curl -sS -m 30 -o /dev/null -w "ara-site-gate: notify HTTP %{http_code}\n" \
        -H 'Content-Type: application/json' -H "X-Api-Key: $key" -d "$body" "$url/api/sendText" || true
}

if [ -e "$MARK" ]; then log "already armed ($(cat "$MARK"))"; exit 0; fi
if [ "${1:-}" = "--force" ]; then
    log "--force: skipping the ara LAN check (staging rehearsal)"
else
    at_ara || exit 0
    if [ "${1:-}" = "--check" ]; then log "on the ara LAN - would arm now"; exit 0; fi
fi

log "ara LAN detected - arming"
failed=""
for s in $STACKS; do
    f=$(compose_file "$s")
    if [ -z "$f" ]; then log "no compose file for $s"; failed="$failed $s"; continue; fi
    docker compose -f "$f" up -d || failed="$failed $s"
done

if ! crontab -u eduardocenci -l 2>/dev/null | grep -qF 'cd ~/netoverview'; then
    { crontab -u eduardocenci -l 2>/dev/null; echo "$CRON"; } | crontab -u eduardocenci -
    log "netoverview update cron installed"
fi

for _ in $(seq 1 30); do curl -s -m 3 -o /dev/null http://127.0.0.1:5000/ && break; sleep 2; done
while IFS='|' read -r mac nick; do
    [ -n "$mac" ] || continue
    curl -s -m 5 -o /dev/null -H 'Content-Type: application/json' \
        -d "$(jq -n --arg m "$mac" --arg n "$nick" '{mac:$m, nickname:$n}')" \
        http://127.0.0.1:5000/api/nickname && log "nickname $mac -> $nick"
done <<< "$NICKNAMES"

mkdir -p "$(dirname "$MARK")"
date -Is > "$MARK"
systemctl disable --now ara-site-gate.timer >/dev/null 2>&1 || true
up=$(docker ps --format '{{.Names}}' | sort | paste -sd' ')
if [ -z "$failed" ]; then
    notify "✅ *ara-raspberrypi2* ($(hostname)) armado na obra — no ar: $up"
else
    notify "⚠️ *ara-raspberrypi2* ($(hostname)) armado na obra, mas falhou:$failed — no ar: $up"
fi
log "armed; failed:${failed:- none}"
