FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
# tzdata is required: python:3.12-slim ships no IANA database, so ZoneInfo
# ("America/Sao_Paulo") would fail at runtime without it.
# fonts-dejavu-core: report.py renders the weekly presence JPEG with these TTFs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir requests==2.32.3 paho-mqtt==2.1.0 tzdata pillow==11.0.0
COPY app.py condfy.py mqtt_bridge.py waha.py report.py ./
CMD ["python", "-u", "app.py"]
