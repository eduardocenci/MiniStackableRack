# starlink-names — syncs Starlink router client names into netoverview
# nicknames. Deliberately a separate image from canteiro-timelapse (decisão
# Eduardo 2026-08-29): no camera concerns, no ffmpeg/rclone — stays small
# and rebuilds independently.
#
# Authoritative copy: scripts/raspberry-pi/ara-raspberrypi/docker/starlink-names/
# Live build context:  ara-raspberrypi:~/starlink-names/ (this folder plus
# starlink-names.py copied in)
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ARG SUPERCRONIC_VERSION=v0.2.34
ADD https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-arm64 /usr/local/bin/supercronic

# same grpcurl version the host binary had (v1.9.1, official release)
ARG GRPCURL_VERSION=1.9.1
ADD https://github.com/fullstorydev/grpcurl/releases/download/v${GRPCURL_VERSION}/grpcurl_${GRPCURL_VERSION}_linux_arm64.tar.gz /tmp/grpcurl.tgz
RUN tar -xzf /tmp/grpcurl.tgz -C /usr/local/bin grpcurl && rm /tmp/grpcurl.tgz \
 && chmod 0755 /usr/local/bin/supercronic /usr/local/bin/grpcurl

WORKDIR /app
COPY starlink-names.py crontab-starlink-names /app/

CMD ["supercronic", "-passthrough-logs", "/app/crontab-starlink-names"]
