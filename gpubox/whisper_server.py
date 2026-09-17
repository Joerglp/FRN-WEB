#!/usr/bin/env python3
"""
Whisper HTTP API Server
Empfängt WAV-Dateien per POST, gibt Transkript als JSON zurück.
Läuft auf Port 9001, nutzt CUDA wenn verfügbar.
"""
from flask import Flask, request, jsonify
from faster_whisper import WhisperModel
import tempfile, os, logging, time, json, re
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# initial_prompt MUSS wie ein natuerlicher Transkript-Ausschnitt klingen, NICHT
# wie eine Stichwortliste/Anweisung ("Gaengige Ausdruecke: ...", "Kein
# Rufzeichen.") -- genau solche Meta-Formulierungen hat Whisper bei leisem/
# unklarem CB-Funk-Audio woertlich als "Transkript" zurueckgegeben (siehe
# Log: "Gaengige Ausdruecke." kam 2026-07-26/27 mehrfach als komplette
# Modellausgabe). Deshalb als fliessender Beispieldialog formuliert.
_DEFAULT_PROMPT = (
    "Kanal 74, CB-Funk zwischen Eickelborn, Lippstadt, Hamm und Soest. "
    "Ist einer da? Ist hier jemand QRV? Hoert mich jemand? Robert, bist du QRV? "
    "Roger, over, so kommt es rueber, Kanal frei, bin auf der Basis, "
    "bin mobile unterwegs, bis spaeter, 77, tschuess."
)
CONFIG_PATH = os.environ.get("CONFIG_PATH",
                              os.path.expanduser("~/config/config.json"))


def _load_initial_prompt() -> str:
    try:
        cfg = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
        p = (cfg.get("whisper", {}) or {}).get("initial_prompt")
        if p and p.strip():
            return p.strip()
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Config %s nicht geladen: %s", CONFIG_PATH, e)
    return _DEFAULT_PROMPT


INITIAL_PROMPT = _load_initial_prompt()
log.info("initial_prompt (%s): %.100s", CONFIG_PATH, INITIAL_PROMPT)


