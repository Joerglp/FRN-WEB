"""
FRN Archive
-----------
Speichert TX-Sessions in SQLite, konvertiert WAV → Opus (komprimiert).
Stellt JSON-API und Audio-Serving bereit.
"""

import asyncio
import logging
import os
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
    conn.execute("PRAGMA foreign_keys=ON")   # sonst kaskadiert ON DELETE CASCADE nicht (siehe delete_entry)
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL    NOT NULL,
                room      TEXT    NOT NULL DEFAULT '',
                callsign  TEXT    NOT NULL DEFAULT '',
                text      TEXT    NOT NULL DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_ts   ON chat_messages(timestamp DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_room ON chat_messages(room)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id   INTEGER NOT NULL REFERENCES transmissions(id) ON DELETE CASCADE,
                timestamp  REAL    NOT NULL,
                text       TEXT    NOT NULL DEFAULT ''
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_entry ON comments(entry_id)")
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


def _unique_opus_path(dt: datetime, safe_room: str) -> tuple[str, str]:
    """Liefert (Dateiname, voller Pfad) fuer einen neuen Archiv-Eintrag,
    kollisionssicher. Reiner Zeitstempel (Sekundenaufloesung) reicht NICHT
    -- beim Zerlegen einer Aufnahme (siehe handle_admin_archive_split)
    behaelt der erste Teil exakt denselben Start-Zeitstempel wie das
    Original, was denselben Dateinamen ergab: die spaetere ffmpeg -y
    Konvertierung ueberschrieb dabei unbemerkt die noch existierende
    Original-Datei, und deren Loeschung (delete_entry) riss dann auch den
    neuen Teil mit -- Datenverlust, live beobachtet am 2026-08-16.

    Reserviert den Namen ATOMAR (os.O_CREAT|os.O_EXCL) statt nur per
    exists()-Check zu pruefen -- ein reiner Check waere ein TOCTOU-Fenster:
    zwei parallele add_entry()-Aufrufe koennten beide denselben freien Namen
    sehen, bevor einer von ihnen tatsaechlich schreibt (2026-08-18 Review-
    Fund, dieselbe Bug-Klasse nur enger). Die anschliessende WAV->Opus-
    Konvertierung (ffmpeg -y) ueberschreibt die hier angelegte leere
    Platzhalterdatei normal."""
    # AUDIO_DIR sicherstellen -- init_db() tut das normalerweise beim
    # Server-Start, aber retranscribe_today.py (Batch-Skript) ruft init_db()
    # nie auf; ohne das wuerde os.open() unten mit FileNotFoundError statt
    # der erwarteten FileExistsError-Kollisionsbehandlung scheitern
    # (2026-08-19 Review-Fund). Billig/idempotent, kein Problem im Normalfall.
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    # safe_room wird ANGEHAENGT statt in den strftime()-Format-String
    # eingesetzt -- ein Raumname mit einem woertlichen '%' gefolgt von
    # einem Direktiven-Buchstaben (z.B. "%d") wuerde sonst von strftime
    # als Formatanweisung interpretiert statt als Text uebernommen, was
    # den Dateinamen still verfaelscht (2026-08-19 Review-Fund).
    base = dt.strftime("frn-%Y%m%d-%H%M%S") + f"-{safe_room}"
    n = 1
    filename = f"{base}.opus"
    while True:
        path = AUDIO_DIR / filename
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return filename, str(path)
        except FileExistsError:
            n += 1
            filename = f"{base}-{n}.opus"


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
    loop = asyncio.get_running_loop()
    # _unique_opus_path() macht blockierende os.open()/os.close()-Syscalls
    # (potenziell mehrfach bei Kollision) -- das gehoert wie wav_to_opus in
    # den Executor, sonst haengt bei einer langsamen/vollen Platte der
    # EINZIGE Event-Loop, der auch das Live-Audio-Streaming/WebSockets
    # bedient (2026-08-19 Review-Fund).
    try:
        filename, opus_path = await loop.run_in_executor(None, _unique_opus_path, dt, safe_room)
    except OSError as e:
        log.warning("Dateinamens-Reservierung fehlgeschlagen (%s): %s", wav_path, e)
        return None
    try:
        duration = await loop.run_in_executor(
            None, wav_to_opus, wav_path, opus_path
        )
    except Exception as e:
        log.warning("Opus-Konvertierung fehlgeschlagen (%s): %s", wav_path, e)
        # Die per _unique_opus_path reservierte leere Platzhalterdatei wieder
        # entfernen, sonst bleibt eine 0-Byte-Leiche liegen.
        try:
            os.unlink(opus_path)
        except OSError:
            pass
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
    """Synchrone Version für Batch-Verarbeitung. None bei Fehler (auch bei
    Datei-I/O-Problemen bei der Dateinamens-Reservierung, nicht nur bei der
    Opus-Konvertierung) -- konsistent mit der dokumentierten Rueckgabe."""
    dt        = datetime.fromtimestamp(timestamp)
    safe_room = room.replace("/", "_").replace(" ", "_")
    try:
        filename, opus_path = _unique_opus_path(dt, safe_room)
    except OSError as e:
        log.warning("Dateinamens-Reservierung fehlgeschlagen (%s): %s", wav_path, e)
        return None

    try:
        duration = wav_to_opus(wav_path, opus_path)
    except Exception as e:
        log.warning("Opus-Konvertierung fehlgeschlagen (%s): %s", wav_path, e)
        try:
            os.unlink(opus_path)
        except OSError:
            pass
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

        # params ohne die angehängten limit/offset für die COUNT-Abfrage
        total = conn.execute(
            f"SELECT COUNT(*) FROM transmissions {where}",
            params[:-2]
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


def add_chat_message(room: str, callsign: str, text: str, timestamp: float | None = None) -> None:
    """Speichert eine FRN-Textnachricht im Archiv."""
    if timestamp is None:
        timestamp = time.time()
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO chat_messages (timestamp, room, callsign, text) VALUES (?, ?, ?, ?)",
                (timestamp, room, callsign, text)
            )
    except Exception as e:
        log.warning("Chat-DB-Fehler: %s", e)


def query_chat_messages(
    limit: int = 100,
    offset: int = 0,
    room: str = "",
    search: str = "",
) -> tuple[list[dict], int]:
    clauses, params = [], []
    if room:
        clauses.append("room = ?")
        params.append(room)
    if search:
        clauses.append("(text LIKE ? OR callsign LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    count_params = params[:]
    params.extend([limit, offset])
    with _get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, timestamp, room, callsign, text FROM chat_messages "
            f"{where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM chat_messages {where}", count_params
        ).fetchone()[0]
    return [
        {
            "id":        r["id"],
            "timestamp": r["timestamp"],
            "time":      datetime.fromtimestamp(r["timestamp"]).strftime("%Y-%m-%d %H:%M:%S"),
            "room":      r["room"],
            "callsign":  r["callsign"],
            "text":      r["text"],
        }
        for r in rows
    ], total


def get_chat_rooms() -> list[str]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT room FROM chat_messages ORDER BY room"
        ).fetchall()
    return [r[0] for r in rows if r[0]]


