#!/usr/bin/env python3
"""
Whisper HTTP API Server (für externen KI-Rechner mit GPU)
Empfängt WAV-Dateien per POST /transcribe, gibt JSON zurück.

Installation:  pip install faster-whisper flask
Start:         python3 whisper_server.py
"""
from flask import Flask, request, jsonify
from faster_whisper import WhisperModel
import tempfile, os, logging, time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

INITIAL_PROMPT = (
    "CB-Funk, Kanal 74, Eickelborn, Lippstadt, Hamm, Soest, NRW. "
    "Gespräch zwischen CB-Funkern. Kein Rufzeichen. "
    "Gängige Ausdrücke: Roger, Over, QRV, so kommt vor, so kommt rüber, "
    "Kanal frei, Basis, Mobile, Standort, Signalstärke, Rapport, QRM, "
    "Guten Morgen, Guten Abend, bis später, 77, tschüss."
)

MODEL_SIZE = os.environ.get("WHISPER_MODEL", "large-v3")
DEVICE     = os.environ.get("WHISPER_DEVICE", "cuda")   # "cpu" wenn keine GPU
COMPUTE    = "float16" if DEVICE == "cuda" else "int8"
PORT       = int(os.environ.get("WHISPER_PORT", 9001))

log.info("Lade Whisper %s auf %s ...", MODEL_SIZE, DEVICE)
model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE)
log.info("Modell geladen.")

app = Flask(__name__)

HALLUCINATIONS = {"", ".", "...", "vielen dank.", "danke.", "tschüss.",
                  "untertitel", "♪", "musik", "[musik]", "[applaus]"}

@app.route("/transcribe", methods=["POST"])
def transcribe():
    if "file" not in request.files:
        return jsonify({"error": "Keine Datei"}), 400
    lang = request.form.get("language", "de")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        request.files["file"].save(tmp.name)
        tmp_path = tmp.name
    try:
        t0 = time.time()
        segments, info = model.transcribe(
            tmp_path, language=lang, beam_size=5, best_of=3,
            initial_prompt=INITIAL_PROMPT,
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
        log.info("Transkript (%.1fs): %s", elapsed, text[:80])
        return jsonify({"text": text, "language": info.language, "duration_s": elapsed})
    finally:
        os.unlink(tmp_path)

@app.route("/health")
def health():
    return jsonify({"status": "ok", "model": MODEL_SIZE, "device": DEVICE})

if __name__ == "__main__":
    log.info("Whisper API Server auf Port %d ...", PORT)
    app.run(host="0.0.0.0", port=PORT, threaded=False)