def _normalize(s: str) -> str:
    """Klein, ohne Satzzeichen, einfache Leerzeichen -- fuer den Vergleich
    Transkript-Segment vs. Prompt (robust gegen Punkt/Komma-Unterschiede)."""
    s = s.lower()
    s = re.sub(r"[^\wäöüß\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


_PROMPT_NORM = _normalize(INITIAL_PROMPT)
_ECHO_MIN_LEN = 15  # kurze Einzelwoerter wie "Roger"/"QRV" ueberlappen
                    # zufaellig mit dem Vokabular -- erst ab einer laengeren
                    # woertlichen Mehrwort-Uebereinstimmung ist es ein
                    # Prompt-Echo und keine echte, zufaellig aehnliche Aussage.


def _prompt_echo_overlap(text_norm: str, prompt_norm: str,
                         n: int = 6, min_len: int = 22) -> bool:
    """Erkennt TEIL-Echos, bei denen nur ein Teil des Segments woertlich aus
    dem Prompt stammt (z.B. "Rapport fuenf-neun, hat er." -- nur der Anfang
    ist Prompt-Text, der Rest nicht, daher kein Treffer beim reinen Ganzes-
    Segment-Vergleich oben). Ab 6 zusammenhaengenden Woertern (min. 22 Zeichen)
    ist eine Uebereinstimmung mit dem Prompt so unwahrscheinlich zufaellig,
    dass es sich um ein Echo handelt -- kuerzere Ueberlappungen (z.B. "bin auf
    der Basis") kommen in echter Sprache natuerlich vor und wuerden sonst
    faelschlich verworfen."""
    words = text_norm.split()
    if len(words) < n:
        return False
    for i in range(len(words) - n + 1):
        ngram = " ".join(words[i:i + n])
        if len(ngram) >= min_len and ngram in prompt_norm:
            return True
    return False

# Substring- statt Exact-Match: faengt auch halb-halluzinierte Varianten wie
# "ARD Text im Auftrag von Funk" oder "Untertitelung des ZDF, 2020" ab, nicht
# nur exakt "untertitel".
HALLUCINATION_PATTERNS = re.compile(
    r"untertitel|amara\.org|im auftrag|copyright|abonnier|"
    r"gefördert durch|zdf|ard text|swr|"
    r"^gängige ausdrücke|^kein rufzeichen\.?$",
    re.IGNORECASE)

# Modell per Umgebungsvariable waehlbar (2026-09-17): die systemd-Unit setzte
# schon immer WHISPER_MODEL, der Code hat es aber ignoriert und stur
# large-v3-turbo geladen -- /health meldete deshalb auch dann "turbo", wenn
# etwas anderes eingestellt war. Jetzt gilt die Variable wirklich, damit sich
# large-v3 gegen large-v3-turbo vergleichen laesst, ohne den Code anzufassen.
MODELL  = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
GERAET  = os.environ.get("WHISPER_DEVICE", "cuda")
COMPUTE = os.environ.get("WHISPER_COMPUTE", "float16")
PORT    = int(os.environ.get("WHISPER_PORT", "9001"))
log.info("Lade Whisper %s (%s, %s)...", MODELL, GERAET, COMPUTE)
model = WhisperModel(MODELL, device=GERAET, compute_type=COMPUTE)
log.info("Modell geladen.")

app = Flask(__name__)

@app.route("/transcribe", methods=["POST"])
def transcribe():
    if "file" not in request.files:
        return jsonify({"error": "Keine Datei"}), 400

    f = request.files["file"]
    lang = request.form.get("language", "de")
    # Der Pi schickt seit 2026-09-17 initial_prompt und hotwords mit. Der
    # Prompt hier bleibt die Vorgabe (er ist als fliessender Beispieldialog
    # formuliert, siehe Kommentar oben -- eine Stichwortliste wuerde Whisper
    # woertlich ausgeben); ein mitgeschickter Prompt hat Vorrang. hotwords
    # sind dagegen genau fuer Namen gedacht und landen NICHT im Prompt-Text,
    # koennen also nicht als Echo zurueckkommen.
    prompt = (request.form.get("initial_prompt") or "").strip() or INITIAL_PROMPT
    prompt_norm = _PROMPT_NORM if prompt == INITIAL_PROMPT else _normalize(prompt)
    hotwords = (request.form.get("hotwords") or "").strip()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    try:
        t0 = time.time()
        segments, info = model.transcribe(
            tmp_path,
            language=lang,
            # Etwas auf Tempo statt maximaler Gruendlichkeit getrimmt (User-
            # Wunsch 2026-07-29): beam_size/best_of kleiner = weniger Suchpfade
            # pro Segment; temperature-Leiter von 6 auf 3 Stufen -- die
            # Fallback-Stufen greifen nur bei unsicherem Audio, kosten dort
            # aber bisher bis zu 6x die Grunddekodierzeit. War Hauptursache
            # der 100s+-Ausreisser bei laengeren/unklaren Aufnahmen.
            # beam_size 3->1 (2026-08-07): seit VAD aus (siehe Kommentar oben)
            # verarbeitet Whisper wieder die komplette Aufnahme statt nur die
            # per VAD erkannten Sprachteile -- im Schnitt ~6x langsamer als
            # vorher. beam_size=1 bringt bei kurzen Aufnahmen ~2x, bei langen
            # (>60s) noch ~12-18% Tempo, bei vergleichbarer Qualitaet
            # (getestet gegen reale Aufnahmen vom 07.08., keine spuerbare
            # Verschlechterung).
            # Modell 2026-08-11 auf large-v3-turbo umgestellt (2-3x schneller,
            # weniger VRAM, vergleichbare Qualitaet zu large-v3). Dadurch
            # beam_size 1->5 zurueckgestellt: auf der A2000 kein spuerbarer
            # Zeitunterschied (getestet, User bestaetigt keine Einschraenkung),
            # Beam-Search statt Greedy sollte bei schwierigem Audio tendenziell
            # praeziser sein.
            beam_size=5,
            best_of=5,
            initial_prompt=prompt,
            **({"hotwords": hotwords} if hotwords else {}),
            condition_on_previous_text=False,
            # VAD AUS (2026-08-05): Silero-VAD (separates neuronales Netz)
            # stufte bei laengeren GSM-komprimierten CB-Funk-Aufnahmen einen
            # Teil faelschlich als "keine Sprache" ein und brach die
            # Transkription dort komplett ab -- bis zu ueber die Haelfte des
            # Inhalts ging verloren, obwohl durchgehend Sprache da war
            # (verifiziert: Restaufnahme separat transkribiert kam vollstaendig
            # und korrekt zurueck). Vermutlich Mismatch zwischen VADs
            # Trainingsdaten (sauberes Breitband-Audio) und bandbegrenztem/
            # komprimiertem Funk-Audio. Kein Versions-Bug (bereits faster-whisper
            # 1.2.1, aktuellste verfuegbare) -- laxere VAD-Parameter (Stille-
            # Schwelle hoch) haben das NICHT behoben, nur komplettes Abschalten.
            vad_filter=False,
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.8,
            temperature=[0.0, 0.4, 0.8],
        )
        HALLUCINATIONS = {"", ".", "...", "vielen dank.", "danke.",
                          "tschüss.", "untertitel", "♪", "musik",
                          "[musik]", "[applaus]"}
        # compression_ratio_threshold oben wird von faster-whisper NUR als
        # Retry-Ausloeser genutzt (naechste Temperatur-Stufe versuchen) --
        # schlaegt die Aufnahme auch bei der hoechsten Stufe (0.8) noch fehl,
        # wird das Ergebnis TROTZDEM zurueckgegeben, nicht verworfen
        # (Whisper-Standardverhalten, kein Bug). Live beobachtet 2026-08-18
        # 06:37:53: 4.3s-Aufnahme kam als "leit" >100x wiederholt zurueck --
        # ein Text mit derart extremer Wiederholung hat einen sehr hohen
        # compression_ratio (repetitiver Text komprimiert stark), haette den
        # Schwellwert 2.4 also klar gerissen. Deshalb hier zusaetzlich
        # explizit NACH dem Transkribieren nochmal dagegen pruefen und
        # ablehnen statt nur zu loggen -- schliesst die Luecke zwischen
        # "Bibliothek erkennt schlechte Qualitaet" und "gibt sie trotzdem
        # aus". BEWUSST kein avg_logprob-Check hier: der haengt auch von
        # reiner Audio-Schwierigkeit ab (schwaches CB-Signal, Rauschen) --
        # das ist bei uns Alltag, kein Qualitaetsproblem des Transkripts,
        # und wuerde eher echten (wenn auch unsicheren) Inhalt verwerfen als
        # Halluzinationen. compression_ratio misst dagegen die Wiederholung
        # im TEXT selbst -- viel spezifischeres Signal fuer genau dieses
        # Fehlerbild, ohne das Risiko.
        parts = []
        behalten = []   # die Segmente hinter parts -- fuer avg_logprob unten
        # Aussortiertes nicht mehr spurlos wegwerfen (2026-09-17, User-Wunsch):
        # frueher blieb bei durchweg verworfenen Segmenten nur ein leeres
        # Transkript uebrig, und der Pi hat die komplette Aufnahme daraufhin
        # aus dem Archiv geworfen -- die Durchsage war damit weg, obwohl das
        # Audio in Ordnung war. Jetzt geht der verworfene Text mit zurueck,
        # der Pi archiviert ihn als unbrauchbar markiert. "stille" zaehlt
        # nicht dazu: da war wirklich nichts, das darf weiter entfallen.
        verworfen = []
        for seg in segments:
            t = seg.text.strip()
            if getattr(seg, "no_speech_prob", 0.0) > 0.8:
                verworfen.append(("stille", t))
                continue
            if getattr(seg, "compression_ratio", 0.0) > 2.4:
                log.info("Segment wegen compression_ratio=%.2f verworfen: %.60s",
                         seg.compression_ratio, t[:60])
                verworfen.append(("wiederholung", t))
                continue
            if t.lower() in HALLUCINATIONS or HALLUCINATION_PATTERNS.search(t):
                verworfen.append(("halluzination", t))
                continue
            t_norm = _normalize(t)
            if (len(t_norm) >= _ECHO_MIN_LEN and t_norm in prompt_norm) or \
               _prompt_echo_overlap(t_norm, prompt_norm):
                log.info("Prompt-Echo verworfen: %.80s", t)
                verworfen.append(("prompt_echo", t))
                continue
            parts.append(t)
            behalten.append(seg)

        text = " ".join(parts).strip()
        elapsed = time.time() - t0
        log.info("Transkript (%.1fs): %s", elapsed, text[:80])
        # avg_logprob/no_speech_prob mitgeben (2026-09-17): Whispers eigenes
        # Mass dafuer, wie sicher es war. Der Pi behilft sich sonst mit einem
        # zweiten Durchgang pro Aufnahme (leicht veraendertes Tempo), was
        # jedesmal einen weiteren GPU-Lauf kostet. Hier nur MITGELIEFERT, nicht
        # zum Verwerfen benutzt -- siehe Begruendung oben, warum ein
        # avg_logprob-Filter an dieser Stelle echten Inhalt wegwerfen wuerde.
        # Ohne uebrig gebliebene Segmente gibt es nichts zu bewerten -- dann
        # null statt 0.0 melden. 0.0 waere der bestmoegliche avg_logprob und
        # wuerde ein leeres Ergebnis als sicherstes von allen ausweisen.
        if behalten:
            gewicht = sum(max(0.0, x.end - x.start) for x in behalten) or 1.0
            logp = round(sum(x.avg_logprob * max(0.0, x.end - x.start)
                             for x in behalten) / gewicht, 3)
            nosp = round(max(x.no_speech_prob for x in behalten), 3)
        else:
            logp = nosp = None
        antwort = {"text": text, "language": info.language,
                   "duration_s": elapsed, "model": MODELL,
                   "avg_logprob": logp, "no_speech_prob": nosp}
        if not text:
            # Nur echte Aussortierer melden -- reine Stille bleibt Stille.
            rest = [(g, t) for g, t in verworfen if g != "stille" and t]
            if rest:
                antwort["verworfen_text"] = " ".join(t for _, t in rest)[:500]
                antwort["verworfen_grund"] = rest[0][0]
                log.info("Nur verworfene Segmente (%s): %.80s",
                         rest[0][0], antwort["verworfen_text"])
        return jsonify(antwort)
    finally:
        os.unlink(tmp_path)

@app.route("/health")
def health():
    return jsonify({"status": "ok", "model": MODELL, "device": GERAET})

if __name__ == "__main__":
    log.info("Whisper API Server startet auf Port %d...", PORT)
    app.run(host="0.0.0.0", port=PORT, threaded=False)
