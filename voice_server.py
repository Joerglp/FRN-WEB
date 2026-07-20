#!/usr/bin/env python3
"""
Voice-Clone-TTS HTTP-Server für FRN — synthetisiert Text in einer geklonten Stimme.

Läuft als eigenständiger Dienst (Docker), analog zum whisper_server.py.
frn_tx_server.py verbindet sich via voice.remote_url in config.json.

Engine: Coqui XTTS-v2 (Zero-Shot-Voice-Cloning aus einem kurzen Referenz-Sample).

Endpunkte:
    POST /tts     Form/JSON {text, language=de, speed=1.0} -> WAV (24 kHz mono)
    GET  /health  Status

Umgebungsvariablen:
    VOICE_PORT      HTTP-Port           (default: 9002)
    VOICE_DEVICE    cpu | cuda          (default: cpu)
    VOICE_REF       Referenz-WAV-Pfad   (default: /ref/speaker.wav)
    VOICE_LANG      Standardsprache     (default: de)
    COQUI_TOS_AGREED  muss "1" sein (XTTS-Lizenz akzeptiert)
"""

import asyncio
import io
import logging
import os
import time
import wave
from pathlib import Path

import numpy as np
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice")

PORT      = int(os.environ.get("VOICE_PORT", 9002))
DEVICE    = os.environ.get("VOICE_DEVICE", "cpu")   # cpu | cuda | cuda-ondemand
REF_WAV   = os.environ.get("VOICE_REF", "/ref/speaker.wav")
DEF_LANG  = os.environ.get("VOICE_LANG", "de")
MODEL_ID  = "tts_models/multilingual/multi-dataset/xtts_v2"

# Geräte-Logik:
#   cpu            → Modell dauerhaft auf CPU (kein VRAM)
#   cuda           → Modell dauerhaft auf GPU (schnell, belegt VRAM permanent)
#   cuda-ondemand  → Modell im CPU-RAM; pro Synthese kurz auf GPU, danach zurück.
#                    Belegt VRAM nur während der ~2 s Rechnung. Bei GPU-OOM → CPU.
_ONDEMAND     = DEVICE == "cuda-ondemand"
_STORE_DEVICE = "cuda" if DEVICE == "cuda" else "cpu"
_COMPUTE_CUDA = DEVICE in ("cuda", "cuda-ondemand")

# Nur eine Synthese gleichzeitig (CPU/GPU-gebunden)
_synth_lock = asyncio.Lock()
_tts  = None
_xtts = None
# Sprecher-Latents-Cache: name -> (gpt_cond_latent, speaker_embedding)
_latents: dict = {}

SPEAKER_DIR = Path(os.environ.get("VOICE_REF_DIR", str(Path(REF_WAV).parent)))
SPEAKER_RE  = __import__("re").compile(r"^[a-z0-9_\-]{1,40}$")


def _speaker_wav(name: str) -> Path:
    """Referenz-WAV für einen Sprecher. 'default' = das klassische REF_WAV."""
    if name == "default":
        return Path(REF_WAV)
    return SPEAKER_DIR / f"{name}.wav"


def _load_model():
    """Lädt XTTS einmalig. Sprecher-Latents werden pro Sprecher lazily
    berechnet und gecacht (spart pro Synthese mehrere Sekunden)."""
    global _tts, _xtts
    if _xtts is not None:
        return
    log.info("Lade XTTS-v2 (Modus '%s', Ablage '%s') …", DEVICE, _STORE_DEVICE)
    from TTS.api import TTS
    _tts  = TTS(MODEL_ID).to(_STORE_DEVICE)
    _xtts = _tts.synthesizer.tts_model
    log.info("Modell geladen.")


def _builtin_latents(speaker: str):
    """Latents eines eingebauten XTTS-Studio-Sprechers.

    Sprecher-Namen kommen als Slug an ('aaron_dreschner' → 'Aaron Dreschner').
    None, wenn kein Builtin passt oder das Modell noch nicht geladen ist.
    """
    if _xtts is None:
        return None
    sm = getattr(_xtts, "speaker_manager", None)
    if sm is None or not getattr(sm, "speakers", None):
        return None
    want = speaker.replace("_", " ").lower()
    for name, data in sm.speakers.items():
        if name.lower() == want:
            return data["gpt_cond_latent"], data["speaker_embedding"]
    return None


