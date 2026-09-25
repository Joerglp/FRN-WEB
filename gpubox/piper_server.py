#!/usr/bin/env python3
"""
Piper-TTS HTTP-Server für FRN — schnelle, lokale Sprachausgabe (kein GPU/Cloud).

Läuft als eigenständiger Dienst (Docker) auf dem Pi, getrennt vom MQTT-Piper.
Spricht dieselbe Schnittstelle wie voice_server.py (XTTS), damit der TX-Server
ihn ohne Code-Änderung über voice.remote_url ansprechen kann.

Engine: Piper (rhasspy), Modell de_DE-thorsten_emotional (8 Emotionen).

Mehrere Stimmen (2026-09-23): alle .onnx-Dateien im Modellverzeichnis stehen
zur Auswahl, die Anfrage waehlt mit "voice" aus. Geladene Modelle bleiben im
Speicher (LRU, PIPER_CACHE Stueck), damit ein Wechsel nicht jedes Mal das
komplette Modell nachlaedt.

Endpunkte:
    POST /tts     JSON {text, voice?, speaker?} -> WAV (22,05 kHz mono)
                  voice   = Modellname, z.B. "de_DE-karlsson-low"
                            (fehlt/unbekannt -> Standardmodell PIPER_MODEL)
                  speaker = Emotion/Sprecher innerhalb des Modells
                            (unbekannt -> Standard-Emotion PIPER_EMOTION)
    GET  /voices  Liste aller Stimmen mit ihren Sprechern/Emotionen
    GET  /health  Status
    GET  /speakers  Emotionen des Standardmodells

Umgebungsvariablen:
    PIPER_PORT     HTTP-Port            (default: 9003)
    PIPER_MODEL    Pfad zur .onnx       (default: /models/de_DE-thorsten_emotional-medium.onnx)
    PIPER_EMOTION  Standard-Emotion     (default: amused)
    PIPER_CACHE    gleichzeitig geladene Modelle (default: 3)
"""

import asyncio
import io
from collections import OrderedDict
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
# >1.0 = langsamer, <1.0 = schneller (Piper-Standard ist 1.0)
LENGTH_SCALE = float(os.environ.get("PIPER_LENGTH_SCALE", "1.15"))
MODELL_DIR = os.path.dirname(os.path.abspath(MODEL)) or "."
CACHE_MAX  = max(1, int(os.environ.get("PIPER_CACHE", "3")))

# Emotionen des thorsten_emotional-Modells (speaker_id_map). Bei Ein-Stimm-
# Modellen ohne Emotionen bleibt _EMO leer und speaker_id ist None.
_EMO: dict = {}
_voice = None
_synth_lock = asyncio.Lock()   # onnxruntime: eine Synthese gleichzeitig
# Name -> (PiperVoice, Sprecher-Map); zuletzt benutzte hinten (LRU).
_geladen: "OrderedDict[str, tuple]" = OrderedDict()


def _standard_name() -> str:
    return os.path.splitext(os.path.basename(MODEL))[0]


def _vorhandene_stimmen() -> list[str]:
    """Alle .onnx im Modellverzeichnis (ohne Endung), alphabetisch."""
    try:
        return sorted(f[:-5] for f in os.listdir(MODELL_DIR) if f.endswith(".onnx"))
    except OSError as e:
        log.warning("Modellverzeichnis %s nicht lesbar: %s", MODELL_DIR, e)
        return [_standard_name()]


def _lade_stimme(name: str):
    """Stimme aus dem Zwischenspeicher oder von der Platte. Liefert
    (PiperVoice, Sprecher-Map). Unbekannter Name -> Standardmodell."""
    if name in _geladen:
        _geladen.move_to_end(name)
        return _geladen[name]
    pfad = os.path.join(MODELL_DIR, name + ".onnx")
    if not os.path.exists(pfad):
        log.warning("Stimme %s nicht vorhanden -- nehme %s", name, _standard_name())
        name, pfad = _standard_name(), MODEL
        if name in _geladen:
            _geladen.move_to_end(name)
            return _geladen[name]
    from piper import PiperVoice
    t0 = time.time()
    log.info("Lade Piper-Modell %s …", name)
    stimme = PiperVoice.load(pfad)
    cfg = getattr(stimme, "config", None)
    smap = getattr(cfg, "speaker_id_map", None) or {}
    eintrag = (stimme, {str(k).lower(): int(v) for k, v in smap.items()})
    _geladen[name] = eintrag
    log.info("Modell %s geladen (%.1fs). Sprecher: %s",
             name, time.time() - t0, list(eintrag[1]) or "(keine)")
    while len(_geladen) > CACHE_MAX:
        raus, _ = _geladen.popitem(last=False)
        log.info("Modell %s aus dem Speicher entfernt (Zwischenspeicher voll)", raus)
    return eintrag


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


