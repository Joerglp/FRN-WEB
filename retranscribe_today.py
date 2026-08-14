#!/usr/bin/env python3
"""
Löscht Archiv-Einträge und transkribiert die zugehoerigen WAV-Dateien neu
— mit den aktuellen Whisper-Einstellungen/Filtern.

Usage: python3 retranscribe_today.py [YYYY-MM-DD] [HH:MM:SS]
       Zweites Argument optional: nur Aufnahmen AB dieser Uhrzeit (sonst
       der ganze Tag).
"""
import re, sys, os, logging, sqlite3, wave, time
from datetime import datetime, date
from pathlib import Path

# Pfad zum stream-Verzeichnis
sys.path.insert(0, "/opt/FRN/stream")
os.chdir("/opt/FRN/stream")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

from frn_transcription import _transcribe_sync
from frn_archive import add_entry_sync, DB_PATH, AUDIO_DIR, _get_conn

TARGET_DATE = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
TIME_FROM   = sys.argv[2] if len(sys.argv) > 2 else None   # z.B. "07:30:00"
log.info("Zieldatum: %s%s", TARGET_DATE, f" ab {TIME_FROM}" if TIME_FROM else "")

WAV_DIR = Path("/opt/FRN/recordings")
# Dateiname-Muster: frn-YYYYMMDD-HHMMSS-<Raumname>.wav -- der Raumname-Suffix
# hat frueher gefehlt, das urspruengliche strptime(stem, "frn-%Y%m%d-%H%M%S")
# schlug deshalb bei JEDER aktuellen Datei fehl ("unconverted data remains")
# und wurde still uebersprungen (2026-08-14 gefunden). Jetzt per Regex nur
# den Datum/Zeit-Teil am Anfang ziehen, Rest (Raumname) ignorieren.
FNAME_RE = re.compile(r"^frn-(\d{8})-(\d{6})")

cutoff_dt = None
if TIME_FROM:
    cutoff_dt = datetime.strptime(f"{TARGET_DATE} {TIME_FROM}", "%Y-%m-%d %H:%M:%S")

# ── 1. WAV-Dateien laden + nach Zeit filtern ──────────────────────────────────
prefix = "frn-" + TARGET_DATE.replace("-", "")
all_wavs = sorted(WAV_DIR.glob(f"{prefix}-*.wav"))
wavs = []
for wp in all_wavs:
    m = FNAME_RE.match(wp.stem)
    if not m:
        continue
    ts_dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    if cutoff_dt and ts_dt < cutoff_dt:
        continue
    wavs.append((wp, ts_dt))

log.info("Gefundene WAV-Dateien gesamt: %d, im Zielfenster: %d", len(all_wavs), len(wavs))

if not wavs:
    log.warning("Keine passenden WAV-Dateien gefunden.")
    sys.exit(0)

# ── 2. Zugehoerige Archiv-Eintraege + Opus-Dateien loeschen (nur im Zeitfenster) ──
cutoff_ts = wavs[0][1].timestamp()
with _get_conn() as conn:
    rows = conn.execute(
        "SELECT id, audio_file FROM transmissions "
        "WHERE date(datetime(timestamp,'unixepoch','localtime')) = ? "
        "AND timestamp >= ?",
        (TARGET_DATE, cutoff_ts)
    ).fetchall()

log.info("Lösche %d vorhandene Einträge …", len(rows))
deleted_opus = 0
for row in rows:
    opus = AUDIO_DIR / row["audio_file"] if row["audio_file"] else None
    if opus and opus.exists():
        opus.unlink()
        deleted_opus += 1

with _get_conn() as conn:
    conn.execute(
        "DELETE FROM transmissions "
        "WHERE date(datetime(timestamp,'unixepoch','localtime')) = ? "
        "AND timestamp >= ?",
        (TARGET_DATE, cutoff_ts)
    )

log.info("  → %d DB-Einträge + %d Opus-Dateien gelöscht", len(rows), deleted_opus)
wavs = [wp for wp, _ in wavs]

# ── 3. Transkribieren ─────────────────────────────────────────────────────────
MODEL   = "medium"
LANG    = "de"
WAV_DIR_PATH = Path("/opt/FRN/recordings")

ok = err = skipped = 0
t0 = time.time()

for i, wav_path in enumerate(wavs, 1):
    # Timestamp aus Dateiname: frn-YYYYMMDD-HHMMSS-<Raum>.wav
    m = FNAME_RE.match(wav_path.stem)
    if not m:
        log.warning("Dateiname unbekannt: %s — übersprungen", wav_path.name)
        skipped += 1
        continue
    ts = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").timestamp()

    # Raum aus Meta-Done-Datei oder unbekannt
    room     = "?"
    callsign = "-"
    meta_done = wav_path.with_suffix(".meta.done")
    meta_raw  = wav_path.with_suffix(".meta")
    for mp in (meta_done, meta_raw):
        if mp.exists():
            import json
            try:
                m = json.loads(mp.read_text())
                room     = m.get("room", room)
                callsign = m.get("callsign", callsign)
            except Exception:
                pass
            break

    # Mindestlänge prüfen (< 1 s überspringen)
    try:
        with wave.open(str(wav_path), "rb") as wf:
            dur = wf.getnframes() / wf.getframerate()
        if dur < 1.0:
            skipped += 1
            continue
    except Exception:
        skipped += 1
        continue

    elapsed = time.time() - t0
    eta_s   = (elapsed / i) * (len(wavs) - i) if i > 1 else 0
    log.info("[%d/%d] %s  [%s] %s  ETA: %dm%02ds",
             i, len(wavs), wav_path.name, room, callsign,
             int(eta_s // 60), int(eta_s % 60))

    try:
        text = _transcribe_sync(str(wav_path), MODEL, LANG)
    except Exception as e:
        log.warning("  Whisper-Fehler: %r", e)
        err += 1
        continue

    if not text:
        skipped += 1
        continue

    log.info("  → %s", text[:100])
    add_entry_sync(str(wav_path), room, callsign, ts, text)
    ok += 1

total = time.time() - t0
log.info("Fertig: %d transkribiert, %d Fehler, %d übersprungen — %.0fs gesamt",
         ok, err, skipped, total)
