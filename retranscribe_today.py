#!/usr/bin/env python3
"""
Löscht alle heutigen Archiv-Einträge und transkribiert alle WAV-Dateien
von heute neu — mit den verbesserten Whisper-Einstellungen.

Usage: python3 retranscribe_today.py [YYYY-MM-DD]
"""
import sys, os, logging, sqlite3, wave, time
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
log.info("Zieldatum: %s", TARGET_DATE)

WAV_DIR = Path("/opt/FRN/recordings")

# ── 1. Heutige Opus-Dateien und DB-Einträge löschen ──────────────────────────
prefix = "frn-" + TARGET_DATE.replace("-", "")

with _get_conn() as conn:
    rows = conn.execute(
        "SELECT id, audio_file FROM transmissions "
        "WHERE date(datetime(timestamp,'unixepoch','localtime')) = ?",
        (TARGET_DATE,)
    ).fetchall()

log.info("Lösche %d vorhandene Einträge vom %s …", len(rows), TARGET_DATE)
deleted_opus = 0
for row in rows:
    opus = AUDIO_DIR / row["audio_file"] if row["audio_file"] else None
    if opus and opus.exists():
        opus.unlink()
        deleted_opus += 1

with _get_conn() as conn:
    conn.execute(
        "DELETE FROM transmissions "
        "WHERE date(datetime(timestamp,'unixepoch','localtime')) = ?",
        (TARGET_DATE,)
    )

log.info("  → %d DB-Einträge + %d Opus-Dateien gelöscht", len(rows), deleted_opus)

# ── 2. WAV-Dateien von heute laden ───────────────────────────────────────────
wavs = sorted(WAV_DIR.glob(f"{prefix}-*.wav"))
log.info("Gefundene WAV-Dateien: %d", len(wavs))

if not wavs:
    log.warning("Keine WAV-Dateien für %s gefunden.", TARGET_DATE)
    sys.exit(0)

# ── 3. Transkribieren ─────────────────────────────────────────────────────────
MODEL   = "medium"
LANG    = "de"
WAV_DIR_PATH = Path("/opt/FRN/recordings")

ok = err = skipped = 0
t0 = time.time()

for i, wav_path in enumerate(wavs, 1):
    # Timestamp aus Dateiname: frn-YYYYMMDD-HHMMSS.wav
    stem = wav_path.stem  # frn-20260415-073012
    try:
        ts = datetime.strptime(stem, "frn-%Y%m%d-%H%M%S").timestamp()
    except ValueError:
        log.warning("Dateiname unbekannt: %s — übersprungen", wav_path.name)
        skipped += 1
        continue

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
