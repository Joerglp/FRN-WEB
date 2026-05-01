FROM python:3.11-slim-bookworm

LABEL org.opencontainers.image.title="FRN-WEB TX Server"
LABEL org.opencontainers.image.description="Browser-based PTT for Free Radio Network"

# WITH_WHISPER=true  → faster-whisper (CPU) + scipy werden installiert
# WITH_WHISPER=false → schlankes Image ohne ML-Abhängigkeiten (default)
ARG WITH_WHISPER=false

# System-Pakete
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgsm1 \
    libgsm-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Basis Python-Abhängigkeiten
RUN pip install --no-cache-dir \
    aiohttp==3.9.* \
    numpy==1.26.* \
    scipy==1.12.*

# Optional: faster-whisper (Sprachtranskription, ~500 MB + Modell)
RUN if [ "$WITH_WHISPER" = "true" ]; then \
      pip install --no-cache-dir \
          faster-whisper==1.* \
          paho-mqtt==1.*; \
    fi

WORKDIR /app

# Alle Python-Skripte, HTML und JS flach kopieren
COPY *.py   ./
COPY *.html ./
COPY *.js   ./

# Verzeichnisse für Laufzeit-Daten
RUN mkdir -p /opt/FRN/recordings /opt/FRN/archive/audio

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/config')"

# config/, tx_users.json, tx_rooms.json werden via Volume eingebunden
CMD ["python3", "frn_tx_server.py", \
     "--config", "/app/config/config.json", \
     "--users",  "/app/config/tx_users.json", \
     "--rooms",  "/app/config/tx_rooms.json"]