def _get_latents(speaker: str):
    """Latents aus Cache oder frisch aus dem Referenz-WAV berechnen.
    Ohne eigenes Sample fällt die Suche auf die eingebauten Studio-Sprecher
    des XTTS-Modells zurück."""
    if speaker in _latents:
        return _latents[speaker]
    wav = _speaker_wav(speaker)
    if not wav.exists():
        built = _builtin_latents(speaker)
        if built is not None:
            log.info("Sprecher '%s': eingebauter XTTS-Studio-Sprecher", speaker)
            _latents[speaker] = built
            return built
        raise FileNotFoundError(f"Kein Stimm-Sample für Sprecher '{speaker}'")
    log.info("Berechne Sprecher-Latents für '%s' aus %s …", speaker, wav)
    gpt, emb = _xtts.get_conditioning_latents(
        audio_path=[str(wav)], gpt_cond_len=30, max_ref_length=60,
    )
    _latents[speaker] = (gpt, emb)
    return _latents[speaker]


def _infer(dev: str, text: str, language: str, speed: float, speaker: str):
    gpt, emb = _get_latents(speaker)
    return _xtts.inference(
        text=text,
        language=language,
        gpt_cond_latent=gpt.to(dev),
        speaker_embedding=emb.to(dev),
        temperature=0.6,
        length_penalty=1.0,
        repetition_penalty=3.0,
        top_k=50,
        top_p=0.85,
        speed=speed,
        enable_text_splitting=True,
    )


def _trim_tail(wav: "np.ndarray", rate: int = 24000,
               thresh: float = 0.02, pad_s: float = 0.20) -> "np.ndarray":
    """Entfernt NUR führende/abschließende Stille (RMS-basiert). Kein Schnitt an
    Innenlücken — XTTS setzt lange Pausen mitten in den Satz, die von einem
    Halluzinations-Schwanz nicht sicher zu unterscheiden sind; ein Lücken-Schnitt
    würde echten Text kappen. Ein evtl. Plapper-Schwanz bleibt lieber stehen, als
    dass ein Spruch zerhackt wird."""
    if wav.size == 0:
        return wav
    frame = max(1, int(0.02 * rate))
    n = wav.size // frame
    if n == 0:
        return wav
    blk = wav[:n * frame].reshape(n, frame)
    env = np.sqrt((blk.astype(np.float64) ** 2).mean(axis=1))
    speech = env > thresh
    if not speech.any():
        return wav
    first = int(np.argmax(speech))
    last  = n - 1 - int(np.argmax(speech[::-1]))
    start = max(0, first * frame - int(pad_s * rate))
    end   = min(wav.size, (last + 1) * frame + int(pad_s * rate))
    return wav[start:end]


