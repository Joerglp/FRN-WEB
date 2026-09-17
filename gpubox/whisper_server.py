#!/usr/bin/env python3
"""Whisper-Dienst fuer die GPU-Box (192.0.0.17:9001).

Ersatz fuer den bisherigen Dienst. Gleiche Schnittstelle wie bisher --
POST /transcribe mit den Formularfeldern "file" und "language", GET /health
mit {"device", "model", "status"} -- plus drei Ergaenzungen, die der Pi
bereits mitschickt bzw. braucht:

  1. initial_prompt  (Formularfeld, optional)
     Kontextsatz, der Whisper auf CB-Funk und die Ortsnamen einstellt. Der
     Pi schickt ihn seit 2026-09-17 mit; der alte Dienst hat ihn verworfen.

  2. hotwords        (Formularfeld, optional)
     Rufnamen der Runde (Peter, Hans, Robert, Anton, Heinrich, Joerg, Ingo).
     Die sind der haeufigste Erkennungsfehler und stehen in keinem
     Standardwortschatz.

  3. WHISPER_MODEL   (Umgebungsvariable, Vorgabe large-v3-turbo)
     Damit laesst sich large-v3 gegen large-v3-turbo testen, ohne den Code
     anzufassen:  WHISPER_MODEL=large-v3 systemctl restart whisper

Zusaetzlich liefert die Antwort jetzt avg_logprob und no_speech_prob mit.
Das ist Whispers eigenes Mass fuer "wie sicher war ich" und waere dem
Kontroll-Lauf auf dem Pi (zweiter Durchgang mit 3 % Tempoaenderung, kostet
pro Aufnahme einen weiteren GPU-Lauf) deutlich ueberlegen. Der Pi nutzt es
noch nicht -- erst umstellen, wenn dieser Dienst laeuft.

Start:  pip install flask faster-whisper
        python3 whisper_server.py            # oder ueber die bisherige Unit
"""
import os
import tempfile
import time

from flask import Flask, jsonify, request
from faster_whisper import WhisperModel

MODELL  = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
GERAET  = os.environ.get("WHISPER_DEVICE", "cuda")
COMPUTE = os.environ.get("WHISPER_COMPUTE", "float16")
PORT    = int(os.environ.get("WHISPER_PORT", "9001"))

app = Flask(__name__)
model = WhisperModel(MODELL, device=GERAET, compute_type=COMPUTE)


@app.get("/health")
def health():
    return jsonify({"status": "ok", "device": GERAET, "model": MODELL})


@app.post("/transcribe")
def transcribe():
    datei = request.files.get("file")
    if datei is None:
        return jsonify({"error": "kein file-Feld"}), 400

    sprache = (request.form.get("language") or "de").strip()
    prompt  = (request.form.get("initial_prompt") or "").strip()
    hotword = (request.form.get("hotwords") or "").strip()

    extra = {}
    if prompt:
        extra["initial_prompt"] = prompt
    if hotword:
        extra["hotwords"] = hotword

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        datei.save(tmp.name)
        tmp.close()
        t0 = time.time()
        segmente, info = model.transcribe(
            tmp.name, language=sprache, beam_size=5,
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
            no_speech_threshold=0.8,
            **extra,
        )
        segmente = list(segmente)
        text = " ".join(s.text.strip() for s in segmente).strip()
        # Mittelwert ueber die Segmente, nach Laenge gewichtet -- ein einzelnes
        # kurzes Segment soll das Gesamturteil nicht kippen.
        dauer = sum(max(0.0, s.end - s.start) for s in segmente) or 1.0
        logp  = sum(s.avg_logprob * max(0.0, s.end - s.start) for s in segmente) / dauer
        nosp  = max((s.no_speech_prob for s in segmente), default=0.0)
        return jsonify({
            "text": text,
            "language": info.language,
            "duration_s": round(time.time() - t0, 2),
            "avg_logprob": round(logp, 3),      # ueber -0.8 = brauchbar
            "no_speech_prob": round(nosp, 3),   # ueber 0.6 = wohl nur Rauschen
            "model": MODELL,
        })
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=False)
