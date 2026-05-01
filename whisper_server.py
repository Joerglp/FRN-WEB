#!/usr/bin/env python3
"""
Whisper HTTP API Server — lokaler Fallback für FRN-Transkription.

Läuft als eigenständiger Dienst (Docker-Service oder systemd).
frn_transcription.py verbindet sich via whisper.remote_url in config.json.

Start (CPU/Pi):
    python3 whisper_server.py

Start (GPU-Rechner):
    WHISPER_MODEL=large-v3 WHISPER_DEVICE=cuda python3 whisper_server.py

Umgebungsvariablen:
    WHISPER_MODEL   Modellgröße (default: medium)
    WHISPER_DEVICE  cpu | cuda  (default: cpu)
    WHISPER_PORT    HTTP-Port   (default: 9001)
    CONFIG          Pfad zu config.json (für initial_prompt, default: ./config/config.json)
"""

import json
import logging
import os
import tempfile
import time
from pathlib import Path

import numpy as np
from aiohttp import web
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MODEL_SIZE = os.environ.get("WHISPER_MODEL",  "medium")
DEVICE     = os.environ.get("WHISPER_DEVICE", "cpu")
COMPUTE    = "float16" if DEVICE == "cuda" else "int8"
PORT       = int(os.environ.get("WHISPER_PORT", 9001))

CONFIG_PATH = Path(os.environ.get("CONFIG", Path(__file__).parent / "config" / "config.json"))

HALLUCINATIONS = {"", ".", "...", "vielen dank.", "danke.", "tschüss.",
                  "untertitel", "♪", "musik", "[musik]", "[applaus]"}

_default_prompt = (
    "CB-Funk, Kanal 74, Eickelborn, Lippstadt. "
    "Gespräch zwischen CB-Funkern. Kein Rufzeichen. "
    "Gängige Ausdrücke: Roger, Over, QRV, Kanal frei, Basis, Mobile, "
    "Signalstärke, Rapport, QRM, bis später, 77, tschüss."
)


def _load_initial_prompt() -> str:
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        return cfg.get("whisper", {}).get("initial_prompt", "").strip() or _default_prompt
    except Exception:
        return _default_prompt


log.info("Lade Whisper-Modell '%s' auf '%s' ...", MODEL_SIZE, DEVICE)
model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE)
log.info("Modell geladen. Whisper API auf Port %d", PORT)


async def handle_transcribe(request: web.Request) -> web.Response:
    reader = await request.multipart()
    wav_data = None
    language = "de"

    async for part in reader:
        if part.name == "file":
            wav_data = await part.read()
        elif part.name == "language":
            language = (await part.read()).decode()

    if not wav_data:
        return web.json_response({"error": "Keine Datei erhalten"}, status=400)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav_data)
        tmp_path = tmp.name

    try:
        initial_prompt = _load_initial_prompt()
        t0 = time.time()
        segments, info = model.transcribe(
            tmp_path,
            language=language,
            beam_size=5,
            best_of=3,
            initial_prompt=initial_prompt,
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.8,
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        )
        parts = [s.text.strip() for s in segments
                 if getattr(s, "no_speech_prob", 0.0) <= 0.8
                 and s.text.strip().lower() not in HALLUCINATIONS]
        text = " ".join(parts).strip()
        elapsed = time.time() - t0
        log.info("Transkript (%.1fs, %s): %s", elapsed, info.language, text[:80])
        return web.json_response({
            "text":       text,
            "language":   info.language,
            "duration_s": round(elapsed, 2),
        })
    finally:
        os.unlink(tmp_path)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "model": MODEL_SIZE, "device": DEVICE})


app = web.Application(client_max_size=50 * 1024 * 1024)  # max 50 MB WAV
app.router.add_post("/transcribe", handle_transcribe)
app.router.add_get("/health",      handle_health)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)