def get_rooms() -> list[str]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT room FROM transmissions ORDER BY room"
        ).fetchall()
    return [r[0] for r in rows if r[0]]


# ── Statistiken ───────────────────────────────────────────────────────────────

def get_stats(days: int = 30) -> dict:
    """Liefert Archiv-Statistiken für das Dashboard."""
    since = time.time() - days * 86400
    with _get_conn() as conn:
        total_tx = conn.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0]
        total_dur = conn.execute(
            "SELECT COALESCE(SUM(duration_s),0) FROM transmissions"
        ).fetchone()[0]
        total_chat = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]

        # Übertragungen pro Tag (letzte `days` Tage)
        rows = conn.execute("""
            SELECT date(timestamp,'unixepoch','localtime') AS d, COUNT(*) AS n,
                   COALESCE(SUM(duration_s),0) AS dur
            FROM transmissions WHERE timestamp >= ?
            GROUP BY d ORDER BY d
        """, (since,)).fetchall()
        per_day = [{"date": r["d"], "count": r["n"], "duration_s": round(r["dur"], 1)}
                   for r in rows]

        # Top-Rufzeichen
        top_cs = conn.execute("""
            SELECT callsign, COUNT(*) AS n, COALESCE(SUM(duration_s),0) AS dur
            FROM transmissions WHERE callsign != ''
            GROUP BY callsign ORDER BY n DESC LIMIT 10
        """).fetchall()
        top_callsigns = [{"callsign": r["callsign"], "count": r["n"],
                          "duration_s": round(r["dur"], 1)} for r in top_cs]

        # Top-Räume
        top_rm = conn.execute("""
            SELECT room, COUNT(*) AS n, COALESCE(SUM(duration_s),0) AS dur
            FROM transmissions WHERE room != ''
            GROUP BY room ORDER BY n DESC LIMIT 10
        """).fetchall()
        top_rooms = [{"room": r["room"], "count": r["n"],
                      "duration_s": round(r["dur"], 1)} for r in top_rm]

    return {
        "total_transmissions": total_tx,
        "total_duration_s":    round(total_dur, 1),
        "total_chat_messages": total_chat,
        "per_day":             per_day,
        "top_callsigns":       top_callsigns,
        "top_rooms":           top_rooms,
    }


