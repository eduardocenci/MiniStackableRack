# ups-monitor — bnu rack nobreak (SMS Premium 1500 VA) → WhatsApp alerts + activation stats.
#
# Authoritative copy: scripts/raspberry-pi/bnu-raspberrypi/docker/ups-monitor/
# Live build context:  bnu-raspberrypi:~/ups-monitor/
# Stdlib only. python:3.11-slim is already cached on this Pi (canteiro-vigia), so
# the build needs no network. tzdata comes from the host (read-only bind mount in
# compose.yml).
FROM python:3.11-slim

WORKDIR /app
COPY sms_serial.py ups_monitor.py /app/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8795
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8795/health', timeout=4)"
CMD ["python3", "/app/ups_monitor.py"]