def _synth_wav(text: str, speaker: str | None, voice: str | None = None) -> bytes:
    if voice:
        _voice_obj, smap = _lade_stimme(voice)
    else:
        _load()
        _voice_obj, smap = _voice, _EMO
    key = (speaker or "").strip().lower()
    sid = None
    if smap:
        sid = smap.get(key, smap.get(DEF_EMO, 0))
    try:
        from piper import SynthesisConfig
        # length_scale IMMER setzen (auch ohne speaker_id) -- sonst greift die
        # Sprechgeschwindigkeit bei Modellen ohne Emotionen (z.B. thorsten-high)
        # gar nicht.
        syn = SynthesisConfig(speaker_id=sid, length_scale=LENGTH_SCALE)
    except Exception:
        syn = None
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        if syn is not None:
            _voice_obj.synthesize_wav(text, wf, syn_config=syn)
        elif sid is not None:
            _voice_obj.synthesize_wav(text, wf, speaker_id=sid)
        else:
            _voice_obj.synthesize_wav(text, wf)
    return buf.getvalue()


async def handle_tts(request: web.Request) -> web.Response:
    text = ""
    speaker = None
    voice = None
    if request.content_type and "application/json" in request.content_type:
        data = await request.json()
    else:
        data = await request.post()
    text    = (data.get("text") or "").strip()
    speaker = (data.get("speaker") or "").strip() or None
    voice   = (data.get("voice") or "").strip() or None
    if not text:
        return web.json_response({"error": "kein Text"}, status=400)
    if len(text) > 1000:
        text = text[:1000]

    t0 = time.time()
    try:
        async with _synth_lock:
            loop = asyncio.get_running_loop()
            wav = await loop.run_in_executor(None, _synth_wav, text, speaker, voice)
    except Exception as e:
        log.exception("TTS-Fehler")
        return web.json_response({"error": str(e)}, status=500)
    elapsed = time.time() - t0
    log.info("Synthese %.2fs (%d Zeichen, Stimme %s, Emotion %s): %s",
             elapsed, len(text), voice or _standard_name(), speaker or DEF_EMO, text[:60])
    return web.Response(body=wav, content_type="audio/wav",
                        headers={"X-Synth-Seconds": f"{elapsed:.2f}"})


async def handle_speakers(request: web.Request) -> web.Response:
    return web.json_response({"emotions": list(_EMO), "default": DEF_EMO})


async def handle_voices(request: web.Request) -> web.Response:
    """Alle Stimmen im Modellverzeichnis. Sprecher/Emotionen kommen aus der
    .onnx.json -- die ist klein, dafuer muss kein Modell geladen werden."""
    import json as _json
    out = []
    for name in _vorhandene_stimmen():
        sprecher = []
        try:
            with open(os.path.join(MODELL_DIR, name + ".onnx.json"), encoding="utf-8") as f:
                smap = (_json.load(f).get("speaker_id_map") or {})
            sprecher = list(smap)
        except (OSError, ValueError):
            pass
        out.append({"name": name, "speakers": sprecher,
                    "geladen": name in _geladen or name == _standard_name()})
    return web.json_response({"voices": out, "default": _standard_name(),
                              "default_emotion": DEF_EMO})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok", "engine": "piper", "model": os.path.basename(MODEL),
        "default_emotion": DEF_EMO, "loaded": _voice is not None,
        "voices": len(_vorhandene_stimmen()), "im_speicher": list(_geladen),
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
app.router.add_get("/voices",    handle_voices)
app.router.add_get("/health",    handle_health)
app.on_startup.append(_warmup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)
