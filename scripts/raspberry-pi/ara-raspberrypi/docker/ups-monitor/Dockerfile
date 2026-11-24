# ups-monitor (ara) — canteiro nobreak (EcoFlow RIVER 3 Plus, USB HID) → WhatsApp alerts
# + activation stats for the globalnet Nobreak (UPS) card.
#
# Authoritative copy: scripts/raspberry-pi/ara-raspberrypi/docker/ups-monitor/
# Live build context:  ara-raspberrypi2:~/ups-monitor/
# Stdlib only. python:3.13-slim is already cached on this Pi (starlink-names), so
# the build needs no network. tzdata comes from the host (read-only bind mount in
# compose.yml).
FROM python:3.13-slim

WORKDIR /app
COPY ecoflow_hid.py ups_monitor.py /app/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8795
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8795/health', timeout=4)"
CMD ["python3", "/app/ups_monitor.py"]
