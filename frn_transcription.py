"""
FRN Transcription Pipeline
--------------------------
Puffert PCM-Audio einer TX-Session, speichert WAV, transkribiert via
Remote-Whisper-API (KI-Rechner) oder lokal als Fallback.
"""

import asyncio
import difflib
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

# Generische Whisper-Halluzinationen (leere/rauschende Audiodateien) -- bisher
# nur im lokalen CPU-Fallback gefiltert, obwohl die Remote-API (Produktion)
# genau denselben Effekten unterliegt (2026-08-11 live beobachtet: "Vielen
# Dank.", "Untertitelung des ZDF, 2020" auf reinem Rauschen).
# "Bis zum naechsten Mal." (+ Varianten) 2026-08-12 ergaenzt: erschien seit
# Tagen dutzende Male, oft zu unplausiblen Uhrzeiten (00:37, 04:07, 04:11) --
# betroffene Aufnahmen jeweils nur 2-4s, Peak exakt auf unserem
# Normalisierungsziel (Rauschen wurde hochverstaerkt, wie beim Eickel-Fall).
# Nur als EXAKTER Gesamttext gefiltert (siehe _is_generic_hallucination),
# nicht als Substring -- ein echtes "...bis zum naechsten Mal" am Ende eines
# laengeren echten Gespraechs bleibt daher unangetastet.
_HALLUCINATION_PHRASES = {
    "", ".", "..", "...", "…",
    "vielen dank.", "vielen dank", "danke.", "danke", "tschüss.", "auf wiedersehen.",
    "bis zum nächsten mal.", "bis zum nächsten mal",
    "bis zum nächsten mal, tschüss.", "bis zum nächsten mal, tschüss",
    "das war's für heute. bis zum nächsten mal. tschüss.",
    "das war's für heute. bis zum nächsten mal.",
    "das war's für heute. bis zum nächsten mal. auf wiedersehen.",
    "untertitel", "untertitelung", "untertitel:",
    "untertitel des zdf", "untertitel: zdf", "untertitel zdf",
    "untertitel von zdf", "untertitel im ersten",
    "untertitel der ard", "untertitel: ard",
    "untertitel ndr", "untertitel: ndr",
    "untertitel wdr", "untertitel: wdr",
    "untertitel mdr", "untertitel: mdr",
    "untertitel br", "untertitel: br",
    "untertitelung des zdf", "untertitelung der ard",
    "copyright", "www.", "alle rechte vorbehalten.",
    "♪", "♫", "musik", "[musik]", "[applaus]", "[gelächter]",
    "[stille]", "(stille)", "[no audio]",
}
_HALLUCINATION_SUBSTRINGS = (
    "untertitel", "zdf 20", "ndr 20", "ard 20", "wdr 20", "mdr 20", "br 20",
)


def _is_generic_hallucination(text: str) -> bool:
    t = text.strip().lower()
    if t in _HALLUCINATION_PHRASES:
        return True
    return any(sub in t for sub in _HALLUCINATION_SUBSTRINGS)


# Echte, oft als kompletter Einzel-Spruch gesagte CB-Jargon-Woerter -- kommen
# im initial_prompt vor (damit Whisper sie erkennt), sind aber legitimer
# Inhalt und duerfen die Prompt-Echo-Erkennung unten NICHT ausloesen.
_CB_JARGON_ALLOWLIST = {
    "roger", "over", "qrv", "kanal", "frei", "basis", "mobile", "standort",
    "signalstärke", "rapport", "qrm", "guten", "morgen", "abend", "tschüss",
    "später", "ja", "nein", "hallo", "danke", "ciao", "tag",
}


def _is_prompt_echo(text: str, prompt: str) -> bool:
    """Erkennt kurze Ausgaben, die fast nur aus initial_prompt-eigenen Woertern
    bestehen -- typisches Whisper-Verhalten bei Rauschsperren-Rauschen: es
    halluziniert Bruchstuecke des Prompts zurueck statt echten Inhalt (live
    beobachtet 2026-08-11: initial_prompt enthaelt "Eickelborn"/"CB-Funk",
    Whisper macht aus purem Rauschen "Eickel"/"Eickel-Funk"). Nur bei sehr
    kurzen Ausgaben aktiv, damit echte kurze Durchsagen nicht faelschlich
    verworfen werden. Echte CB-Jargon-Einzelworte (Roger, Over, ...) sind
    per Allowlist ausgenommen, da die genau deswegen im Prompt stehen, weil
    sie oft als vollstaendiger, echter Spruch vorkommen.
    """
    words = [w.strip(".,!?…").lower() for w in _re.split(r"[\s-]+", text.strip())]
    words = [w for w in words if len(w) >= 3]
    if not words or len(words) > 2:
        return False
    prompt_tokens = [w.strip(".,!?…").lower() for w in _re.split(r"[\s-]+", prompt)]
    prompt_tokens = [w for w in prompt_tokens if len(w) >= 4]
    matched_distinctive = False
    for w in words:
        if w in _CB_JARGON_ALLOWLIST:
            continue
        if any(w in pt or pt in w for pt in prompt_tokens):
            matched_distinctive = True
        else:
            return False
    return matched_distinctive


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


