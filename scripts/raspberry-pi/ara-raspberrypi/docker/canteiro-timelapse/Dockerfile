# canteiro-timelapse — camera-side jobs of the ARA canteiro: timelapse
# captures (sunrise/sunset/trabalho, both lenses, no camera movement since
# 2026-11-14), daily Drive upload.
#
# Authoritative copy: scripts/raspberry-pi/ara-raspberrypi/docker/canteiro-timelapse/
# Live build context:  ara-raspberrypi2:~/canteiro-timelapse/ (this folder plus
# timelapse-capture.py and canteiro-ptz.py copied in WITHOUT the .py suffix —
# canteiro-ptz is a manual tool only since 2026-11-14; see README.md)
FROM python:3.13-slim

# ffmpeg: keyframe grabs from the local relay; rclone: Drive upload;
# tzdata: ZoneInfo/TZ America/Sao_Paulo
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg rclone tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# numpy + pillow: luminância média dos quadros (timelapse-capture luminancia →
# Luminancia125); a re-âncora visual que também os usava saiu em 14/11/2026
RUN pip install --no-cache-dir numpy pillow

ARG SUPERCRONIC_VERSION=v0.2.34
ADD https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-arm64 /usr/local/bin/supercronic

COPY timelapse-capture canteiro-ptz /usr/local/bin/
RUN chmod 0755 /usr/local/bin/supercronic /usr/local/bin/timelapse-capture /usr/local/bin/canteiro-ptz

COPY crontab-timelapse entrypoint.sh /app/
RUN chmod 0755 /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
