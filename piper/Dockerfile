FROM python:3.11-slim

# ffmpeg nicht nötig — Piper liefert WAV; der TX-Server wandelt selbst um.
RUN pip install --no-cache-dir piper-tts aiohttp

WORKDIR /app
COPY piper_server.py /app/piper_server.py

# Modell wird als Volume gemountet (models/ -> /models), nicht ins Image gebacken.
ENV PIPER_MODEL=/models/de_DE-thorsten_emotional-medium.onnx \
    PIPER_EMOTION=amused \
    PIPER_PORT=9003

EXPOSE 9003
CMD ["python3", "piper_server.py"]
