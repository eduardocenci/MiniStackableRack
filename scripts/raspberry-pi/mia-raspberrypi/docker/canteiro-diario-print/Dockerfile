# canteiro-diario-print — MIA print-only leg of the Diário de Obra ARA pipeline.
# Same script as bnu (canteiro-diario.py, copied in), light image: rclone + cups-client.
# Authoritative copy: scripts/raspberry-pi/mia-raspberrypi/docker/canteiro-diario-print/
# Live build context:  mia-raspberrypi:~/canteiro-diario-print/
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends rclone cups-client tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# no pillow: the print leg never touches images (PIL is imported lazily by `collect` only)

# mia-raspberrypi runs a 32-bit userland (Raspbian 11, docker arch "arm") on an aarch64
# kernel — the image is linux/arm/v7, so supercronic must be the 32-bit arm build.
ARG SUPERCRONIC_VERSION=v0.2.34
ARG SUPERCRONIC_ARCH=arm
ADD https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${SUPERCRONIC_ARCH} /usr/local/bin/supercronic
RUN chmod 0755 /usr/local/bin/supercronic

WORKDIR /app
COPY canteiro-diario.py crontab-diario-print /app/
