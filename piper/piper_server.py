#!/usr/bin/env python3
"""
Piper-TTS HTTP-Server für FRN — schnelle, lokale Sprachausgabe (kein GPU/Cloud).

Läuft als eigenständiger Dienst (Docker) auf dem Pi, getrennt vom MQTT-Piper.
Spricht dieselbe Schnittstelle wie voice_server.py (XTTS), damit der TX-Server
ihn ohne Code-Änderung über voice.remote_url ansprechen kann.

Engine: Piper (rhasspy), Modell de_DE-thorsten_emotional (8 Emotionen).

Endpunkte:
    POST /tts     JSON {text, speaker?} -> WAV (22,05 kHz mono)
                  speaker = Emotion (amused|neutral|surprised|…) oder beliebig
                  (unbekannt -> Standard-Emotion PIPER_EMOTION).
    GET  /health  Status
    GET  /speakers  Liste der Emotionen

Umgebungsvariablen:
    PIPER_PORT     HTTP-Port            (default: 9003)
    PIPER_MODEL    Pfad zur .onnx       (default: /models/de_DE-thorsten_emotional-medium.onnx)
    PIPER_EMOTION  Standard-Emotion     (default: amused)
"""

import asyncio
import io
import logging
import os
import time
import wave

from aiohttp import web

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("piper")

PORT     = int(os.environ.get("PIPER_PORT", 9003))
MODEL    = os.environ.get("PIPER_MODEL",
                          "/models/de_DE-thorsten_emotional-medium.onnx")
DEF_EMO  = os.environ.get("PIPER_EMOTION", "amused").strip().lower()

# Emotionen des thorsten_emotional-Modells (speaker_id_map). Bei Ein-Stimm-
# Modellen ohne Emotionen bleibt _EMO leer und speaker_id ist None.
_EMO: dict = {}
_voice = None
_synth_lock = asyncio.Lock()   # onnxruntime: eine Synthese gleichzeitig


def _load():
    global _voice, _EMO
    if _voice is not None:
        return
    from piper import PiperVoice
    log.info("Lade Piper-Modell %s …", MODEL)
    _voice = PiperVoice.load(MODEL)
    cfg = getattr(_voice, "config", None)
    smap = getattr(cfg, "speaker_id_map", None) or {}
    _EMO = {str(k).lower(): int(v) for k, v in smap.items()}
    log.info("Modell geladen. Emotionen: %s", list(_EMO) or "(keine)")


def _speaker_id(name: str | None):
    """Emotion-Name -> speaker_id. Unbekannt/leer -> Standard-Emotion."""
    if not _EMO:
        return None
    key = (name or "").strip().lower()
    if key in _EMO:
        return _EMO[key]
    return _EMO.get(DEF_EMO, 0)


def _synth_wav(text: str, speaker: str | None) -> bytes:
    _load()
    sid = _speaker_id(speaker)
    try:
        from piper import SynthesisConfig
        syn = SynthesisConfig(speaker_id=sid) if sid is not None else None
    except Exception:
        syn = None
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        if syn is not None:
            _voice.synthesize_wav(text, wf, syn_config=syn)
        elif sid is not None:
            _voice.synthesize_wav(text, wf, speaker_id=sid)
        else:
            _voice.synthesize_wav(text, wf)
    return buf.getvalue()


async def handle_tts(request: web.Request) -> web.Response:
    text = ""
    speaker = None
    if request.content_type and "application/json" in request.content_type:
        data = await request.json()
        text    = (data.get("text") or "").strip()
        speaker = (data.get("speaker") or "").strip() or None
    else:
        data = await request.post()
        text    = (data.get("text") or "").strip()
        speaker = (data.get("speaker") or "").strip() or None
    if not text:
        return web.json_response({"error": "kein Text"}, status=400)
    if len(text) > 1000:
        text = text[:1000]

    t0 = time.time()
    try:
        async with _synth_lock:
            loop = asyncio.get_running_loop()
            wav = await loop.run_in_executor(None, _synth_wav, text, speaker)
    except Exception as e:
        log.exception("TTS-Fehler")
        return web.json_response({"error": str(e)}, status=500)
    elapsed = time.time() - t0
    log.info("Synthese %.2fs (%d Zeichen, Emotion %s): %s",
             elapsed, len(text), speaker or DEF_EMO, text[:60])
    return web.Response(body=wav, content_type="audio/wav",
                        headers={"X-Synth-Seconds": f"{elapsed:.2f}"})


async def handle_speakers(request: web.Request) -> web.Response:
    return web.json_response({"emotions": list(_EMO), "default": DEF_EMO})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok", "engine": "piper", "model": os.path.basename(MODEL),
        "default_emotion": DEF_EMO, "loaded": _voice is not None,
    })


async def _warmup(app):
    def _warm():
        try:
            _load()
            _synth_wav("Bereit.", None)   # onnxruntime + Modell aufwärmen
        except Exception:
            log.exception("Warmup fehlgeschlagen")
    await asyncio.get_running_loop().run_in_executor(None, _warm)


app = web.Application(client_max_size=8 * 1024 * 1024)
app.router.add_post("/tts",      handle_tts)
app.router.add_get("/speakers",  handle_speakers)
app.router.add_get("/health",    handle_health)
app.on_startup.append(_warmup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)
