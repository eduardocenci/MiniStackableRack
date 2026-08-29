# canteiro-jobs — shared image for the three canteiro WhatsApp job containers
# (canteiro-watchdog, canteiro-presenca, canteiro-sunset-compare).
#
# Authoritative copy: scripts/raspberry-pi/bnu-raspberrypi/docker/canteiro-jobs/
# Live build context:  bnu-raspberrypi:~/canteiro-jobs/  (this folder plus the
# three scripts copied in from their sibling folders — see README.md)
FROM python:3.13-slim

# ffmpeg + rclone: canteiro-sunset-compare; tzdata: ZoneInfo("America/Sao_Paulo")
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg rclone tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# supercronic: cron for containers (runs as PID 1, non-root OK, jobs log to
# stdout). Schedules honor the container's TZ env.
ARG SUPERCRONIC_VERSION=v0.2.34
ADD https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-arm64 /usr/local/bin/supercronic
RUN chmod 0755 /usr/local/bin/supercronic

WORKDIR /app
COPY canteiro-watchdog.py canteiro-presenca.py canteiro-sunset-compare.py \
     crontab-presenca crontab-sunset-compare /app/
