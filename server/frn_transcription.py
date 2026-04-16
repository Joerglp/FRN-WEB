"""
FRN Transcription Pipeline
--------------------------
Puffert PCM-Audio einer TX-Session, speichert WAV, transkribiert via
Remote-Whisper-API (KI-Rechner) oder lokal als Fallback.
"""

import asyncio
import json
import logging
import re as _re
import time
import wave
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# ── Remote Whisper API ────────────────────────────────────────────────────────
# URL wird zur Laufzeit aus config.json gelesen (whisper.remote_url).
# Leer lassen → lokales medium-Modell auf dem Pi als Fallback.

_whisper_lock = asyncio.Lock()


def _get_whisper_remote_url() -> str:
    """Liest remote_url: zuerst Umgebungsvariable, dann config.json."""
    import os
    env_url = os.environ.get("WHISPER_REMOTE_URL", "").strip()
    if env_url:
        return env_url
    try:
        cfg_path = Path(__file__).parent / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return cfg.get("whisper", {}).get("remote_url", "").strip()
    except Exception:
        return ""


def _remove_repetitions(text: str) -> str:
    """Entfernt Whisper-typische Halluzinations-Wiederholungen wie 'ja, ja, ja'."""
    text = _re.sub(r'\b(\w+)(?:[,.]?\s+\1){2,}\b', r'\1', text, flags=_re.IGNORECASE)
    text = _re.sub(r'(.{3,}?)\s+\1(\s+\1)+', r'\1', text)
    return text.strip()


