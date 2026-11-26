# ups-monitor (mia) — rack nobreak (CyberPower CP1000PFCLCD, read through nut-upsd)
# → WhatsApp alerts + activation stats for the globalnet Nobreak (UPS) card.
#
# Authoritative copy: scripts/raspberry-pi/mia-raspberrypi/docker/ups-monitor/
# Live build context:  mia-raspberrypi:~/ups-monitor/
# Stdlib only. python:3.12-slim-bookworm is already cached on this Pi, so the build
# needs no network. tzdata comes from the host (read-only bind mount in compose.yml).
FROM python:3.12-slim-bookworm

WORKDIR /app
COPY nut_client.py ups_monitor.py /app/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8795
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8795/health', timeout=4)"
CMD ["python3", "/app/ups_monitor.py"]
