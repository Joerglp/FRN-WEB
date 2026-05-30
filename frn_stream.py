#!/usr/bin/env python3
"""
FRN Audio Stream Client

Connects to an FRN server as a listener, decodes WAV49 GSM audio, and
writes raw PCM (s16le 8 kHz mono) to stdout for piping to FFmpeg.

Silence = true PCM zeros → no hum/buzz.
Audio   = libgsm WAV49 decode (return value ignored so broken frames
          still produce output instead of being zeroed out).

Protocol reference: frnprotocol.htm + Net::FRN Perl module (Client.pm)
"""

import argparse
import ctypes
import ctypes.util
import json
import logging
import os
import queue
import re
import select
import signal
import socket
import struct
import sys
import threading
import time
import wave
from pathlib import Path

# --- Constants ---

FRN_PROTO_VERSION = "2014003"

FRN_TYPE_CROSSLINK = "0"
FRN_TYPE_GATEWAY   = "1"
FRN_TYPE_PC_ONLY   = "2"

MARKER_KEEPALIVE   = 0x00
MARKER_TX_APPROVE  = 0x01
MARKER_SOUND       = 0x02
MARKER_CLIENTS     = 0x03
MARKER_MESSAGE     = 0x04
MARKER_NETWORKS    = 0x05
MARKER_ADMIN_LIST  = 0x06
MARKER_ACCESS_LIST = 0x07
MARKER_BAN         = 0x08
MARKER_MUTE        = 0x09
MARKER_ACCESS_MODE = 0x0A

WAV49_BLOCK_SIZE        = 65   # One WAV49 frame pair (33 + 32 bytes)
WAV49_BLOCKS_PER_PACKET = 5    # 5 pairs per FRN audio packet = 200 ms
AUDIO_PACKET_SIZE       = 325  # 5 × 65 bytes
RX_PACKET_SIZE          = 327  # 2-byte client index + 325-byte audio

PCM_BLOCK_BYTES = 640   # 320 samples × 2 bytes (one decoded WAV49 pair = 40 ms)
PCM_SILENCE     = b"\x00" * PCM_BLOCK_BYTES   # true digital silence

GSM_OPT_WAV49      = 4
KEEPALIVE_INTERVAL = 1.0

log = logging.getLogger("frn_stream")


# ---------------------------------------------------------------------------
# GSM decoder (WAV49 mode)
# ---------------------------------------------------------------------------

class GSMDecoder:
    """Decode WAV49 GSM audio to 16-bit signed PCM using libgsm.

    Key points:
    * argtypes use c_void_p to avoid ctypes null-termination quirks with c_char_p
    * gsm_decode return value is intentionally ignored — the library still writes
      output even for frames it flags as erroneous, and silence insertion would
      be more disruptive than a mildly imperfect frame.
    """

    def __init__(self):
        lib_path = ctypes.util.find_library("gsm")
        if not lib_path:
            for path in [
                "/usr/lib/aarch64-linux-gnu/libgsm.so.1",
                "/usr/lib/x86_64-linux-gnu/libgsm.so.1",
                "/usr/lib/libgsm.so.1",
                "libgsm.so.1",
            ]:
                if os.path.exists(path):
                    lib_path = path
                    break
        if not lib_path:
            raise RuntimeError("libgsm not found. Install libgsm1-dev.")

        self.lib = ctypes.CDLL(lib_path)

        self.lib.gsm_create.restype  = ctypes.c_void_p
        self.lib.gsm_create.argtypes = []

        self.lib.gsm_option.restype  = ctypes.c_int
        self.lib.gsm_option.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                         ctypes.POINTER(ctypes.c_int)]

        # c_void_p avoids the null-termination behaviour of c_char_p
        self.lib.gsm_decode.restype  = ctypes.c_int
        self.lib.gsm_decode.argtypes = [ctypes.c_void_p,  # gsm handle
                                         ctypes.c_void_p,  # gsm_byte *src
                                         ctypes.c_void_p]  # gsm_signal *dst

        self.lib.gsm_destroy.restype  = None
        self.lib.gsm_destroy.argtypes = [ctypes.c_void_p]

        self.handle = self.lib.gsm_create()
        if not self.handle:
            raise RuntimeError("gsm_create() failed")

        val = ctypes.c_int(1)
        self.lib.gsm_option(self.handle, GSM_OPT_WAV49, ctypes.byref(val))

        # Output buffer: 160 samples × 2 bytes = 320 bytes per GSM frame
        self._pcm_buf = ctypes.create_string_buffer(320)

    def decode_pair(self, pair_65):
        """Decode one 65-byte WAV49 pair → 640 bytes of PCM (320 samples, 40 ms).

        Returns bytes even if gsm_decode signals an error — imperfect audio
        beats sudden silence mid-transmission.
        """
        if len(pair_65) != WAV49_BLOCK_SIZE:
            return PCM_SILENCE

        pcm_out = bytearray()

        # Odd frame: first 33 bytes
        src = ctypes.create_string_buffer(bytes(pair_65[:33]), 33)
        self.lib.gsm_decode(self.handle, src, self._pcm_buf)
        pcm_out.extend(self._pcm_buf.raw)

        # Even frame: next 32 bytes
        src = ctypes.create_string_buffer(bytes(pair_65[33:65]), 32)
        self.lib.gsm_decode(self.handle, src, self._pcm_buf)
        pcm_out.extend(self._pcm_buf.raw)

        return bytes(pcm_out)   # 640 bytes

    def close(self):
        if self.handle:
            self.lib.gsm_destroy(self.handle)
            self.handle = None


