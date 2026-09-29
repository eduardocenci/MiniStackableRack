#!/bin/bash
# rpi-health — Raspberry Pi thermal/power health for the globalnet dashboard.
#
# Authoritative copy: scripts/raspberry-pi/rpi-health/ (MiniStackableRack repo)
# Live copy:          /usr/local/sbin/rpi-health, run by rpi-health.service
#
# Why a host service and not a container: the PMIC temperature and the
# throttle flags only come from `vcgencmd` (firmware mailbox via /dev/vchiq),
# which the netoverview image cannot reach. prometheus-node-exporter is
# already on every Pi and globalnet already scrapes it for the cpu/ram bars,
# so this writes one textfile into the exporter's collector directory and
# nothing else in the chain changes (the SoC temperature, disk, boot time,
# clock, swap, OOM, NTP and link counters are in the scrape already).
#
# Every POLL_S seconds: `vcgencmd get_throttled` → four causes, each with a
# "now" bit (0-3) and a "since boot" bit (16-19):
#   bit 0/16 under_voltage    bit 1/17 freq_capped
#   bit 2/18 throttled        bit 3/19 soft_temp_limit
# A rising edge of a now-bit — or a since-boot bit that appears without its
# now-bit having been seen (an event shorter than one poll) — is one EVENT,
# appended to $EVENTS as "<epoch> <cause>". Exception: a freq_capped /
# throttled / soft_temp_limit edge while under-voltage is active (or within
# DEDUP_S of an under-voltage event) is the firmware's brownout response, not
# a thermal event, and is NOT logged — so those three counts mean thermal
# throttling and a brownout is one event. Under-voltage events the kernel
# logged before this service started ("Undervoltage detected!" — the hwmon
# driver polls the same firmware flag every 2 s) are seeded from
# `journalctl -k -b` at start, so the 24 h count is right from the first
# scrape. Every WRITE_EVERY polls, or right after an event, the PMIC
# temperature and the counts are written to $PROM (tmp + rename, so the
# collector never reads a half file). Events older than RETAIN_S are pruned
# hourly; the 24 h / 7 d counts survive reboots because the log is on disk.
set -u

PROM_DIR=${PROM_DIR:-/var/lib/prometheus/node-exporter}
PROM=$PROM_DIR/rpi.prom
STATE=${STATE_DIR:-/var/lib/rpi-health}
EVENTS=$STATE/events
STICKY=$STATE/sticky            # "<boot_id> <since-boot bits>" last seen
POLL_S=${POLL_S:-2}
WRITE_EVERY=${WRITE_EVERY:-15}  # polls between writes: 15 × 2 s = the dashboard's 30 s
RETAIN_S=$((8 * 86400))         # the 7-day count needs 7 days of events
DEDUP_S=5                       # same cause within 5 s = the same event
VC=${VC:-/usr/bin/vcgencmd}
CAUSES=(under_voltage freq_capped throttled soft_temp_limit)

log() { echo "rpi-health: $*"; }

has_event_near() {  # <epoch> <cause> → true if an event of that cause exists within DEDUP_S
    awk -v ts="$1" -v c="$2" -v d="$DEDUP_S" \
        '$2 == c && $1 - ts < d && ts - $1 < d { f = 1 } END { exit !f }' "$EVENTS"
}

add_event() {       # <epoch> <cause> → true if it was new
    has_event_near "$1" "$2" && return 1
    echo "$1 $2" >> "$EVENTS"
    log "event: $2 at $(date -d "@$1" '+%F %T')"
}

prune_events() {
    local cut=$(( $(date +%s) - RETAIN_S ))
    awk -v cut="$cut" '$1 >= cut' "$EVENTS" > "$EVENTS.tmp" && mv "$EVENTS.tmp" "$EVENTS"
}

seed_journal() {    # under-voltage transitions the kernel logged this boot
    local n=0 ts
    while read -r ts _; do
        add_event "${ts%%.*}" under_voltage && n=$((n + 1))
    done < <(journalctl -k -b -q -o short-unix 2>/dev/null | grep -iE 'under-?voltage detected')
    log "journal seed: $n new under-voltage event(s) this boot"
}

