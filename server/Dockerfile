# Spukzentrale – Server
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

ENV SPUK_CONFIG=/data/config \
    SPUK_MEDIEN=/data/medien \
    TZ=Europe/Berlin \
    PYTHONUNBUFFERED=1

EXPOSE 8095
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8095/api/status', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8095", "--no-access-log"]
