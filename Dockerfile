FROM python:3.11-slim-bookworm

LABEL org.opencontainers.image.title="FRN-WEB TX Server"
LABEL org.opencontainers.image.description="Browser-based PTT for Free Radio Network"

# System dependencies: libgsm for codec, ffmpeg for stream relay
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgsm1 \
    libgsm-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
RUN pip install --no-cache-dir \
    aiohttp==3.9.* \
    numpy==1.26.* \
    scipy==1.12.*

WORKDIR /app

COPY server/ ./server/
COPY web/    ./web/

# Default config location (override via volume mount)
COPY config/ ./config/

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/config')"

CMD ["python3", "server/frn_tx_server.py", \
     "--config", "/app/config/config.json"]