# ---------------------------------------------------------------------------
# FRN protocol client
# ---------------------------------------------------------------------------

class FRNClient:
    """FRN protocol client — receive-only listener."""

    def __init__(self, server, port, email, password, room,
                 callsign="WebStream", description="Audio Stream",
                 country="DE", city="Stream",
                 client_type=FRN_TYPE_PC_ONLY):
        self.server      = server
        self.port        = port
        self.email       = email
        self.password    = password
        self.room        = room
        self.callsign    = callsign
        self.description = description
        self.country     = country
        self.city        = city
        self.client_type = client_type

        self.sock           = None
        self.inbuffer       = b""
        self.clients        = []
        self.connected      = False
        self.last_keepalive = 0
        self.rx_sent        = False

    def connect(self):
        log.info("Connecting to %s:%d room '%s'...", self.server, self.port, self.room)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect((self.server, self.port))
        self.sock.setblocking(False)
        self.connected = True

        ct = (
            f"CT:"
            f"<VX>{FRN_PROTO_VERSION}</VX>"
            f"<EA>{self.email}</EA>"
            f"<PW>{self.password}</PW>"
            f"<ON>{self.callsign}</ON>"
            f"<CL>{self.client_type}</CL>"
            f"<BC>0</BC>"
            f"<DS>{self.description}</DS>"
            f"<NN>{self.country}</NN>"
            f"<CT>{self.city}</CT>"
            f"<NT>{self.room}</NT>"
            f"\r\n"
        )
        self._send_raw(ct.encode("utf-8"))

        version_line = self._read_line(timeout=10)
        log.info("Server version: %s", version_line)

        result_line  = self._read_line(timeout=10)
        result       = self._parse_xml_tags(result_line)
        access_level = result.get("AL", "UNKNOWN")
        log.info("Login: AL=%s SV=%s", access_level, result.get("SV", "?"))

        if access_level in ("OK", "ADMIN", "OWNER", "NETOWNER"):
            log.info("Logged in as %s", access_level)
        elif access_level == "WRONG":
            raise ConnectionError("Authentication failed")
        elif access_level == "BLOCK":
            raise ConnectionError("Blocked by server")
        else:
            raise ConnectionError(f"Login failed: AL={access_level}")

        self.last_keepalive = time.time()
        self.rx_sent = False

    def run(self, pcm_callback, debug=False):
        """Receive loop.  pcm_callback(pcm_640_bytes, callsign) called per decoded WAV49 pair."""
        gsm = GSMDecoder()
        try:
            while self.connected:
                now = time.time()

                if now - self.last_keepalive > KEEPALIVE_INTERVAL:
                    self._send_ping()
                    self.last_keepalive = now

                if not self.rx_sent:
                    self._send_rx0()
                    self.rx_sent = True

                ready = select.select([self.sock], [], [], 0.05)
                if ready[0]:
                    try:
                        data = self.sock.recv(8192)
                    except (ConnectionError, OSError) as exc:
                        log.error("Socket error: %s", exc)
                        break
                    if not data:
                        log.warning("Server closed connection")
                        break
                    self.inbuffer += data

                while self._has_bytes(1):
                    marker = self.inbuffer[0]

                    if marker == MARKER_KEEPALIVE:
                        self._consume(1)

                    elif marker == MARKER_SOUND:
                        if not self._has_bytes(1 + RX_PACKET_SIZE):
                            break
                        self._consume(1)      # marker
                        idx_bytes = self._consume(2)
                        client_idx = struct.unpack(">H", bytes(idx_bytes))[0]
                        gsm_data = self._consume(AUDIO_PACKET_SIZE)
                        # Callsign aus Client-Liste
                        callsign = ""
                        if 0 <= client_idx < len(self.clients):
                            callsign = self.clients[client_idx].get("ON", "")
                        if debug:
                            log.debug("SOUND idx=%d callsign=%s", client_idx, callsign)
                        # Decode each of the 5 WAV49 pairs individually
                        for i in range(WAV49_BLOCKS_PER_PACKET):
                            pair = gsm_data[i * WAV49_BLOCK_SIZE :
                                            (i + 1) * WAV49_BLOCK_SIZE]
                            pcm = gsm.decode_pair(pair)
                            pcm_callback(pcm, callsign)

                    elif marker == MARKER_TX_APPROVE:
                        if not self._has_bytes(3):
                            break
                        self._consume(3)

                    elif marker == MARKER_CLIENTS:
                        self._consume(1)
                        self._parse_client_list()

                    elif marker == MARKER_MESSAGE:
                        self._consume(1)
                        self._parse_message()

                    elif marker == MARKER_NETWORKS:
                        self._consume(1)
                        self._parse_line_list("networks")

                    elif marker in (MARKER_BAN, MARKER_MUTE,
                                    MARKER_ADMIN_LIST, MARKER_ACCESS_LIST,
                                    MARKER_ACCESS_MODE):
                        self._consume(1)
                        self._parse_line_list(f"0x{marker:02x}")

                    else:
                        log.warning("Unknown marker 0x%02x, skipping", marker)
                        self._consume(1)
        finally:
            gsm.close()

    def close(self):
        self.connected = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    # --- Protocol helpers ---

    def _send_raw(self, data):
        try:
            self.sock.sendall(data)
        except (ConnectionError, OSError) as exc:
            log.error("Send error: %s", exc)
            self.connected = False

    def _send_ping(self):
        self._send_raw(b"P\r\n")

    def _send_rx0(self):
        self._send_raw(b"RX0\r\n")
        log.debug("Sent RX0")

    def _read_line(self, timeout=10):
        deadline = time.time() + timeout
        while True:
            idx = self.inbuffer.find(b"\r\n")
            if idx >= 0:
                line = self.inbuffer[:idx].decode("utf-8", errors="replace")
                self.inbuffer = self.inbuffer[idx + 2:]
                return line
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("Timeout reading server response")
            self.sock.setblocking(True)
            self.sock.settimeout(remaining)
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                raise TimeoutError("Timeout reading server response")
            finally:
                self.sock.setblocking(False)
            if not data:
                raise ConnectionError("Server closed connection during login")
            self.inbuffer += data

    def _has_bytes(self, n):
        return len(self.inbuffer) >= n

    def _consume(self, n):
        data = self.inbuffer[:n]
        self.inbuffer = self.inbuffer[n:]
        return data

    def _wait_for_line(self, timeout=2.0):
        deadline = time.time() + timeout
        while True:
            idx = self.inbuffer.find(b"\r\n")
            if idx >= 0:
                line = self.inbuffer[:idx].decode("utf-8", errors="replace")
                self.inbuffer = self.inbuffer[idx + 2:]
                return line
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            ready = select.select([self.sock], [], [], min(remaining, 0.5))
            if ready[0]:
                try:
                    data = self.sock.recv(4096)
                except (ConnectionError, OSError):
                    return None
                if not data:
                    return None
                self.inbuffer += data

    def _parse_client_list(self):
        if self._has_bytes(2):
            self._consume(2)
        count_str = self._wait_for_line()
        if count_str is None:
            return
        try:
            count = int(count_str.strip())
        except ValueError:
            return
        self.clients = []
        for _ in range(count):
            line = self._wait_for_line()
            if line is None:
                break
            self.clients.append(self._parse_xml_tags(line))
        log.info("Client list: %d clients", len(self.clients))
        for c in self.clients:
            log.debug("  %s (%s)", c.get("ON", "?"), c.get("DS", ""))

    def _parse_message(self):
        count_str = self._wait_for_line()
        if count_str is None:
            return
        try:
            count = int(count_str.strip())
        except ValueError:
            return
        for _ in range(count):
            self._wait_for_line()

    def _parse_line_list(self, name):
        count_str = self._wait_for_line()
        if count_str is None:
            return
        try:
            count = int(count_str.strip())
        except ValueError:
            return
        for _ in range(count):
            self._wait_for_line()
        log.debug("Parsed %s: %d entries", name, count)

    @staticmethod
    def _parse_xml_tags(text):
        result = {}
        for m in re.finditer(r"<(\w+)>(.*?)(?:</\1>)?(?=<\w+>|$)", text):
            result[m.group(1)] = m.group(2)
        return result


