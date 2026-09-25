#!/usr/bin/env python3
"""Speaker-ID API (ECAPA-TDNN, SpeechBrain) — Testdienst auf Port 9005.

Gleiche Schnittstelle wie speaker_id_server.py (POST /embed mit 'file',
liefert {"embedding": [...]}) , damit der Pi ohne Code-Aenderung umgestellt
werden kann. Resemblyzer liefert 256 Werte, ECAPA 192 — die eingelernten
Stimmproben muessen beim Wechsel also neu berechnet werden.
"""
import logging, os, tempfile, time
import numpy as np
import soundfile as sf
import torch, torchaudio
from flask import Flask, jsonify, request
from speechbrain.inference.speaker import EncoderClassifier

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)
app = Flask(__name__)

log.info("Lade ECAPA-TDNN (CPU) …")
_t0 = time.time()
MODELL = EncoderClassifier.from_hparams(
    source="speechbrain/spkrec-ecapa-voxceleb",
    savedir=os.path.expanduser("~/.cache/speechbrain/ecapa"),
    run_opts={"device": "cpu"})
log.info("Modell geladen (%.1fs)", time.time() - _t0)


@app.route("/embed", methods=["POST"])
def embed():
    if "file" not in request.files:
        return jsonify({"error": "kein 'file' im Request"}), 400
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        request.files["file"].save(tmp.name)
        pfad = tmp.name
    try:
        t0 = time.time()
        # torchaudio.load braucht seit 2.11 TorchCodec -- soundfile reicht
        # fuer die WAV-Dateien, die der Pi schickt.
        daten, rate = sf.read(pfad, dtype="float32", always_2d=True)
        signal = torch.from_numpy(np.ascontiguousarray(daten.mean(axis=1)[None, :]))
        # ECAPA erwartet 16 kHz; Funkaudio kommt teils mit 8 kHz.
        if rate != 16000:
            signal = torchaudio.functional.resample(signal, rate, 16000)
        if signal.shape[1] < 1600:            # < 0.1 s
            return jsonify({"error": "Audio zu kurz"}), 422
        with torch.no_grad():
            vec = MODELL.encode_batch(signal).squeeze().tolist()
        dt = time.time() - t0
        log.info("Embedding berechnet (%.2fs, %d Samples)", dt, signal.shape[1])
        return jsonify({"embedding": vec, "duration_s": round(dt, 2)})
    except Exception as e:
        log.exception("Embedding fehlgeschlagen")
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            os.unlink(pfad)
        except OSError:
            pass


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "model": "ecapa-tdnn", "device": "cpu", "dim": 192})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9005, threaded=False)