# ── Kommentare ────────────────────────────────────────────────────────────────

def get_comments(entry_id: int) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, timestamp, text FROM comments WHERE entry_id = ? ORDER BY timestamp",
            (entry_id,)
        ).fetchall()
    return [{"id": r["id"],
             "time": datetime.fromtimestamp(r["timestamp"]).strftime("%H:%M"),
             "text": r["text"]} for r in rows]


def add_comment(entry_id: int, text: str) -> int | None:
    """None, wenn entry_id nicht (mehr) existiert. Seit PRAGMA foreign_keys=ON
    (siehe _get_conn, fuer delete_entry's Kaskade noetig) schlaegt das
    INSERT dafuer jetzt mit IntegrityError fehl statt frueher eine
    verwaiste Kommentar-Zeile anzulegen -- kann z.B. passieren, wenn ein
    Browser-Tab noch einen Eintrag zeigt, der inzwischen zerlegt/geloescht
    wurde (2026-08-18 Review-Fund)."""
    ts = time.time()
    try:
        with _get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO comments (entry_id, timestamp, text) VALUES (?, ?, ?)",
                (entry_id, ts, text.strip())
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        log.info("Kommentar verworfen: Eintrag #%d existiert nicht (mehr)", entry_id)
        return None


def get_entry(entry_id: int) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, timestamp, room, callsign, text, audio_file, duration_s "
            "FROM transmissions WHERE id = ?", (entry_id,)
        ).fetchone()
    if not row:
        return None
    return {"id": row["id"], "timestamp": row["timestamp"], "room": row["room"],
            "callsign": row["callsign"], "text": row["text"],
            "audio_file": row["audio_file"], "duration_s": row["duration_s"]}


def count_entries_between(room: str, ts_start: float, ts_end: float) -> int:
    """Anzahl Eintraege in einem Raum mit Zeitstempel STRIKT zwischen
    ts_start und ts_end -- fuer's Zusammenkleben zweier Eintraege (nur
    erlaubt, wenn nichts dazwischen liegt, sonst wuerde das Zwischenstueck
    stillschweigend uebersprungen)."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM transmissions WHERE room = ? AND timestamp > ? AND timestamp < ?",
            (room, ts_start, ts_end)
        ).fetchone()
    return row[0]


def update_callsign(entry_id: int, callsign: str) -> bool:
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE transmissions SET callsign = ? WHERE id = ?",
            (callsign.strip(), entry_id)
        )
        return cur.rowcount > 0


def delete_entry(entry_id: int) -> bool:
    """Loescht einen Archiv-Eintrag inkl. Audio-Datei (z.B. nach dem
    Zerlegen einer Mehr-Sprecher-Aufnahme in mehrere neue Eintraege)."""
    entry = get_entry(entry_id)
    if not entry:
        return False
    with _get_conn() as conn:
        conn.execute("DELETE FROM transmissions WHERE id = ?", (entry_id,))
    if entry.get("audio_file"):
        try:
            (AUDIO_DIR / entry["audio_file"]).unlink(missing_ok=True)
        except OSError as e:
            log.warning("Audio-Datei nicht geloescht (%s): %s", entry["audio_file"], e)
    return True
