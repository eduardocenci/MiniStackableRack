#!/bin/sh
# Upload-on-start replaces the old timer's Persistent=true: after a shed
# power cut, the container comes back with Docker and drains whatever the
# outage stranded in the outbox. rclone move is idempotent — an empty outbox
# or a repeat run is a no-op. A failure must not block the scheduler.
#
# Resume (29/10/2026): the shed lost power on 27/10 ~18:27 (Pi, Starlink
# dish and router all rebooted) — the sunset sequence died after its first
# window, no Luminancia125 that day, no Dia de Trabalho grid on 27 AND
# 28/10 (the grid uses the previous day as baseline). When the container
# (re)starts inside a sequence's span — power back, or the clock-resync
# restart after NTP — today's solar frames stay in the outbox (the resumed
# run needs them for the luminance choice; the 20:00 upload takes them) and
# the sequence is relaunched: it skips the windows already shot and reports
# the ones the outage really lost. A flock inside the script keeps a cron
# start and a resume from running twice. `( … & )` double-forks so the
# resumed run is reaped by tini, not left as a zombie under supercronic.
SEQ=$(timelapse-capture inprogress 2>/dev/null)
TODAY=$(date +%F)
case "$SEQ" in
  sunrise|sunset)
    echo "sequencia '$SEQ' em andamento: quadros solares de hoje ficam no outbox e a sequencia e retomada"
    rclone move /var/lib/timelapse/outbox ceuazul:Timelapse --min-age 1m --log-level INFO \
      --exclude "/posicao*/*/${TODAY}_*.jpg" --exclude "/lentefixa/*/${TODAY}_*.jpg" \
      || echo "start-time upload failed (next try: the 20:00 cron)"
    ( timelapse-capture "$SEQ" --resume & )
    ;;
  *)
    rclone move /var/lib/timelapse/outbox ceuazul:Timelapse --min-age 1m --log-level INFO \
      || echo "start-time upload failed (next try: the 20:00 cron)"
    ;;
esac
exec supercronic -passthrough-logs /app/crontab-timelapse