# ---------------------------------------------------------------------------
# RoomRecorder — erkennt TX-Sessions, speichert WAV + Metadaten
# ---------------------------------------------------------------------------

class RoomRecorder:
    """
    Puffert PCM-Audio, erkennt Sendepausen und speichert abgeschlossene
    Übertragungen als WAV + .meta JSON.  Die Transkription übernimmt der
    TX-Server (zentraler Whisper-Prozess).
    """

    SILENCE_TIMEOUT = 4.0    # Sekunden Stille nach letztem Audio → Session beendet
    MIN_DURATION    = 1.5    # Sekunden Mindestlänge (kürzere werden verworfen)
    MAX_DURATION    = 300.0  # Sekunden Maximallänge (erzwungener Schnitt)
    SAMPLE_RATE     = 8000
    SAMPLE_WIDTH    = 2      # int16

    def __init__(self, room_name: str, wav_dir: str):
        self.room_name = room_name
        self.wav_dir   = Path(wav_dir)
        self.wav_dir.mkdir(parents=True, exist_ok=True)
        self._buf: list[bytes] = []
        self._callsign  = ""
        self._start_ts  = 0.0
        self._last_ts   = 0.0
        self._active    = False
        self._lock      = threading.Lock()
        self._timer: threading.Timer | None = None

    def feed(self, pcm: bytes, callsign: str = ""):
        """640-Byte PCM-Block einreichen."""
        is_silence = (pcm == PCM_SILENCE or pcm == b"\x00" * len(pcm))

        with self._lock:
            if is_silence:
                if not self._active:
                    return  # Noch keine Session — Stille ignorieren
                # Session aktiv: Stille in Puffer schreiben (korrekte Zeitstempel)
                # Timer läuft bereits — nicht zurücksetzen
                self._buf.append(pcm)
                return

            # Echte Audio-Daten
            if not self._active:
                self._active   = True
                self._start_ts = time.time()
                self._buf      = []
                self._callsign = callsign or ""
                log.debug("[%s] TX-Session gestartet (%s)", self.room_name, callsign)
            elif callsign:
                self._callsign = callsign

            self._buf.append(pcm)
            self._last_ts = time.time()

            # Maximallänge überschritten → Session zwangsweise beenden
            max_bytes = int(self.MAX_DURATION * self.SAMPLE_RATE) * self.SAMPLE_WIDTH
            if sum(len(b) for b in self._buf) >= max_bytes:
                log.info("[%s] MAX_DURATION erreicht — Session wird gespeichert",
                         self.room_name)
                if self._timer:
                    self._timer.cancel()
                    self._timer = None
                self._active  = False
                pcm_data      = b"".join(self._buf)
                callsign_snap = self._callsign
                start_ts      = self._start_ts
                self._buf     = []
                threading.Thread(
                    target=self._save, args=(pcm_data, callsign_snap, start_ts),
                    daemon=True).start()
                return

        # Timer zurücksetzen — nur bei echtem Audio
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self.SILENCE_TIMEOUT, self._on_silence)
        self._timer.daemon = True
        self._timer.start()

    def _on_silence(self):
        with self._lock:
            if not self._active:
                return
            self._active  = False
            self._timer   = None
            pcm_data      = b"".join(self._buf)
            self._buf     = []
            callsign      = self._callsign
            start_ts      = self._start_ts

        min_bytes = int(self.MIN_DURATION * self.SAMPLE_RATE) * self.SAMPLE_WIDTH
        if len(pcm_data) < min_bytes:
            log.debug("[%s] Session zu kurz — verworfen", self.room_name)
            return

        threading.Thread(
            target=self._save,
            args=(pcm_data, callsign, start_ts),
            daemon=True,
        ).start()

    def _save(self, pcm_data: bytes, callsign: str, ts: float):
        from datetime import datetime
        dt   = datetime.fromtimestamp(ts)
        # Raum in den Dateinamen aufnehmen: alle Raum-Dienste schreiben ins
        # selbe wav_dir. Ohne Raum kollidieren Übertragungen, die in zwei
        # Räumen in derselben Sekunde starten — eine Aufnahme würde die andere
        # überschreiben (Datenverlust). safe_room wie in frn_archive.add_entry.
        safe_room = re.sub(r"[^A-Za-z0-9_-]", "_", self.room_name) or "room"
        name = dt.strftime(f"frn-%Y%m%d-%H%M%S-{safe_room}")
        wav_path  = self.wav_dir / f"{name}.wav"
        meta_path = self.wav_dir / f"{name}.meta"

        # WAV schreiben
        try:
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(self.SAMPLE_WIDTH)
                wf.setframerate(self.SAMPLE_RATE)
                wf.writeframes(pcm_data)
        except Exception as e:
            log.warning("[%s] WAV-Schreibfehler: %s", self.room_name, e)
            return

        # Meta-Datei schreiben (wird vom TX-Server aufgegriffen)
        meta = {
            "room":      self.room_name,
            "callsign":  callsign,
            "timestamp": ts,
            "wav":       str(wav_path),
        }
        try:
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
        except Exception as e:
            log.warning("[%s] Meta-Schreibfehler: %s", self.room_name, e)
            return

        log.info("[%s] Aufnahme gespeichert: %s (%s)", self.room_name, wav_path.name, callsign)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="FRN Audio Stream Client — writes PCM s16le 8 kHz to stdout")
    parser.add_argument("--server",      default="localhost")
    parser.add_argument("--port",        type=int, default=10024)
    parser.add_argument("--room",        required=True)
    parser.add_argument("--email",       required=True)
    parser.add_argument("--password",    required=True)
    parser.add_argument("--callsign",    default="WebStream")
    parser.add_argument("--description", default="Audio Stream")
    parser.add_argument("--country",     default="DE")
    parser.add_argument("--city",        default="Stream")
    parser.add_argument("--debug",       action="store_true")
    parser.add_argument("--record",      action="store_true",
                        help="WAV-Aufnahmen + Meta für Transkription speichern")
    parser.add_argument("--wav-dir",     default="/opt/FRN/recordings",
                        help="Verzeichnis für WAV-Aufnahmen (default: /opt/FRN/recordings)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        stream=sys.stderr,
    )

    stdout_bin = os.fdopen(sys.stdout.fileno(), "wb", buffering=0)

    # ---- Recorder (optional) ----
    recorder = None
    if args.record:
        recorder = RoomRecorder(room_name=args.room, wav_dir=args.wav_dir)
        log.info("Aufnahme aktiv: %s → %s", args.room, args.wav_dir)

    # ---- Timed output thread ----
    # Each slot = 640 bytes = 320 samples = 40 ms at 8 kHz 16-bit mono.
    # 40 ms granularity: a missed slot is a barely-perceptible 40 ms gap.
    # maxsize = 10 → 400 ms jitter buffer (low latency).
    audio_q = queue.Queue(maxsize=10)
    BLOCK_DURATION = 0.040   # 40 ms per block

    def output_thread():
        next_t = time.monotonic()
        while True:
            now  = time.monotonic()
            wait = next_t - now
            if wait > 0:
                time.sleep(wait)
            try:
                block = audio_q.get_nowait()
            except queue.Empty:
                block = PCM_SILENCE   # true digital zeros — no hum
            try:
                stdout_bin.write(block)
            except BrokenPipeError:
                log.info("Stdout pipe closed")
                os._exit(0)
            next_t += BLOCK_DURATION

    threading.Thread(target=output_thread, daemon=True).start()

    def pcm_callback(pcm_block, callsign=""):
        """Receive one decoded 640-byte PCM block from FRNClient.run()."""
        try:
            audio_q.put_nowait(pcm_block)
        except queue.Full:
            log.debug("Audio queue full, dropping block")
        if recorder:
            recorder.feed(pcm_block, callsign)

    def signal_handler(signum, _frame):
        log.info("Signal %d, exiting", signum)
        sys.exit(0)

    signal.signal(signal.SIGINT,  signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    client = FRNClient(
        server=args.server, port=args.port,
        email=args.email,   password=args.password,
        room=args.room,     callsign=args.callsign,
        description=args.description,
        country=args.country, city=args.city,
    )

    try:
        client.connect()
        client.run(pcm_callback, debug=args.debug)
    except (ConnectionError, TimeoutError, OSError) as exc:
        log.error("Connection error: %s", exc)
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
