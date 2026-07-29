FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
# tzdata is required: python:3.12-slim ships no IANA database, so ZoneInfo
# ("America/Sao_Paulo") would fail at runtime without it.
RUN pip install --no-cache-dir requests==2.32.3 paho-mqtt==2.1.0 tzdata
COPY app.py condfy.py mqtt_bridge.py waha.py ./
CMD ["python", "-u", "app.py"]
