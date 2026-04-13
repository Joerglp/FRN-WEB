"""
FRN Archive
-----------
Speichert TX-Sessions in SQLite, konvertiert WAV → Opus (komprimiert).
Stellt JSON-API und Audio-Serving bereit.
"""

import asyncio
import logging
import shlex
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH    = Path("/opt/FRN/archive/frn_archive.db")
AUDIO_DIR  = Path("/opt/FRN/archive/audio")
OPUS_KBPS  = 12   # kbps — gut für Sprachqualität bei 8 kHz


# ── Datenbank ─────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transmissions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   REAL    NOT NULL,           -- Unix-Zeit
                room        TEXT    NOT NULL DEFAULT '',
                callsign    TEXT    NOT NULL DEFAULT '',
                text        TEXT    NOT NULL DEFAULT '',
                audio_file  TEXT    NOT NULL DEFAULT '', -- Dateiname in AUDIO_DIR
                duration_s  REAL    NOT NULL DEFAULT 0,
                wav_source  TEXT    NOT NULL DEFAULT ''  -- Original-WAV-Pfad (Referenz)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_timestamp ON transmissions(timestamp DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_room ON transmissions(room)
        """)
    log.info("FRN Archive DB bereit: %s", DB_PATH)


# ── Opus-Konvertierung ────────────────────────────────────────────────────────

def wav_to_opus(wav_path: str, opus_path: str) -> float:
    """
    Konvertiert WAV → Opus (OGG-Container).
    Gibt die Audiodauer in Sekunden zurück.
    Blockierend — im ThreadPool aufrufen.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", wav_path,
        "-c:a", "libopus",
        "-b:a", f"{OPUS_KBPS}k",
        "-vbr", "on",
        "-compression_level", "10",
        "-ar", "8000",
        "-ac", "1",
        opus_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg Fehler: {result.stderr[-300:]}")

    # Dauer ermitteln
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", opus_path],
        capture_output=True, text=True
    )
    try:
        return float(probe.stdout.strip())
    except ValueError:
        return 0.0


# ── Eintrag hinzufügen ────────────────────────────────────────────────────────

async def add_entry(
    wav_path: str,
    room: str,
    callsign: str,
    timestamp: float,
    text: str,
) -> int | None:
    """
    Konvertiert WAV → Opus und speichert Eintrag in DB.
    Gibt die neue ID zurück, oder None bei Fehler.
    """
    dt        = datetime.fromtimestamp(timestamp)
    safe_room = room.replace("/", "_").replace(" ", "_")
    filename  = dt.strftime(f"frn-%Y%m%d-%H%M%S-{safe_room}.opus")
    opus_path = str(AUDIO_DIR / filename)

    loop = asyncio.get_running_loop()
    try:
        duration = await loop.run_in_executor(
            None, wav_to_opus, wav_path, opus_path
        )
    except Exception as e:
        log.warning("Opus-Konvertierung fehlgeschlagen (%s): %s", wav_path, e)
        filename = ""
        duration = 0.0

    try:
        with _get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO transmissions
                   (timestamp, room, callsign, text, audio_file, duration_s, wav_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, room, callsign, text, filename, duration, wav_path)
            )
            entry_id = cur.lastrowid
        log.debug("Archiv-Eintrag #%d: [%s] %s → %s", entry_id, room, callsign, filename)
        return entry_id
    except Exception as e:
        log.warning("DB-Fehler beim Archivieren: %s", e)
        return None


def add_entry_sync(
    wav_path: str,
    room: str,
    callsign: str,
    timestamp: float,
    text: str,
) -> int | None:
    """Synchrone Version für Batch-Verarbeitung."""
    dt        = datetime.fromtimestamp(timestamp)
    safe_room = room.replace("/", "_").replace(" ", "_")
    filename  = dt.strftime(f"frn-%Y%m%d-%H%M%S-{safe_room}.opus")
    opus_path = str(AUDIO_DIR / filename)

    try:
        duration = wav_to_opus(wav_path, opus_path)
    except Exception as e:
        log.warning("Opus-Konvertierung fehlgeschlagen (%s): %s", wav_path, e)
        filename = ""
        duration = 0.0

    try:
        with _get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO transmissions
                   (timestamp, room, callsign, text, audio_file, duration_s, wav_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, room, callsign, text, filename, duration, wav_path)
            )
            return cur.lastrowid
    except Exception as e:
        log.warning("DB-Fehler: %s", e)
        return None


# ── Abfrage-API ───────────────────────────────────────────────────────────────

def query_entries(
    limit: int = 50,
    offset: int = 0,
    room: str = "",
    search: str = "",
    date_from: str = "",   # "YYYY-MM-DD"
    date_to: str = "",
) -> list[dict]:
    clauses = []
    params  = []

    if room:
        clauses.append("room = ?")
        params.append(room)
    if search:
        clauses.append("(text LIKE ? OR callsign LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    if date_from:
        try:
            ts = datetime.strptime(date_from, "%Y-%m-%d").timestamp()
            clauses.append("timestamp >= ?")
            params.append(ts)
        except ValueError:
            pass
    if date_to:
        try:
            ts = datetime.strptime(date_to, "%Y-%m-%d").timestamp() + 86400
            clauses.append("timestamp < ?")
            params.append(ts)
        except ValueError:
            pass

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.extend([limit, offset])

    with _get_conn() as conn:
        rows = conn.execute(
            f"""SELECT id, timestamp, room, callsign, text, audio_file, duration_s
                FROM transmissions
                {where}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?""",
            params
        ).fetchall()

        total = conn.execute(
            f"SELECT COUNT(*) FROM transmissions {where}",
            params[:-2]
        ).scalar() if clauses else conn.execute(
            "SELECT COUNT(*) FROM transmissions"
        ).fetchone()[0]

    result = []
    for r in rows:
        result.append({
            "id":         r["id"],
            "timestamp":  r["timestamp"],
            "time":       datetime.fromtimestamp(r["timestamp"]).strftime("%Y-%m-%d %H:%M:%S"),
            "room":       r["room"],
            "callsign":   r["callsign"],
            "text":       r["text"],
            "audio_file": r["audio_file"],
            "duration_s": round(r["duration_s"], 1),
            "has_audio":  bool(r["audio_file"]),
        })
    return result, total


def get_rooms() -> list[str]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT room FROM transmissions ORDER BY room"
        ).fetchall()
    return [r[0] for r in rows if r[0]]
