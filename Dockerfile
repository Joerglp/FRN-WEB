# Gleiche Python-Version wie im Betrieb per systemd (Debian 13 "trixie")
FROM python:3.13-slim-trixie

LABEL org.opencontainers.image.title="FRN-WEB TX Server"
LABEL org.opencontainers.image.description="Browser-based PTT for Free Radio Network"

# WITH_WHISPER=true  → zusaetzlich faster-whisper (CPU) fuer lokales Whisper
# WITH_WHISPER=false → ohne ML-Abhaengigkeiten (default)
ARG WITH_WHISPER=false

# libgsm: FRN-Audio (GSM/WAV49), ffmpeg: MP3-Stream, Opus-Archiv, Resampling
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgsm1 \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-whisper.txt ./
RUN if [ "$WITH_WHISPER" = "true" ]; then \
        pip install --no-cache-dir -r requirements-whisper.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi

# Programmdateien flach kopieren (siehe .dockerignore)
COPY *.py *.html *.js ./

# Laufzeit-Daten auf Volumes: Notizbuch-Vorschlaege, Nachrichten, Rueckblick,
# Tagesprogramm, Stimmproben, Sprachvorlagen (stimmen_ref/, voices/), Tokens
ENV FRN_DATEN=/app/daten \
    FRN_CONFIG=/app/config/config.json \
    PYTHONUNBUFFERED=1
RUN mkdir -p /app/daten /opt/FRN/recordings /opt/FRN/archive/audio

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/config')"

# config/ (config.json, tx_users.json, tx_rooms.json) wird per Volume eingebunden
CMD ["python3", "frn_tx_server.py", \
     "--config", "/app/config/config.json", \
     "--users",  "/app/config/tx_users.json", \
     "--rooms",  "/app/config/tx_rooms.json"]
