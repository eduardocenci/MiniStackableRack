# canteiro-jobs — shared image for the four canteiro job containers
# (canteiro-watchdog, canteiro-presenca, canteiro-sunset-compare, canteiro-diario).
#
# Authoritative copy: scripts/raspberry-pi/bnu-raspberrypi/docker/canteiro-jobs/
# Live build context:  bnu-raspberrypi:~/canteiro-jobs/  (this folder plus the
# scripts copied in from their sibling folders — see README.md)
FROM python:3.13-slim

# ffmpeg + rclone: canteiro-sunset-compare; tzdata: ZoneInfo("America/Sao_Paulo")
# chromium + poppler-utils + cups-client + fonts: canteiro-diario (HTML → PDF/JPG, lp)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg rclone tzdata ca-certificates \
      chromium poppler-utils cups-client fontconfig fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

# Diário fonts (OFL) — Barlow Condensed + Source Sans 3, the identity reviewed 09/09/2026
ADD https://github.com/google/fonts/raw/main/ofl/barlowcondensed/BarlowCondensed-SemiBold.ttf /usr/share/fonts/truetype/google/
ADD https://github.com/google/fonts/raw/main/ofl/barlowcondensed/BarlowCondensed-Bold.ttf /usr/share/fonts/truetype/google/
ADD https://github.com/google/fonts/raw/main/ofl/sourcesans3/SourceSans3%5Bwght%5D.ttf /usr/share/fonts/truetype/google/
RUN chmod 0644 /usr/share/fonts/truetype/google/* && fc-cache -f

# numpy + pillow: alinhamento por software das grades do por do sol
# (canteiro-sunset-compare + pilar_align — mede o pilar e desloca+recorta)
# gspread: PlanejadoRealizado (canteiro-diario); paramiko: condfy tags via bnu-proxmox (canteiro-diario)
RUN pip install --no-cache-dir numpy pillow gspread paramiko

# supercronic: cron for containers (runs as PID 1, non-root OK, jobs log to
# stdout). Schedules honor the container's TZ env.
ARG SUPERCRONIC_VERSION=v0.2.34
ADD https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-arm64 /usr/local/bin/supercronic
RUN chmod 0755 /usr/local/bin/supercronic

WORKDIR /app
COPY canteiro-watchdog.py canteiro-presenca.py canteiro-sunset-compare.py \
     pilar_align.py canteiro-diario.py diario_render.py \
     crontab-presenca crontab-sunset-compare crontab-diario /app/
