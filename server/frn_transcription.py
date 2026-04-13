"""
FRN Transcription Pipeline
--------------------------
Puffert PCM-Audio einer TX-Session, speichert WAV, transkribiert via
faster-whisper (CPU) und veröffentlicht das Transkript per MQTT und Logfile.
"""

import asyncio
import json
import logging
import time
import wave
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# ── Whisper-Modell (wird beim ersten Aufruf geladen) ─────────────────────────

_whisper_model = None
_whisper_lock  = asyncio.Lock()

def _get_model(model_size: str):
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        log.info("Lade faster-whisper Modell '%s' ...", model_size)
        _whisper_model = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",        # schnell + wenig RAM auf ARM
        )
        log.info("Modell geladen.")
    return _whisper_model


def _transcribe_sync(wav_path: str, model_size: str, language: str) -> str:
    """Läuft im ThreadPool — blockiert den Event-Loop nicht."""
    from scipy.signal import resample_poly

    # WAV lesen und auf 16 kHz hochsampeln (FRN liefert 8 kHz)
    with wave.open(wav_path, "rb") as wf:
        src_rate = wf.getframerate()
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)

    audio = pcm.astype(np.float32) / 32768.0
    if src_rate != 16000:
        audio = resample_poly(audio, 16000, src_rate)

    # Normalisieren
    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio / peak

    model = _get_model(model_size)
    segments, _ = model.transcribe(
        audio,
        language=language,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 200},
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()
    return text


async def transcribe_wav(wav_path: str, model_size: str = "medium",
                         language: str = "de") -> str:
    """Transkribiert eine WAV-Datei via faster-whisper (CPU, non-blocking)."""
    async with _whisper_lock:           # nie zwei Inferenzen gleichzeitig
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _transcribe_sync, wav_path, model_size, language
        )


# ── MQTT-Publisher ────────────────────────────────────────────────────────────

def mqtt_publish(broker: str, port: int, topic: str, payload: str,
                 user: str = "", password: str = ""):
    """Synchrones MQTT-Publish (läuft im ThreadPool)."""
    try:
        import paho.mqtt.publish as publish
        auth = {"username": user, "password": password} if user else None
        publish.single(topic, payload=payload, hostname=broker, port=port,
                       auth=auth, qos=0, retain=False)
        log.debug("MQTT → %s: %s", topic, payload[:80])
    except Exception as e:
        log.warning("MQTT publish failed: %s", e)


# ── SessionRecorder ───────────────────────────────────────────────────────────

class SessionRecorder:
    """
    Puffert PCM-Daten einer TX-Session und schreibt am Ende eine WAV-Datei.
    Löst die Transkriptions-Pipeline aus sobald der Sender schweigt.
    """

    SILENCE_TIMEOUT = 1.5   # Sekunden ohne Audio → Session beendet
    SAMPLE_RATE     = 8000
    SAMPLE_WIDTH    = 2     # int16

    def __init__(self, room_name: str, cfg: dict,
                 pipeline: "TranscriptionPipeline"):
        self.room_name  = room_name
        self.cfg        = cfg
        self.pipeline   = pipeline
        self._buf: list[bytes] = []
        self._callsign  = ""
        self._start_ts  = 0.0
        self._timer: asyncio.TimerHandle | None = None
        self._active    = False

    def feed(self, pcm: bytes, callsign: str = ""):
        """PCM-Daten (s16le 8 kHz mono) einreichen."""
        if not self._active:
            self._active   = True
            self._start_ts = time.time()
            self._callsign = callsign
            self._buf.clear()
            log.debug("[%s] TX-Session gestartet (%s)", self.room_name, callsign)
        elif callsign:
            self._callsign = callsign

        self._buf.append(pcm)

        # Silence-Timer zurücksetzen
        if self._timer:
            self._timer.cancel()
        loop = asyncio.get_event_loop()
        self._timer = loop.call_later(self.SILENCE_TIMEOUT, self._on_silence)

    def _on_silence(self):
        if not self._active:
            return
        self._active = False
        self._timer  = None
        pcm_data     = b"".join(self._buf)
        self._buf.clear()
        if len(pcm_data) < self.SAMPLE_RATE * self.SAMPLE_WIDTH:
            log.debug("[%s] Session zu kurz — verworfen", self.room_name)
            return
        asyncio.ensure_future(
            self.pipeline.process(pcm_data, self.room_name,
                                  self._callsign, self._start_ts)
        )


