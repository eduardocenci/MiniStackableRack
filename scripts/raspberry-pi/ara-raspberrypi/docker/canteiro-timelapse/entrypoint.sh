#!/bin/sh
# Upload-on-start replaces the old timer's Persistent=true: after a shed
# power cut, the container comes back with Docker and drains whatever the
# outage stranded in the outbox. rclone move is idempotent — an empty outbox
# or a repeat run is a no-op. A failure must not block the scheduler.
rclone move /var/lib/timelapse/outbox ceuazul:Timelapse --min-age 1m --log-level INFO \
  || echo "start-time upload failed (next try: the 20:00 cron)"
exec supercronic -passthrough-logs /app/crontab-timelapse