read_throttled() {  # → decimal value of get_throttled, or failure
    local v
    v=$($VC get_throttled 2>/dev/null) || return 1
    v=${v#throttled=}
    [[ $v == 0x* ]] || return 1
    echo $(( v ))
}

read_pmic_temp() {  # → "58.4", or nothing when the firmware has no PMIC sensor
    local v
    v=$($VC measure_temp pmic 2>/dev/null) || return 1
    v=${v#temp=}; v=${v%\'C}
    [[ $v =~ ^[0-9]+(\.[0-9]+)?$ ]] && echo "$v"
}

write_prom() {      # <epoch> <raw>
    local now=$1 raw=$2 pmic c i l d w
    local -A last d24 d7
    pmic=$(read_pmic_temp)
    while read -r c l d w; do last[$c]=$l; d24[$c]=$d; d7[$c]=$w; done < <(awk -v now="$now" -v causes="${CAUSES[*]}" '
        BEGIN { n = split(causes, C, " ") }
        { if ($1 > L[$2]) L[$2] = $1; if (now - $1 < 86400) D[$2]++; if (now - $1 < 604800) W[$2]++ }
        END { for (i = 1; i <= n; i++) printf "%s %d %d %d\n", C[i], L[C[i]] + 0, D[C[i]] + 0, W[C[i]] + 0 }' "$EVENTS")
    {
        echo '# HELP rpi_health_sampled_timestamp_seconds When rpi-health last wrote this file (globalnet treats > 5 min as stale).'
        echo '# TYPE rpi_health_sampled_timestamp_seconds gauge'
        echo "rpi_health_sampled_timestamp_seconds $now"
        if [[ -n $pmic ]]; then
            echo '# HELP rpi_pmic_temp_celsius PMIC temperature (vcgencmd measure_temp pmic).'
            echo '# TYPE rpi_pmic_temp_celsius gauge'
            echo "rpi_pmic_temp_celsius $pmic"
        fi
        echo '# HELP rpi_throttled_raw vcgencmd get_throttled as an integer (bits 0-3 now, 16-19 since boot).'
        echo '# TYPE rpi_throttled_raw gauge'
        echo "rpi_throttled_raw $raw"
        echo '# HELP rpi_throttle_active 1 while the cause is active right now.'
        echo '# TYPE rpi_throttle_active gauge'
        for i in "${!CAUSES[@]}"; do echo "rpi_throttle_active{cause=\"${CAUSES[$i]}\"} $(( (raw >> i) & 1 ))"; done
        echo '# HELP rpi_throttle_since_boot 1 if the cause has occurred since boot (firmware sticky bit).'
        echo '# TYPE rpi_throttle_since_boot gauge'
        for i in "${!CAUSES[@]}"; do echo "rpi_throttle_since_boot{cause=\"${CAUSES[$i]}\"} $(( (raw >> (16 + i)) & 1 ))"; done
        echo '# HELP rpi_throttle_last_event_timestamp_seconds Epoch of the last event of the cause (0 = none on record).'
        echo '# TYPE rpi_throttle_last_event_timestamp_seconds gauge'
        for c in "${CAUSES[@]}"; do echo "rpi_throttle_last_event_timestamp_seconds{cause=\"$c\"} ${last[$c]}"; done
        echo '# HELP rpi_throttle_events_24h Events of the cause in the last 24 h.'
        echo '# TYPE rpi_throttle_events_24h gauge'
        for c in "${CAUSES[@]}"; do echo "rpi_throttle_events_24h{cause=\"$c\"} ${d24[$c]}"; done
        echo '# HELP rpi_throttle_events_7d Events of the cause in the last 7 days.'
        echo '# TYPE rpi_throttle_events_7d gauge'
        for c in "${CAUSES[@]}"; do echo "rpi_throttle_events_7d{cause=\"$c\"} ${d7[$c]}"; done
    } > "$PROM.tmp" && mv "$PROM.tmp" "$PROM"
}

main() {
    local boot_id boot_ts now raw now_bits sticky prev_sticky seen_now=0 polls=0 changed i c last_uv=0
    local hour_polls=$(( 3600 / POLL_S ))
    [[ -d $PROM_DIR ]] || { log "$PROM_DIR missing — prometheus-node-exporter is not installed"; exit 1; }
    mkdir -p "$STATE"; touch "$EVENTS"
    boot_id=$(cat /proc/sys/kernel/random/boot_id)
    boot_ts=$(( $(date +%s) - $(cut -d. -f1 /proc/uptime) ))
    raw=$(read_throttled) || { log "vcgencmd get_throttled failed — is $VC installed and /dev/vchiq present?"; exit 1; }
    prune_events
    seed_journal
    # Since-boot bits already set when we start are NOT events: they happened
    # while nobody watched and cannot be dated (the journal seed above already
    # recovered the under-voltage ones the kernel saw this boot). They stay
    # visible as rpi_throttle_since_boot; the 24 h counts only ever hold dated
    # events, which is what the dashboard's LED promises.
    sticky=$(( (raw >> 16) & 0xF )); prev_sticky=$sticky
    echo "$boot_id $sticky" > "$STICKY"
    log "since-boot flags at start: $(printf '0x%x' "$sticky") (boot $(date -d "@$boot_ts" '+%F %T'))"
    log "started: get_throttled=$(printf '0x%x' "$raw"), poll ${POLL_S}s, write every $(( POLL_S * WRITE_EVERY ))s → $PROM"

    while :; do
        if ! raw=$(read_throttled); then
            sleep "$POLL_S"; continue    # transient firmware hiccup: skip the cycle, never "recover" on it
        fi
        now=$(date +%s)
        now_bits=$(( raw & 0xF )); sticky=$(( (raw >> 16) & 0xF )); changed=0
        for i in "${!CAUSES[@]}"; do
            c=${CAUSES[$i]}
            if (( i > 0 && ( now_bits & 1 || now - last_uv <= DEDUP_S ) )); then
                continue    # throttle bits raised by the brownout itself — not a thermal event
            fi
            if (( (now_bits >> i) & 1 && !((seen_now >> i) & 1) )); then
                add_event "$now" "$c" && { changed=1; (( i == 0 )) && last_uv=$now; }   # rising edge
            elif (( (sticky >> i) & 1 && !((prev_sticky >> i) & 1) && !((now_bits >> i) & 1) )); then
                add_event "$now" "$c" && { changed=1; (( i == 0 )) && last_uv=$now; }   # shorter than one poll
            fi
        done
        seen_now=$now_bits
        if (( sticky != prev_sticky )); then prev_sticky=$sticky; echo "$boot_id $sticky" > "$STICKY"; fi
        if (( changed || polls % WRITE_EVERY == 0 )); then write_prom "$now" "$raw"; fi
        if (( polls > 0 && polls % hour_polls == 0 )); then prune_events; fi
        polls=$(( polls + 1 ))
        sleep "$POLL_S"
    done
}

main
