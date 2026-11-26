# nut-upsd (mia) — NUT server for the mia rack nobreak (CyberPower CP1000PFCLCD, USB HID).
#
# Authoritative copy: scripts/raspberry-pi/mia-raspberrypi/docker/nut-upsd/
# Live build context:  mia-raspberrypi:~/nut-upsd/
# python:3.12-slim-bookworm is only used as "a Debian bookworm already cached on this
# Pi" (netoverview/canteiro-diario-print); nothing here needs Python.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends nut-server nut-client \
 && rm -rf /var/lib/apt/lists/*
COPY ups.conf upsd.conf upsd.users nut.conf /etc/nut/
COPY entrypoint.sh /entrypoint.sh
RUN chmod 640 /etc/nut/upsd.users /etc/nut/upsd.conf /etc/nut/ups.conf \
 && chown root:nut /etc/nut/* && chmod +x /entrypoint.sh
EXPOSE 3493
HEALTHCHECK --interval=30s --timeout=8s --start-period=30s \
  CMD upsc ups@127.0.0.1 ups.status >/dev/null 2>&1
ENTRYPOINT ["/entrypoint.sh"]
