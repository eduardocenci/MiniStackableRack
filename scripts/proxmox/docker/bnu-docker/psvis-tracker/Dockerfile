FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
# fonts-inter: the cards' typeface (cards.py falls back to DejaVu Sans without it)
RUN apt-get update && apt-get install -y --no-install-recommends fonts-inter \
    && rm -rf /var/lib/apt/lists/*
# tzdata: python:3.12-slim ships no IANA database (America/Sao_Paulo)
RUN pip install --no-cache-dir requests==2.32.3 flask==3.0.3 matplotlib==3.9.2 tzdata
COPY app.py fr24.py report.py waha.py maptile.py db.py enroute.py metar.py cards.py ./
CMD ["python", "-u", "app.py"]