def _get_whisper_initial_prompt() -> str:
    try:
        cfg_path = Path(__file__).parent / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return cfg.get("whisper", {}).get("initial_prompt", "").strip()
    except Exception:
        return ""


def _remove_repetitions(text: str) -> str:
    """Entfernt Whisper-typische Halluzinations-Wiederholungen wie 'ja, ja, ja'."""
    text = _re.sub(r'\b(\w+)(?:[,.]?\s+\1){2,}\b', r'\1', text, flags=_re.IGNORECASE)
    text = _re.sub(r'(.{3,}?)\s+\1(\s+\1)+', r'\1', text)
    return text.strip()


_CASCADE_SIM = 0.62   # ab dieser SequenceMatcher-Aehnlichkeit gilt ein Satz
                      # als Wiederholung eines frueheren (nicht 1.0, weil
                      # Whisper beim "Kreisen" den Satz meist leicht variiert
                      # statt ihn exakt zu wiederholen)


def _dedupe_cascade(text: str) -> str:
    """Erkennt Whisper-Wiederhol-Kaskaden auf Satzebene -- im Unterschied zu
    _remove_repetitions (nur EXAKTE Wort-/Phrasenwiederholung wie 'ja, ja,
    ja') geht es hier um ganze Saetze, die sich leicht VARIIERT wiederholen
    (beobachtet 2026-08-13 bei langen, unklaren Aufnahmen: z.B. "polierst du
    den gerade die Augen da?" taucht mit anderem Drumherum ein zweites Mal
    auf -- das Modell "kreist" um dieselbe Aussage statt neuen Inhalt zu
    erkennen). Verfahren angelehnt an RadioTranscriber
    (github.com/Nite01007/RadioTranscriber):
      - Kaskade ueber die GANZE Aussage (>50% der Saetze sind Duplikate)
        -> komplett verwerfen, vermutlich ohnehin kaum echter Inhalt.
      - Kaskade am ENDE (durchgehende Duplikat-Serie bis zum Schluss)
        -> abschneiden, den Anfang (meist der eigentliche Inhalt) behalten.
      - Kaskade am ANFANG (Modell wiederholt sich zu Beginn, findet danach
        aber zu echtem/neuem Inhalt) -> Kopf verwerfen, Rest behalten,
        statt die ganze Aussage wegzuwerfen.
      - Vereinzelte Duplikate dazwischen -> nur die einzelnen Saetze raus,
        erstes Vorkommen bleibt.
    """
    parts = [p.strip() for p in _re.split(r'(?<=[.!?])\s+', text.strip()) if p.strip()]
    if len(parts) < 3:
        return text   # zu kurz fuer eine sinnvolle Kaskaden-Analyse

    def sim(a, b):
        return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()

    def dup_flags(seq):
        flags = [False] * len(seq)
        for i in range(1, len(seq)):
            for j in range(i):
                if not flags[j] and sim(seq[i], seq[j]) > _CASCADE_SIM:
                    flags[i] = True
                    break
        return flags

    flags = dup_flags(parts)
    if sum(flags) / len(parts) > 0.5:
        return ""

    # Kaskade am Ende: durchgehende Duplikat-Serie bis zum Schluss (min. 2)
    tail = len(parts)
    while tail > 0 and flags[tail - 1]:
        tail -= 1
    if len(parts) - tail >= 2:
        parts, flags = parts[:tail], flags[:tail]

    # Kaskade am Anfang: die ersten Saetze wiederholen sich untereinander,
    # danach kommt (nicht mehr komplett doppelter) Inhalt.
    if len(parts) >= 3 and flags[1] and not all(flags):
        head_end = 1
        while head_end < len(parts) and flags[head_end]:
            head_end += 1
        if head_end < len(parts):
            parts, flags = parts[head_end:], flags[head_end:]

    # Verstreute Einzel-Duplikate: raus, erstes Vorkommen bleibt.
    return " ".join(p for p, d in zip(parts, flags) if not d).strip()


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
    # War 60s -- zu kurz fuer ungewoehnlich lange Aufnahmen (bis zu
    # MAX_DURATION=300s in frn_stream.py), die auf der GPU auch mal 190s+
    # brauchen. Client gab dann vorzeitig auf, obwohl der (single-threaded)
    # Whisper-Server im Hintergrund trotzdem weiterrechnete -- 3 sinnlose
    # Fehlversuche a 60s+30s Wartezeit spaeter blockierte das die ganze
    # Warteschlange. Jetzt knapp unter dem aeusseren 300s-Timeout in
    # process_wav (asyncio.wait_for), damit lange Clips eine faire Chance
    # bekommen statt garantiert zu scheitern.
    with urllib.request.urlopen(req, timeout=280) as resp:
        result = json.loads(resp.read())
    text = _remove_repetitions(result.get("text", "").strip())
    deduped = _dedupe_cascade(text)
    if deduped != text:
        log.info("Remote-Transkript Kaskade bereinigt: %.80s -> %.80s", text, deduped)
        text = deduped
    log.debug("Remote-Transkript (%.1fs): %s", result.get("duration_s", 0), text[:80])
    if _is_generic_hallucination(text):
        log.info("Remote-Transkript als generische Halluzination verworfen: %.80s", text)
        return ""
    prompt = _get_whisper_initial_prompt()
    if prompt and _is_prompt_echo(text, prompt):
        log.info("Remote-Transkript als Prompt-Echo verworfen (Rauschsperre?): %.80s", text)
        return ""
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

    HALLUCINATIONS = {
        "", ".", "..", "...", "…",
        "vielen dank.", "danke.", "tschüss.", "auf wiedersehen.",
        "untertitel", "untertitelung", "untertitel:",
        "untertitel des zdf", "untertitel: zdf", "untertitel zdf",
        "untertitel von zdf", "untertitel im ersten",
        "untertitel der ard", "untertitel: ard",
        "untertitel ndr", "untertitel: ndr",
        "untertitel wdr", "untertitel: wdr",
        "untertitel mdr", "untertitel: mdr",
        "untertitel br", "untertitel: br",
        "untertitelung des zdf", "untertitelung der ard",
        "copyright", "www.", "alle rechte vorbehalten.",
        "♪", "♫", "musik", "[musik]", "[applaus]", "[gelächter]",
        "[stille]", "(stille)", "[no audio]",
    }
    segments, _ = _local_model.transcribe(
        audio, language=language, beam_size=5,
        condition_on_previous_text=False,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
        no_speech_threshold=0.8,
    )
    HALLUCINATION_SUBSTRINGS = (
        "untertitel", "untertitelung", "zdf 20", "ndr 20", "ard 20",
        "wdr 20", "mdr 20", "br 20",
    )

    def _is_hallucination(text: str) -> bool:
        t = text.lower()
        if t in HALLUCINATIONS:
            return True
        return any(sub in t for sub in HALLUCINATION_SUBSTRINGS)

    parts = [s.text.strip() for s in segments
             if getattr(s, "no_speech_prob", 0.0) <= 0.8
             and not _is_hallucination(s.text.strip())]
    return _dedupe_cascade(_remove_repetitions(" ".join(parts).strip()))


