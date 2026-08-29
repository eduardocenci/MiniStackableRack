FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
# tzdata: python:3.12-slim ships no IANA database (America/Sao_Paulo)
RUN pip install --no-cache-dir requests==2.32.3 flask==3.0.3 matplotlib==3.9.2 tzdata
COPY app.py fr24.py report.py waha.py maptile.py db.py ./
CMD ["python", "-u", "app.py"]