def _synth_to_wav_bytes(text: str, language: str, speed: float,
                        speaker: str = "default") -> bytes:
    _load_model()
    import torch

    use_cuda = _COMPUTE_CUDA and torch.cuda.is_available()

    if _ONDEMAND and use_cuda:
        # Modell nur für diese Synthese auf die GPU schieben, danach VRAM freigeben.
        try:
            _xtts.to("cuda")
            try:
                out = _infer("cuda", text, language, speed, speaker)
            finally:
                _xtts.to("cpu")
                torch.cuda.empty_cache()
        except (torch.cuda.OutOfMemoryError, RuntimeError) as e:
            if isinstance(e, torch.cuda.OutOfMemoryError) or "out of memory" in str(e).lower():
                log.warning("GPU belegt/OOM — weiche für diese Ansage auf CPU aus")
                try:
                    _xtts.to("cpu")
                except Exception:
                    pass
                torch.cuda.empty_cache()
                out = _infer("cpu", text, language, speed, speaker)
            else:
                raise
    elif use_cuda:
        out = _infer("cuda", text, language, speed, speaker)
    else:
        out = _infer("cpu", text, language, speed, speaker)

    wav   = np.asarray(out["wav"], dtype=np.float32)
    wav   = _trim_tail(wav, rate=24000)
    pcm16 = (np.clip(wav, -1.0, 1.0) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


async def handle_tts(request: web.Request) -> web.Response:
    text = ""
    language = DEF_LANG
    speed = 1.0
    speaker = "default"

    if request.content_type and "application/json" in request.content_type:
        data = await request.json()
        text     = (data.get("text") or "").strip()
        language = (data.get("language") or DEF_LANG).strip()
        speed    = float(data.get("speed") or 1.0)
        speaker  = (data.get("speaker") or "default").strip()
    else:
        data = await request.post()
        text     = (data.get("text") or "").strip()
        language = (data.get("language") or DEF_LANG).strip()
        speaker  = (data.get("speaker") or "default").strip()
        try:
            speed = float(data.get("speed") or 1.0)
        except (TypeError, ValueError):
            speed = 1.0

    if not text:
        return web.json_response({"error": "kein Text"}, status=400)
    if speaker != "default" and not SPEAKER_RE.match(speaker):
        return web.json_response({"error": "ungültiger Sprecher-Name"}, status=400)
    if (not _speaker_wav(speaker).exists() and _xtts is not None
            and _builtin_latents(speaker) is None):
        return web.json_response(
            {"error": f"Sprecher '{speaker}' hat kein Stimm-Sample"}, status=404)
    if len(text) > 1000:
        text = text[:1000]
    speed = max(0.5, min(2.0, speed))

    t0 = time.time()
    try:
        async with _synth_lock:
            loop = asyncio.get_running_loop()
            wav_bytes = await loop.run_in_executor(
                None, _synth_to_wav_bytes, text, language, speed, speaker
            )
    except Exception as e:
        log.exception("TTS-Fehler")
        return web.json_response({"error": str(e)}, status=500)

    elapsed = time.time() - t0
    log.info("Synthese %.1fs (%d Zeichen, Sprecher %s): %s",
             elapsed, len(text), speaker, text[:60])
    return web.Response(
        body=wav_bytes,
        content_type="audio/wav",
        headers={"X-Synth-Seconds": f"{elapsed:.2f}"},
    )


async def handle_speaker_upload(request: web.Request) -> web.Response:
    """Stimm-Sample für einen Sprecher hochladen (roher WAV-Body).

    Ersetzt ein evtl. vorhandenes Sample und invalidiert den Latents-Cache.
    """
    name = request.match_info["name"].strip().lower()
    if not SPEAKER_RE.match(name):
        return web.json_response({"error": "ungültiger Sprecher-Name"}, status=400)
    body = await request.read()
    if len(body) < 40000:   # < ~1 s bei 24 kHz s16 — sicher zu kurz
        return web.json_response({"error": "Sample zu kurz"}, status=400)
    dest = SPEAKER_DIR / f"{name}.wav"
    tmp  = SPEAKER_DIR / f"_{name}.tmp"
    tmp.write_bytes(body)
    try:
        with wave.open(str(tmp), "rb") as wf:
            dur = wf.getnframes() / wf.getframerate()
    except Exception:
        tmp.unlink(missing_ok=True)
        return web.json_response({"error": "kein gültiges WAV"}, status=400)
    if dur < 5.0:
        tmp.unlink(missing_ok=True)
        return web.json_response(
            {"error": f"Sample zu kurz ({dur:.1f}s) — mindestens 5 s"}, status=400)
    tmp.replace(dest)
    _latents.pop(name, None)   # Cache invalidieren → nächste Synthese rechnet neu
    log.info("Sprecher '%s': neues Sample gespeichert (%.1fs)", name, dur)
    return web.json_response({"ok": True, "speaker": name,
                              "duration_s": round(dur, 1)})


async def handle_speakers_list(request: web.Request) -> web.Response:
    speakers = ["default"] + sorted(
        p.stem for p in SPEAKER_DIR.glob("*.wav")
        if not p.name.startswith("_") and SPEAKER_RE.match(p.stem)
        and p != Path(REF_WAV)
    )
    builtin = []
    if _xtts is not None:
        sm = getattr(_xtts, "speaker_manager", None)
        if sm is not None and getattr(sm, "speakers", None):
            builtin = sorted(n.lower().replace(" ", "_") for n in sm.speakers)
    return web.json_response({"speakers": speakers, "builtin": builtin})


async def handle_health(request: web.Request) -> web.Response:
    ref_ok = Path(REF_WAV).exists()
    return web.json_response({
        "status": "ok" if ref_ok else "no_ref",
        "engine": "xtts_v2",
        "device": DEVICE,
        "ref":    REF_WAV,
        "loaded": _xtts is not None,
    })


async def _warmup(app):
    # Modell im Hintergrund vorladen, damit die erste echte Anfrage schnell ist
    def _warm():
        _load_model()
        try:
            _get_latents("default")
        except Exception:
            log.warning("Default-Latents nicht vorberechenbar (kein REF_WAV?)")

    async def _bg():
        try:
            await asyncio.get_running_loop().run_in_executor(None, _warm)
        except Exception:
            log.exception("Warmup fehlgeschlagen")
    app["warmup"] = asyncio.create_task(_bg())


app = web.Application(client_max_size=32 * 1024 * 1024)
app.router.add_post("/tts",             handle_tts)
app.router.add_get ("/health",          handle_health)
app.router.add_post("/speaker/{name}",  handle_speaker_upload)
app.router.add_get ("/speakers",        handle_speakers_list)
app.on_startup.append(_warmup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)