def _mark_discarded(wav_path: str) -> None:
    """Benennt die zugehoerige .meta.done zu .meta.discarded um -- Marker fuer
    "vollstaendig verarbeitet, aber bewusst nicht archiviert" (reines Rauschen/
    Halluzination), damit _recover_lost_meta das beim naechsten Neustart nicht
    faelschlich als verlorenen Lauf erneut durch Whisper jagt (siehe dort)."""
    try:
        done_path = Path(wav_path).with_suffix(".meta.done")
        if done_path.exists():
            done_path.rename(Path(wav_path).with_suffix(".meta.discarded"))
    except Exception as e:
        log.debug("_mark_discarded fehlgeschlagen (%s): %s", wav_path, e)


def _is_remote_available(url: str) -> bool:
    """Prüft ob der Remote-Whisper-Server erreichbar ist (Health-Check)."""
    import urllib.request as _ur
    health = url.rsplit("/", 1)[0] + "/health"
    try:
        _ur.urlopen(_ur.Request(health), timeout=5)
        return True
    except Exception:
        return False


def _transcribe_sync(wav_path: str, model_size: str, language: str) -> str:
    """Remote-API wenn konfiguriert, sonst lokales Modell."""
    remote_url = _get_whisper_remote_url()
    if remote_url:
        return _transcribe_remote(wav_path, remote_url, language)
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
            """Alle 0.5s nach neuen .meta-Dateien aus frn_stream.py suchen
            (war 10s, dann 2s -- reine Warteschlangen-Latenz vor der
            Transkription, ohne Zusatzkosten reduzierbar da nur ein
            Datei-Listing)."""
            # Beim Start: .meta.done ohne DB-Eintrag zurücksetzen
            await self._recover_lost_meta()
            while True:
                await asyncio.sleep(0.5)
                await self._process_meta_files()

        loop = asyncio.get_event_loop()
        # Referenzen halten damit Tasks nicht garbage-collected werden
        self._task_cleanup = loop.create_task(cleanup_loop())
        self._task_meta    = loop.create_task(meta_watcher())

    async def _recover_lost_meta(self):
        """Beim Start: .meta.done Dateien ohne DB-Eintrag zurück zu .meta setzen.

        Nur echte "verloren gegangene" Laeufe (z.B. Absturz/Neustart mitten in
        der Verarbeitung) sollen hier erneut versucht werden. Aufnahmen, die
        VOLLSTAENDIG verarbeitet, aber bewusst NICHT archiviert wurden (reines
        Rauschen/Halluzination -- process_wav._mark_discarded benennt deren
        .meta.done zu .meta.discarded um), tauchen hier gar nicht erst auf
        (Glob unten matcht nur *.meta.done), werden also nicht mehr bei JEDEM
        Neustart erneut sinnlos durch Whisper gejagt (2026-08-14 gefunden:
        ~210 Dateien kamen bei zwei aufeinanderfolgenden Neustarts fast
        identisch wieder -- allesamt korrekt verworfenes Rauschen, kein
        echter Verlust)."""
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
        """Verarbeitet .meta-Dateien die frn_stream.py abgelegt hat.

        Verarbeitet immer die NEUESTE zuerst und scannt nach JEDER Datei neu
        (statt einmal eine Liste zu bilden und die stur abzuarbeiten) — sonst
        blockiert ein angestauter Rückstau alter Dateien den Live-Betrieb:
        eine gerade eingehende Aufnahme müsste sonst hinter Dutzenden alten
        Karteileichen warten, bis die durch sind. So bekommt Live immer
        Vorrang, der Rückstau wird nebenbei aufgeholt, wenn gerade nichts
        Neueres ansteht."""
        while True:
            pending = sorted(self.wav_dir.glob("*.meta"), reverse=True)
            if not pending:
                return
            meta_path = pending[0]
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

                poll_delay = time.time() - ts
                log.info("[%s] Meta-Datei gefunden: %s (%s) — Warteschlange: %.1fs",
                         room, Path(wav_path).name, callsign, poll_delay)
                dbg = getattr(self, "debug_trace", None)
                if dbg:
                    dbg(room, ts, "Aufnahme", "ok", poll_delay,
                        f"{Path(wav_path).name} ({callsign or 'kein Rufzeichen'})",
                        audio=Path(wav_path).name)
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
        # Eigene KI-Funker-Sendungen (Robert) muessen NICHT durch Whisper --
        # der Text ist bereits exakt bekannt (die generierte LLM-Antwort).
        # frn_tx_server setzt pipeline.resolve_known_text und liefert ihn ueber
        # dasselbe Zeitfenster wie die Echo-Erkennung fuers Rufzeichen. Spart
        # ~10-15s GPU-Zeit pro Bot-Antwort und vermeidet, dass Whisper Roberts
        # eigene synthetisierte Stimme fehlerhaft zurueck-transkribiert.
        dbg = getattr(self, "debug_trace", None)
        rkt = getattr(self, "resolve_known_text", None)
        text = (rkt(room, ts) if rkt else None) or ""
        if text:
            log.info("[%s] Bekannter Bot-Text übernommen (kein Whisper nötig)", room)
            if dbg:
                dbg(room, ts, "Whisper", "skip", 0.0, "bekannter Bot-Text übernommen")
        else:
            model_size = self.cfg.get("whisper_model", "medium")
            language   = self.cfg.get("whisper_language", "de")

            # Bei konfiguriertem Remote-Server: bei Fehler warten und erneut versuchen.
            # Server-weg (Health-Check) zählt NICHT als Fehlversuch. Echte Timeouts/Fehler
            # bei der Transkription werden gezählt — nach MAX_FAILS wird die Datei
            # übersprungen, damit eine einzelne kaputte/zu große Datei nicht die ganze
            # Warteschlange dauerhaft blockiert.
            MAX_FAILS = 3
            remote_url = _get_whisper_remote_url()
            fails = 0
            while True:
                if remote_url:
                    loop = asyncio.get_event_loop()
                    while not await loop.run_in_executor(None, _is_remote_available, remote_url):
                        log.warning("Remote-Whisper nicht erreichbar — Warteschlange pausiert, "
                                    "nächster Versuch in 30s …")
                        await asyncio.sleep(30)
                try:
                    _t0 = time.time()
                    text = await asyncio.wait_for(
                        transcribe_wav(wav_path, model_size, language),
                        timeout=300.0
                    )
                    _wdt = time.time() - _t0
                    log.info("[%s] Whisper: %.1fs", room, _wdt)
                    if dbg:
                        dbg(room, ts, "Whisper", "ok", _wdt, text[:200])
                    break  # Erfolg
                except asyncio.TimeoutError:
                    log.warning("[%s] Whisper-Timeout für %s", room, Path(wav_path).name)
                    if not remote_url:
                        if dbg:
                            dbg(room, ts, "Whisper", "error", time.time() - _t0,
                               "Timeout (lokal, kein Fallback)", final=True)
                        break  # lokaler Fehler → überspringen
                    fails += 1
                    if fails >= MAX_FAILS:
                        log.warning("[%s] %s nach %d Timeouts übersprungen",
                                    room, Path(wav_path).name, fails)
                        if dbg:
                            dbg(room, ts, "Whisper", "error", time.time() - _t0,
                               f"nach {fails} Timeouts übersprungen", final=True)
                        return
                    log.warning("Warte 30s und versuche erneut (Versuch %d/%d) …", fails, MAX_FAILS)
                    await asyncio.sleep(30)
                except Exception as e:
                    log.warning("[%s] Whisper-Fehler für %s: %r", room, Path(wav_path).name, e)
                    # WAV inzwischen weg (z.B. vom Cleanup gelöscht) → permanent, nicht retryen
                    if not Path(wav_path).exists():
                        log.warning("[%s] WAV %s existiert nicht mehr — übersprungen",
                                    room, Path(wav_path).name)
                        if dbg:
                            dbg(room, ts, "Whisper", "error", None,
                               "WAV existiert nicht mehr", final=True)
                        return
                    if not remote_url:
                        if dbg:
                            dbg(room, ts, "Whisper", "error", None, str(e)[:200], final=True)
                        break  # lokaler Fehler → überspringen
                    fails += 1
                    if fails >= MAX_FAILS:
                        log.warning("[%s] %s nach %d Fehlern übersprungen",
                                    room, Path(wav_path).name, fails)
                        if dbg:
                            dbg(room, ts, "Whisper", "error", None,
                               f"nach {fails} Fehlern übersprungen: {e}"[:200], final=True)
                        return
                    log.warning("Warte 30s und versuche erneut (Versuch %d/%d) …", fails, MAX_FAILS)
                    await asyncio.sleep(30)

        if not text:
            if dbg:
                dbg(room, ts, "Whisper", "skip", None,
                   "kein Text erkannt (Stille/VAD)", final=True)
            _mark_discarded(wav_path)
            return

        # Rufzeichen-Auflösung: frn_tx_server setzt pipeline.resolve_callsign,
        # damit z.B. eigene KI-Funker-Sendungen im Archiv unter dem Bot-Namen
        # geführt werden statt ohne Rufzeichen.
        rc = getattr(self, "resolve_callsign", None)
        if rc:
            try:
                callsign = await rc(room, callsign, ts, text, wav_path) or callsign
            except Exception as e:
                log.debug("resolve_callsign-Hook: %s", e)

        self._log(ts, room, callsign, text)

        # Hook für Auto-Antwort: frn_tx_server setzt pipeline.on_transcript.
        cb = getattr(self, "on_transcript", None)
        if cb:
            try:
                asyncio.create_task(cb(room, callsign, text, ts))
            except Exception as e:
                log.debug("on_transcript-Hook: %s", e)

        try:
            from frn_archive import add_entry
            await add_entry(wav_path, room, callsign, ts, text)
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

    def _cleanup_old_wavs(self):
        max_age = int(self.cfg.get("max_age_days", 2))
        cutoff  = datetime.now() - timedelta(days=max_age)
        removed = 0
        for p in self.wav_dir.glob("*.wav"):
            try:
                if datetime.fromtimestamp(p.stat().st_mtime) >= cutoff:
                    continue
                # Nicht löschen wenn noch eine .meta-Datei (ausstehend) existiert
                if p.with_suffix(".meta").exists():
                    continue
                p.unlink()
                # .meta.done/.meta.discarded Sidecar ebenfalls entfernen
                for suffix in (".meta.done", ".meta.discarded"):
                    sidecar = p.with_suffix(suffix)
                    if sidecar.exists():
                        sidecar.unlink()
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