def _transcribe_remote(wav_path: str, url: str, language: str) -> str:
    """Schickt WAV per multipart/form-data an den Remote-Whisper-Server."""
    import urllib.request
    boundary = "----FRNWhisperBoundary"
    with open(wav_path, "rb") as f:
        wav_data = f.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode() + wav_data + (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="language"\r\n\r\n'
        f"{language}\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
    text = _remove_repetitions(result.get("text", "").strip())
    log.debug("Remote-Transkript (%.1fs): %s", result.get("duration_s", 0), text[:80])
    return text


_local_model = None

def _transcribe_local(wav_path: str, model_size: str, language: str) -> str:
    """Lokales faster-whisper (CPU, medium) als Fallback."""
    try:
        from scipy.signal import resample_poly
        from faster_whisper import WhisperModel
    except ImportError:
        log.error("faster-whisper nicht installiert und kein remote_url konfiguriert — "
                  "bitte WITH_WHISPER=true beim Docker-Build oder remote_url in config.json setzen")
        return ""

    global _local_model
    if _local_model is None:
        log.info("Lade lokales Fallback-Modell '%s' ...", model_size)
        _local_model = WhisperModel(model_size, device="cpu", compute_type="int8")

    with wave.open(wav_path, "rb") as wf:
        src_rate = wf.getframerate()
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    audio = pcm.astype(np.float32) / 32768.0
    if src_rate != 16000:
        audio = resample_poly(audio, 16000, src_rate)
    rms = np.sqrt(np.mean(audio ** 2))
    if rms > 0:
        audio = audio * min(0.1 / rms, 10.0)
        peak = np.max(np.abs(audio))
        if peak > 1.0:
            audio = audio / peak

    HALLUCINATIONS = {"", ".", "...", "vielen dank.", "danke.", "tschüss.",
                      "untertitel", "♪", "musik", "[musik]", "[applaus]"}
    segments, _ = _local_model.transcribe(
        audio, language=language, beam_size=5,
        condition_on_previous_text=False,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
        no_speech_threshold=0.8,
    )
    parts = [s.text.strip() for s in segments
             if getattr(s, "no_speech_prob", 0.0) <= 0.8
             and s.text.strip().lower() not in HALLUCINATIONS]
    return _remove_repetitions(" ".join(parts).strip())


def _transcribe_sync(wav_path: str, model_size: str, language: str) -> str:
    """Remote-API wenn konfiguriert, sonst lokales Modell."""
    remote_url = _get_whisper_remote_url()
    if remote_url:
        try:
            return _transcribe_remote(wav_path, remote_url, language)
        except Exception as e:
            log.warning("Remote-Whisper nicht erreichbar (%s) — lokaler Fallback", e)
    return _transcribe_local(wav_path, model_size, language)


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
        """Startet stündlichen Cleanup + Meta-Datei-Watcher."""
        async def cleanup_loop():
            while True:
                await asyncio.sleep(3600)
                self._cleanup_old_wavs()

        async def meta_watcher():
            """Alle 10s nach neuen .meta-Dateien aus frn_stream.py suchen."""
            # Beim Start: .meta.done ohne DB-Eintrag zurücksetzen
            await self._recover_lost_meta()
            while True:
                await asyncio.sleep(10)
                await self._process_meta_files()

        loop = asyncio.get_event_loop()
        # Referenzen halten damit Tasks nicht garbage-collected werden
        self._task_cleanup = loop.create_task(cleanup_loop())
        self._task_meta    = loop.create_task(meta_watcher())

    async def _recover_lost_meta(self):
        """Beim Start: .meta.done Dateien ohne DB-Eintrag zurück zu .meta setzen."""
        try:
            from frn_archive import _get_conn
            with _get_conn() as conn:
                sources = {r[0] for r in conn.execute(
                    "SELECT wav_source FROM transmissions WHERE wav_source != ''"
                ).fetchall()}
        except Exception as e:
            log.warning("_recover_lost_meta: DB-Fehler: %s", e)
            return

        recovered = 0
        for done_path in self.wav_dir.glob("*.meta.done"):
            try:
                meta = json.loads(done_path.read_text(encoding="utf-8"))
                wav = meta.get("wav", "")
                if wav and wav not in sources and Path(wav).exists():
                    meta_path = done_path.with_suffix("")  # .meta.done → .meta
                    done_path.rename(meta_path)
                    recovered += 1
            except Exception:
                pass
        if recovered:
            log.info("_recover_lost_meta: %d Dateien zurückgesetzt", recovered)

    async def _process_meta_files(self):
        """Verarbeitet .meta-Dateien die frn_stream.py abgelegt hat."""
        for meta_path in sorted(self.wav_dir.glob("*.meta")):
            try:
                import json as _json
                meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                wav_path = meta.get("wav", "")
                room     = meta.get("room", "")
                callsign = meta.get("callsign", "")
                ts       = float(meta.get("timestamp", 0))

                if not wav_path or not Path(wav_path).exists():
                    meta_path.rename(meta_path.with_suffix(".meta.done"))
                    continue

                # Als erledigt markieren BEVOR Verarbeitung (verhindert Doppelverarbeitung)
                meta_path.rename(meta_path.with_suffix(".meta.done"))

                log.info("[%s] Meta-Datei gefunden: %s (%s)", room, Path(wav_path).name, callsign)
                # Sequenziell abarbeiten — verhindert Timeout wenn viele Dateien warten
                await self.process_wav(wav_path, room, callsign, ts)

            except Exception as e:
                log.warning("Meta-Datei Fehler (%s): %s", meta_path.name, e)
                try:
                    meta_path.rename(meta_path.with_suffix(".meta.err"))
                except Exception:
                    pass

    async def process_wav(self, wav_path: str, room: str, callsign: str, ts: float):
        """Transkribiert eine fertige WAV-Datei (von frn_stream.py aufgezeichnet)."""
        model_size = self.cfg.get("whisper_model", "medium")
        language   = self.cfg.get("whisper_language", "de")
        text = ""
        try:
            text = await asyncio.wait_for(
                transcribe_wav(wav_path, model_size, language),
                timeout=300.0
            )
        except asyncio.TimeoutError:
            log.warning("[%s] Whisper-Timeout für %s", room, Path(wav_path).name)
        except Exception as e:
            log.warning("[%s] Whisper-Fehler für %s: %r", room, Path(wav_path).name, e)

        if not text:
            return

        self._log(ts, room, callsign, text)

        try:
            from frn_archive import add_entry
            await add_entry(wav_path, room, callsign, ts, text)
        except Exception as e:
            log.warning("[%s] Archiv-Fehler: %s", room, e)

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
                timeout=300.0
            )
        except asyncio.TimeoutError:
            log.warning("[%s] Whisper-Timeout (>300s) — übersprungen", room)
        except Exception as e:
            log.warning("[%s] Whisper-Fehler: %r", room, e)

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