# ── TranscriptionPipeline ─────────────────────────────────────────────────────

class TranscriptionPipeline:
    """Nimmt PCM entgegen, speichert WAV, transkribiert, loggt, MQTT."""

    def __init__(self, cfg: dict):
        self.cfg      = cfg
        self.wav_dir  = Path(cfg.get("wav_dir", "/opt/FRN/recordings"))
        self.wav_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = Path(cfg.get("log_file",
                                     "/opt/FRN/stream/transcription.log"))
        self._setup_cleanup()

    def _setup_cleanup(self):
        """Startet einen stündlichen Cleanup-Task für alte WAV-Dateien."""
        async def cleanup_loop():
            while True:
                await asyncio.sleep(3600)
                self._cleanup_old_wavs()
        asyncio.ensure_future(cleanup_loop())

    def _cleanup_old_wavs(self):
        max_age = int(self.cfg.get("max_age_days", 2))
        cutoff  = datetime.now() - timedelta(days=max_age)
        removed = 0
        for p in self.wav_dir.glob("*.wav"):
            try:
                if datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
                    p.unlink()
                    removed += 1
            except Exception:
                pass
        if removed:
            log.info("Cleanup: %d alte WAV(s) gelöscht", removed)

    def _save_wav(self, pcm: bytes, ts: float) -> Path:
        dt   = datetime.fromtimestamp(ts)
        name = dt.strftime("frn-%Y%m%d-%H%M%S") + ".wav"
        path = self.wav_dir / name
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(SessionRecorder.SAMPLE_WIDTH)
            wf.setframerate(SessionRecorder.SAMPLE_RATE)
            wf.writeframes(pcm)
        return path

    def _log(self, ts: float, room: str, callsign: str, text: str):
        dt   = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        line = f"{dt} [{room}] {callsign}: {text}\n"
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            log.warning("Log write failed: %s", e)
        log.info("Transkript %s", line.rstrip())

    async def process(self, pcm: bytes, room: str, callsign: str, ts: float):
        """WAV speichern → transkribieren → loggen → Archiv → MQTT."""
        wav_path = self._save_wav(pcm, ts)
        log.debug("[%s] WAV gespeichert: %s", room, wav_path)

        model_size = self.cfg.get("whisper_model", "medium")
        language   = self.cfg.get("whisper_language", "de")
        text = ""
        try:
            text = await asyncio.wait_for(
                transcribe_wav(str(wav_path), model_size, language),
                timeout=120.0
            )
        except Exception as e:
            log.warning("[%s] Whisper-Fehler: %s", room, e)

        if not text:
            log.debug("[%s] Kein Transkript erhalten", room)
            return

        self._log(ts, room, callsign, text)

        # ── Archiv ──
        try:
            from frn_archive import add_entry
            await add_entry(str(wav_path), room, callsign, ts, text)
        except Exception as e:
            log.warning("[%s] Archiv-Fehler: %s", room, e)

        # ── MQTT ──
        broker   = self.cfg.get("mqtt_broker", "localhost")
        port_m   = int(self.cfg.get("mqtt_port", 1883))
        user     = self.cfg.get("mqtt_user", "")
        password = self.cfg.get("mqtt_password", "")
        prefix   = self.cfg.get("mqtt_topic_prefix", "Home/FRN").rstrip("/")
        topic    = f"{prefix}/{room}"
        payload  = json.dumps({
            "callsign": callsign,
            "text":     text,
            "room":     room,
            "time":     datetime.fromtimestamp(ts).isoformat(),
        }, ensure_ascii=False)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, mqtt_publish, broker, port_m,
                                   topic, payload, user, password)
