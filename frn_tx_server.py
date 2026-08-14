#!/usr/bin/env python3
"""
FRN Web TX Server
=================
HTTP + WebSocket server for browser-based PTT transmitting into FRN rooms.
Includes an admin API for managing users and rooms at runtime.

Usage:
    python3 frn_tx_server.py [--config /path/to/config.json]
                             [--host 0.0.0.0] [--port 8765]
                             [--frn-server localhost] [--frn-port 10024]
                             [--users tx_users.json] [--rooms tx_rooms.json]
"""

import argparse
import asyncio
import collections
import ctypes
import ctypes.util
import difflib
import hashlib
import json
import logging
import os
import re
import secrets
import signal
import struct
import subprocess
import time
from pathlib import Path

import aiohttp
import numpy as np
from aiohttp import web
from scipy.signal import resample as sp_resample

try:
    from frn_transcription import TranscriptionPipeline
    _TRANSCRIPTION_AVAILABLE = True
except ImportError:
    _TRANSCRIPTION_AVAILABLE = False

try:
    import frn_archive as _archive
    _archive.init_db()
    _ARCHIVE_AVAILABLE = True
except Exception:
    _ARCHIVE_AVAILABLE = False

log = logging.getLogger("frn_tx")

# ── FRN protocol constants ──────────────────────────────────────────────────
FRN_PROTO_VERSION = "2014003"
FRN_TYPE_PC_ONLY  = "2"
MARKER_KEEPALIVE  = 0x00
MARKER_TX_APPROVE = 0x01
MARKER_SOUND      = 0x02
MARKER_CLIENTS    = 0x03
MARKER_MESSAGE    = 0x04
MARKER_NETWORKS   = 0x05
MARKER_ADMIN_LIST = 0x06
MARKER_ACCESS_LIST= 0x07
MARKER_BAN        = 0x08
MARKER_MUTE       = 0x09
MARKER_ACCESS_MODE= 0x0A
GSM_OPT_WAV49     = 4

LINE_LIST_MARKERS = frozenset({
    MARKER_NETWORKS, MARKER_ADMIN_LIST, MARKER_ACCESS_LIST,
    MARKER_BAN, MARKER_MUTE, MARKER_ACCESS_MODE,
})

AUDIO_PACKET_SIZE  = 325
PCM_PACKET_SAMPLES = 1600
PCM_PACKET_BYTES   = 3200
KEEPALIVE_INTERVAL = 2.0


# ── GSM Encoder ─────────────────────────────────────────────────────────────

class GSMEncoder:
    """Encode PCM s16le → WAV49 GSM using libgsm."""

    def __init__(self):
        lib_path = ctypes.util.find_library("gsm")
        if not lib_path:
            for p in ("/usr/lib/aarch64-linux-gnu/libgsm.so.1",
                      "/usr/lib/x86_64-linux-gnu/libgsm.so.1",
                      "/usr/lib/libgsm.so.1"):
                if os.path.exists(p):
                    lib_path = p
                    break
        if not lib_path:
            raise RuntimeError("libgsm not found — install libgsm1")

        lib = ctypes.CDLL(lib_path)
        lib.gsm_create.restype  = ctypes.c_void_p
        lib.gsm_create.argtypes = []
        lib.gsm_encode.restype  = None
        lib.gsm_encode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        lib.gsm_option.restype  = ctypes.c_int
        lib.gsm_option.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                   ctypes.POINTER(ctypes.c_int)]
        lib.gsm_destroy.restype  = None
        lib.gsm_destroy.argtypes = [ctypes.c_void_p]
        self.lib    = lib
        self.handle = lib.gsm_create()
        if not self.handle:
            raise RuntimeError("gsm_create() failed")

        val = ctypes.c_int(1)
        lib.gsm_option(self.handle, GSM_OPT_WAV49, ctypes.byref(val))

    def encode_packet(self, pcm_bytes: bytes) -> bytes:
        """Encode 3200 bytes PCM s16le (1600 samples @ 8 kHz) → 325 bytes WAV49."""
        if len(pcm_bytes) < PCM_PACKET_BYTES:
            pcm_bytes = pcm_bytes + b"\x00" * (PCM_PACKET_BYTES - len(pcm_bytes))
        else:
            pcm_bytes = pcm_bytes[:PCM_PACKET_BYTES]

        out = bytearray(325)
        for pair in range(5):
            s = pair * 2 * 320
            dst1 = ctypes.create_string_buffer(33)
            src1 = ctypes.create_string_buffer(pcm_bytes[s:s + 320], 320)
            self.lib.gsm_encode(self.handle,
                                ctypes.cast(src1, ctypes.c_void_p),
                                ctypes.cast(dst1, ctypes.c_void_p))
            dst2 = ctypes.create_string_buffer(33)
            src2 = ctypes.create_string_buffer(pcm_bytes[s + 320:s + 640], 320)
            self.lib.gsm_encode(self.handle,
                                ctypes.cast(src2, ctypes.c_void_p),
                                ctypes.cast(dst2, ctypes.c_void_p))
            base = pair * 65
            out[base:base + 32]      = dst1.raw[:32]
            out[base + 32:base + 65] = dst2.raw[:33]

        return bytes(out)

    def close(self):
        if self.handle:
            self.lib.gsm_destroy(self.handle)
            self.handle = None


# ── GSM Decoder ─────────────────────────────────────────────────────────────

class GSMDecoder:
    """Decode WAV49 GSM → PCM s16le using libgsm.

    WAV49 packs two GSM frames into 65 bytes:
      even half: 32 bytes  → 160 samples (320 bytes PCM)
      odd  half: 33 bytes  → 160 samples (320 bytes PCM)
    One 325-byte FRN packet = 5 pairs = 3200 bytes PCM @ 8 kHz mono.
    """

    def __init__(self):
        lib_path = ctypes.util.find_library("gsm")
        if not lib_path:
            for p in ("/usr/lib/aarch64-linux-gnu/libgsm.so.1",
                      "/usr/lib/x86_64-linux-gnu/libgsm.so.1",
                      "/usr/lib/libgsm.so.1"):
                if os.path.exists(p):
                    lib_path = p
                    break
        if not lib_path:
            raise RuntimeError("libgsm not found — install libgsm1")

        lib = ctypes.CDLL(lib_path)
        lib.gsm_create.restype  = ctypes.c_void_p
        lib.gsm_create.argtypes = []
        lib.gsm_decode.restype  = ctypes.c_int
        lib.gsm_decode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        lib.gsm_option.restype  = ctypes.c_int
        lib.gsm_option.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                   ctypes.POINTER(ctypes.c_int)]
        lib.gsm_destroy.restype  = None
        lib.gsm_destroy.argtypes = [ctypes.c_void_p]
        self.lib    = lib
        self.handle = lib.gsm_create()
        if not self.handle:
            raise RuntimeError("gsm_create() failed")
        val = ctypes.c_int(1)
        lib.gsm_option(self.handle, GSM_OPT_WAV49, ctypes.byref(val))

    def decode_packet(self, wav49: bytes) -> bytes:
        """Decode 325 bytes WAV49 → 3200 bytes PCM s16le.

        WAV49 per-pair layout (65 bytes total):
          bytes [base   : base+33]  → even frame (33 bytes for gsm_decode)
          bytes [base+33 : base+65] → odd  frame (32 bytes, padded to 33 for gsm_decode)

        Note: the encoder stores 32 bytes for the even half and 33 for the odd half,
        but the first byte of the odd region is the 33rd byte consumed by the even decode.
        """
        if len(wav49) < 325:
            wav49 = bytes(wav49) + b"\x00" * (325 - len(wav49))
        out = bytearray(3200)
        for pair in range(5):
            base = pair * 65
            # even decode reads 33 bytes starting at base
            src1 = ctypes.create_string_buffer(bytes(wav49[base:base + 33]), 33)
            dst1 = ctypes.create_string_buffer(320)
            self.lib.gsm_decode(self.handle,
                                ctypes.cast(src1, ctypes.c_void_p),
                                ctypes.cast(dst1, ctypes.c_void_p))
            # odd decode reads 32 bytes starting at base+33 (pad to 33 for safety)
            src2 = ctypes.create_string_buffer(
                bytes(wav49[base + 33:base + 65]) + b"\x00", 33)
            dst2 = ctypes.create_string_buffer(320)
            self.lib.gsm_decode(self.handle,
                                ctypes.cast(src2, ctypes.c_void_p),
                                ctypes.cast(dst2, ctypes.c_void_p))
            out[pair * 640:pair * 640 + 320]       = dst1.raw
            out[pair * 640 + 320:pair * 640 + 640] = dst2.raw
        return bytes(out)

    def close(self):
        if self.handle:
            self.lib.gsm_destroy(self.handle)
            self.handle = None


# ── FRN TX Room ──────────────────────────────────────────────────────────────

class FRNTXRoom:
    """Persistent FRN connection for one room (TX only)."""

    def __init__(self, name: str, frn_server: str, frn_port: int,
                 email: str, password: str, callsign: str):
        self.name     = name
        self.server   = frn_server
        self.port     = frn_port
        self.email    = email
        self.password = password
        self.callsign = callsign

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected       = False
        self._tx_lock         = asyncio.Lock()
        self._keepalive_task: asyncio.Task | None = None
        self._reader_task:    asyncio.Task | None = None
        self._tx_approved     = asyncio.Event()
        self._encoder         = GSMEncoder()
        self._pcm_buf         = b""
        self._clients: list   = []   # last received MARKER_CLIENTS list
        self._rx_clients: set = set()   # WebSocket connections for RX audio
        self._chat_clients: set = set() # WebSocket connections for chat messages
        self._recorder        = None    # SessionRecorder (gesetzt nach load_config)
        self.on_message       = None    # callback(sender, text, room_name)
        try:
            self._gsm_dec = GSMDecoder()
        except RuntimeError as e:
            log.warning("GSM decoder unavailable: %s — RX stream disabled", e)
            self._gsm_dec = None

    async def ensure_connected(self):
        if self._connected:
            return
        log.info("[%s] Connecting to %s:%d …", self.name, self.server, self.port)
        self._reader, self._writer = await asyncio.open_connection(
            self.server, self.port)
        self._connected = True

        ct = (
            f"CT:"
            f"<VX>{FRN_PROTO_VERSION}</VX>"
            f"<EA>{self.email}</EA>"
            f"<PW>{self.password}</PW>"
            f"<ON>{self.callsign}</ON>"
            f"<CL>{FRN_TYPE_PC_ONLY}</CL>"
            f"<BC>0</BC>"
            f"<DS>WebTX</DS>"
            f"<NN>DE</NN>"
            f"<CT>Stream</CT>"
            f"<NT>{self.name}</NT>"
            f"\r\n"
        )
        self._writer.write(ct.encode())
        await self._writer.drain()

        version    = await asyncio.wait_for(self._reader.readline(), timeout=10)
        result_raw = await asyncio.wait_for(self._reader.readline(), timeout=10)
        result     = result_raw.decode(errors="replace")
        m  = re.search(r"<AL>(.*?)</AL>", result)
        al = m.group(1) if m else "?"
        if al not in ("OK", "ADMIN", "OWNER", "NETOWNER"):
            self._connected = False
            raise ConnectionError(f"FRN login failed: AL={al}")
        log.info("[%s] FRN login OK (AL=%s)", self.name, al)

        self._writer.write(b"RX0\r\n")
        await self._writer.drain()

        self._tx_approved.clear()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        self._reader_task    = asyncio.create_task(self._reader_loop())

    async def disconnect(self):
        self._connected = False
        for t in (self._keepalive_task, self._reader_task):
            if t:
                t.cancel()
        self._keepalive_task = None
        self._reader_task    = None
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._writer = None
        self._reader = None
        # Close all RX WebSocket listeners
        for ws in list(self._rx_clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._rx_clients.clear()

    async def send_text(self, text: str):
        """Send a text message to the FRN room."""
        if not self._writer or self._writer.is_closing():
            return
        try:
            msg = f"TM:\r\n1\r\n<ON>{self.callsign}</ON><TM>{text}</TM>\r\n"
            self._writer.write(msg.encode())
            await self._writer.drain()
        except Exception as e:
            log.debug("[%s] send_text error: %s", self.name, e)

    async def _keepalive_loop(self):
        try:
            while self._connected:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                if self._writer and not self._writer.is_closing():
                    self._writer.write(b"P\r\n")
                    await self._writer.drain()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("[%s] Keepalive: %s", self.name, e)
            self._connected = False

    @staticmethod
    def _parse_xml_tags(text: str) -> dict:
        result = {}
        for m in re.finditer(r"<(\w+)>(.*?)(?:</\1>)?(?=<\w+>|$)", text):
            result[m.group(1)] = m.group(2)
        return result

    def _try_parse_messages(self, buf: bytes):
        """Try to parse a MARKER_MESSAGE (0x04) block from buf.

        Returns (messages_list, remaining_buf) on success,
        or       (None,         original_buf)  if more data is needed.
        """
        orig = buf
        if not buf or buf[0] != MARKER_MESSAGE:
            return None, orig
        buf = buf[1:]
        idx = buf.find(b"\r\n")
        if idx < 0:
            return None, orig
        try:
            count = int(buf[:idx].decode(errors="replace").strip())
        except ValueError:
            return None, orig
        buf = buf[idx + 2:]
        messages = []
        for _ in range(count):
            idx = buf.find(b"\r\n")
            if idx < 0:
                return None, orig
            line = buf[:idx].decode(errors="replace")
            buf  = buf[idx + 2:]
            parsed = self._parse_xml_tags(line)
            if parsed:
                messages.append(parsed)
        return messages, buf

    def _try_parse_clients(self, buf: bytes):
        """Try to parse a MARKER_CLIENTS (0x03) block from buf.

        Returns (clients_list, remaining_buf) on success,
        or       (None,         original_buf) if more data is needed.
        """
        orig = buf
        if not buf or buf[0] != MARKER_CLIENTS:
            return None, orig
        buf = buf[1:]
        if len(buf) < 2:
            return None, orig
        buf = buf[2:]               # 2 extra bytes after marker
        idx = buf.find(b"\r\n")
        if idx < 0:
            return None, orig
        try:
            count = int(buf[:idx].decode(errors="replace").strip())
        except ValueError:
            return None, orig
        buf = buf[idx + 2:]
        clients = []
        for _ in range(count):
            idx = buf.find(b"\r\n")
            if idx < 0:
                return None, orig   # incomplete — wait for more data
            line = buf[:idx].decode(errors="replace")
            buf  = buf[idx + 2:]
            parsed = self._parse_xml_tags(line)
            if parsed:
                clients.append(parsed)
        return clients, buf

    @staticmethod
    def _try_parse_line_list(buf: bytes):
        """Skip a count-prefixed line-list packet (marker byte already included).

        Returns remaining_buf on success, or None if more data is needed.
        """
        if not buf:
            return None
        buf = buf[1:]  # skip marker
        idx = buf.find(b"\r\n")
        if idx < 0:
            return None
        try:
            count = int(buf[:idx].decode(errors="replace").strip())
        except ValueError:
            count = 0
        buf = buf[idx + 2:]
        for _ in range(count):
            idx = buf.find(b"\r\n")
            if idx < 0:
                return None
            buf = buf[idx + 2:]
        return buf

    async def _reader_loop(self):
        buf = b""
        try:
            while self._connected and self._reader:
                data = await self._reader.read(4096)
                if not data:
                    log.warning("[%s] FRN server closed connection", self.name)
                    self._connected = False
                    break
                buf += data

                # Consume as much of the buffer as possible
                progress = True
                while progress and buf:
                    progress = False
                    marker = buf[0]

                    if marker == MARKER_KEEPALIVE:          # 0x00 — single byte
                        buf = buf[1:]
                        progress = True

                    elif marker == MARKER_TX_APPROVE:       # 0x01 — 3 bytes total
                        if len(buf) < 3:
                            break
                        buf = buf[3:]
                        self._tx_approved.set()
                        progress = True

                    elif marker == MARKER_SOUND:            # 0x02 — 1+2+325 = 328 bytes
                        if len(buf) < 328:
                            break
                        if self._gsm_dec and (self._rx_clients or self._recorder):
                            wav49 = buf[3:328]
                            try:
                                pcm = self._gsm_dec.decode_packet(bytes(wav49))
                                if self._rx_clients:
                                    asyncio.create_task(self._broadcast_rx(pcm))
                                if self._recorder:
                                    client_idx = struct.unpack(">H", buf[1:3])[0]
                                    if 0 <= client_idx < len(self._clients):
                                        speaker = self._clients[client_idx].get("ON", "")
                                    else:
                                        speaker = next(
                                            (c.get("ON", "") for c in self._clients
                                             if c.get("ON")), ""
                                        )
                                    self._recorder.feed(pcm, speaker)
                            except Exception as e:
                                log.debug("[%s] GSM decode error: %s", self.name, e)
                        buf = buf[328:]
                        progress = True

                    elif marker == MARKER_CLIENTS:          # 0x03 — variable length
                        clients, new_buf = self._try_parse_clients(buf)
                        if clients is not None:
                            self._clients = clients
                            asyncio.create_task(self._dispatch_clients())
                            buf = new_buf
                            progress = True
                        else:
                            break                           # need more data

                    elif marker == MARKER_MESSAGE:          # 0x04 — text message
                        messages, new_buf = self._try_parse_messages(buf)
                        if messages is not None:
                            buf = new_buf
                            progress = True
                            for msg in messages:
                                sender = msg.get("ON", "")
                                text   = msg.get("TM", "")
                                if sender and text:
                                    asyncio.create_task(
                                        self._dispatch_message(sender, text))
                                    if self.on_message:
                                        asyncio.create_task(
                                            self.on_message(sender, text, self.name))
                                    if _ARCHIVE_AVAILABLE:
                                        try:
                                            _archive.add_chat_message(self.name, sender, text)
                                        except Exception:
                                            pass
                        else:
                            break                           # need more data

                    elif marker in LINE_LIST_MARKERS:   # 0x05–0x0A — count+lines
                        new_buf = self._try_parse_line_list(buf)
                        if new_buf is not None:
                            buf = new_buf
                            progress = True
                        else:
                            break                           # need more data

                    else:
                        buf = buf[1:]                       # skip truly unknown byte
                        progress = True

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("[%s] Reader: %s", self.name, e)
            self._connected = False

    async def _broadcast_rx(self, pcm: bytes):
        """Send decoded PCM bytes to all connected RX WebSocket listeners.

        Sends run concurrently with a per-client timeout: a stalled listener
        (e.g. an iOS device frozen on lock screen with a full write buffer)
        would otherwise block on send_bytes without ever raising, piling up
        hanging tasks. On timeout/error the client is dropped and closed.
        """
        clients = list(self._rx_clients)
        if not clients:
            return

        async def _send(ws):
            try:
                await asyncio.wait_for(ws.send_bytes(pcm), timeout=2.0)
                return None
            except Exception:
                return ws

        results = await asyncio.gather(*(_send(ws) for ws in clients))
        dead = {ws for ws in results if ws is not None}
        if dead:
            self._rx_clients -= dead
            for ws in dead:
                try:
                    await ws.close()
                except Exception:
                    pass

    async def _dispatch_message(self, sender: str, text: str):
        """Broadcast an incoming FRN text message to all connected chat clients."""
        payload = {"type": "chat", "from": sender, "text": text}
        dead = set()
        for ws in list(self._chat_clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.add(ws)
        self._chat_clients -= dead

    async def _dispatch_clients(self):
        """Broadcast current client list to all connected WS clients."""
        clients = [
            {"callsign": c.get("ON", "?"), "desc": c.get("DS", ""), "type": c.get("CL", "2")}
            for c in self._clients
        ]
        payload = {"type": "clients", "clients": clients}
        dead = set()
        for ws in list(self._chat_clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.add(ws)
        self._chat_clients -= dead

    async def request_tx(self, timeout: float = 30.0) -> bool:
        if not self._connected:
            await self.ensure_connected()
        self._tx_approved.clear()
        self._writer.write(b"TX0\r\n")
        await self._writer.drain()
        try:
            await asyncio.wait_for(self._tx_approved.wait(), timeout=timeout)
            self._pcm_buf = b""
            return True
        except asyncio.TimeoutError:
            log.warning("[%s] TX approve timeout (%.0fs)", self.name, timeout)
            # Send RX0 to cancel the pending TX request on the server side
            try:
                self._writer.write(b"RX0\r\n")
                await self._writer.drain()
            except Exception:
                pass
            return False

    async def send_pcm(self, pcm_chunk: bytes):
        if not self._connected or not self._writer:
            return
        self._pcm_buf += pcm_chunk
        while len(self._pcm_buf) >= PCM_PACKET_BYTES:
            packet_pcm    = self._pcm_buf[:PCM_PACKET_BYTES]
            self._pcm_buf = self._pcm_buf[PCM_PACKET_BYTES:]
            wav49 = self._encoder.encode_packet(packet_pcm)
            self._writer.write(b"TX1\r\n" + wav49)
            await self._writer.drain()

    async def end_tx(self):
        if self._connected and self._writer:
            self._writer.write(b"RX0\r\n")
            await self._writer.drain()
        self._pcm_buf = b""

    def to_dict(self) -> dict:
        return {
            "mount":      None,   # filled by TXServer
            "name":       self.name,
            "callsign":   self.callsign,
            "email":      self.email,
            "password":   self.password,
            "frn_server": self.server,
            "frn_port":   self.port,
            "connected":  self._connected,
        }


# ── Auth helpers ─────────────────────────────────────────────────────────────

_PBKDF2_ITERATIONS = 240_000   # OWASP-Empfehlung für PBKDF2-HMAC-SHA256

def hash_password(password: str) -> str:
    """Hash a password as 'pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>'."""
    salt = secrets.token_bytes(16)
    dk   = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"

def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify. Accepts new PBKDF2 hashes and legacy bare SHA-256."""
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iters_s, salt_hex, hash_hex = stored.split("$")
            dk = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters_s))
            return secrets.compare_digest(dk.hex(), hash_hex)
        except (ValueError, TypeError):
            return False
    # Legacy: ungesalzenes SHA-256 (wird bei erfolgreichem Login migriert)
    legacy = hashlib.sha256(password.encode()).hexdigest()
    return secrets.compare_digest(legacy, stored)

def needs_rehash(stored: str) -> bool:
    """True wenn der gespeicherte Hash nicht das aktuelle PBKDF2-Format hat."""
    if not stored or not stored.startswith("pbkdf2_sha256$"):
        return True
    try:
        return int(stored.split("$")[1]) < _PBKDF2_ITERATIONS
    except (ValueError, IndexError):
        return True


# ── Server ───────────────────────────────────────────────────────────────────

class TXServer:
    def __init__(self, args):
        self.args       = args
        self.cfg        = {}   # parsed config.json
        self.users: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}
        self.rooms: dict[str, FRNTXRoom] = {}
        # Persistente User-TX-Verbindungen (email+mount → FRNTXRoom)
        # bleiben zwischen PTT-Drücken am Leben → kein AL=BLOCK
        self._user_tx_conns: dict[tuple, "FRNTXRoom"] = {}
        self._users_path: Path | None = None
        self._rooms_path: Path | None = None
        self._tokens_path: Path = Path(__file__).parent / "tx_tokens.json"
        self._load_tokens()
        # Brute-Force-Schutz: Login-Fehlversuche pro Client-IP (Zeitstempel)
        self._login_fails: dict[str, list[float]] = {}
        # Auto-Antwort: Zeitpunkt des letzten Vorschlags pro Raum+User (Cooldown)
        self._auto_reply_last: dict[str, float] = {}
        # Chat-WS → Benutzername (für gezielte Auto-Antwort-Zustellung)
        self._ws_users: dict[int, str] = {}
        # KI-Funker: Transkript-Verlauf, Echo-Schutz und Belegt-Flag pro Raum
        self._room_hist: dict[str, list] = {}       # Raum → [(ts, wer, text), …]
        self._bot_last_reply: dict[str, float] = {} # Raum → Zeit letzter Bot-Sendung
        self._bot_own_tx: dict[str, list] = {}      # Raum → [(t0, t1, text), …]
        self._bot_busy: set[str] = set()
        # Debug-Ablaufverfolgung: jede gehoerte Durchsage bekommt eine "Spur"
        # mit einzelnen Schritten (Aufnahme/Whisper/Bot-Trigger/LLM/TTS/
        # Senden), je mit Status (ok/warn/error/skip) + Zeitmessung, fuers
        # Debug-Panel im Admin-Bereich.
        self._debug_traces: collections.deque = collections.deque(maxlen=80)
        self._debug_trace_by_key: dict[tuple, dict] = {}

    # ── Debug-Ablaufverfolgung ──────────────────────────────────────────────

    def _debug_trace_get(self, room: str, ts: float) -> dict:
        """Liefert die Spur fuer (Raum, Aufnahme-Zeit), legt bei Bedarf neu an.
        ts wird gerundet, damit derselbe Aufruf aus verschiedenen Schritten
        (Whisper, Bot, LLM, TTS) dieselbe Spur trifft, auch wenn er den
        Zeitstempel als float minimal anders herumreicht."""
        key = (room, round(ts, 1))
        tr = self._debug_trace_by_key.get(key)
        if tr is None:
            tr = {"room": room, "ts": ts, "started": time.time(),
                 "steps": [], "done": False, "total_s": None}
            self._debug_trace_by_key[key] = tr
            self._debug_traces.appendleft(tr)
            # Key-Dict raeumen, sonst waechst es unbegrenzt (Anzeige-Deque
            # begrenzt sich selbst per maxlen, das Zuordnungs-Dict nicht)
            while len(self._debug_trace_by_key) > 200:
                self._debug_trace_by_key.pop(next(iter(self._debug_trace_by_key)), None)
        return tr

    def debug_trace_step(self, room: str, ts: float, name: str, status: str,
                         duration_s: float | None = None, detail: str = "",
                         final: bool = False, audio: str = ""):
        """Traegt einen Schritt in die Spur der Durchsage (room, ts) ein.
        status: "ok" (gruen) | "warn" (gelb) | "error" (rot) | "skip" (grau).
        final=True markiert die Spur als abgeschlossen (Gesamtzeit wird
        berechnet, keine weiteren Schritte werden erwartet).
        audio: Dateiname der zugehoerigen WAV-Aufnahme (nur Name, kein Pfad)
        -- macht den Schritt im Debug-Panel per Klick abspielbar."""
        try:
            tr = self._debug_trace_get(room, ts)
            tr["steps"].append({
                "name": name, "status": status,
                "duration_s": round(duration_s, 2) if duration_s is not None else None,
                "detail": (detail or "")[:400],
                "at": time.time(),
                "audio": Path(audio).name if audio else "",
            })
            if final:
                tr["done"] = True
                tr["total_s"] = round(time.time() - tr["started"], 2)
        except Exception as e:
            log.debug("debug_trace_step fehlgeschlagen: %s", e)

    # ── Token Persistenz ───────────────────────────────────────────────────

    def _load_tokens(self):
        """Lädt gespeicherte Tokens (überleben Server-Neustart)."""
        try:
            if self._tokens_path.exists():
                data = json.loads(self._tokens_path.read_text())
                now  = time.time()
                self.tokens = {
                    t: v for t, v in data.items()
                    if v.get("expires", 0) > now
                }
                log.info("Tokens geladen: %d aktive", len(self.tokens))
        except Exception as e:
            log.warning("Token-Load fehlgeschlagen: %s", e)

    def _save_tokens(self):
        """Speichert aktive Tokens auf Disk (FRN-Passwörter werden nicht gespeichert)."""
        try:
            safe = {t: {k: v for k, v in d.items() if k != "frn_password"}
                    for t, d in self.tokens.items()}
            self._tokens_path.write_text(
                json.dumps(safe, indent=2), encoding="utf-8"
            )
            self._tokens_path.chmod(0o600)   # Session-Tokens: nicht world-readable
        except Exception as e:
            log.warning("Token-Save fehlgeschlagen: %s", e)

    # ── Config loading ─────────────────────────────────────────────────────

    def load_config(self):
        path = Path(self.args.config) if self.args.config else None
        if path and path.exists():
            with open(path) as f:
                self.cfg = json.load(f)
            log.info("Config loaded from %s", path)
        # CLI args override config.json; config.json overrides defaults
        frn_cfg = self.cfg.get("frn", {})
        if not self.args.frn_server or self.args.frn_server == "localhost":
            self.args.frn_server = frn_cfg.get("server", self.args.frn_server)
        if self.args.frn_port == 10024:
            self.args.frn_port = frn_cfg.get("port", self.args.frn_port)
        tx_cfg = self.cfg.get("tx_server", {})
        if self.args.port == 8765:
            self.args.port = tx_cfg.get("port", self.args.port)
        if self.args.host == "0.0.0.0":
            self.args.host = tx_cfg.get("host", self.args.host)

    def load_users(self):
        path = Path(self.args.users)
        self._users_path = path
        if not path.exists():
            log.warning("Users file not found: %s", path)
            return
        with open(path) as f:
            data = json.load(f)
        for u in data.get("users", []):
            entry = {
                "callsign":     u.get("callsign", u["username"].upper()),
                "is_admin":     u.get("is_admin", False),
                "default_room": u.get("default_room", ""),
                "frn_only":     u.get("frn_only", False),
            }
            if not entry["frn_only"]:
                entry["password_hash"] = u["password_hash"]
            if u.get("voice"):
                entry["voice"] = u["voice"]
            # E-Mail-Adressen case-insensitiv behandeln (sonst Duplikate Jkuphal/jkuphal)
            self.users[u["username"].lower()] = entry
        log.info("Loaded %d users", len(self.users))

    async def _handle_frn_command(self, sender: str, text: str, room_name: str):
        """Process !web commands received as FRN text messages."""
        if not text.strip().lower().startswith("!web"):
            return
        sender_is_admin = any(
            u.get("callsign", "").upper() == sender.upper() and u.get("is_admin")
            for u in self.users.values()
        )
        if not sender_is_admin:
            log.info("[FRN-CMD] %s tried !web but is not admin", sender)
            return

        room   = self.rooms.get(room_name)
        parts  = text.split()
        cmd    = parts[1].lower() if len(parts) > 1 else "help"
        log.info("[FRN-CMD] admin %s in '%s': %s", sender, room_name, text.strip())

        async def reply(msg):
            if room:
                await room.send_text(msg)

        if cmd == "help":
            await reply("Befehle: help | users | adduser <email> <cs> | deluser <email> | rooms | status")
        elif cmd == "users":
            entries = [f"{u}({v.get('callsign','?')})" + ("[A]" if v.get("is_admin") else "")
                       for u, v in self.users.items()]
            await reply("User: " + (", ".join(entries) if entries else "—"))
        elif cmd == "adduser":
            if len(parts) < 4:
                await reply("Verwendung: !web adduser <email> <callsign>")
                return
            email, callsign = parts[2].lower(), parts[3]
            if email in self.users:
                await reply(f"{email} existiert bereits.")
                return
            self.users[email] = {"callsign": callsign, "is_admin": False,
                                 "frn_only": True, "default_room": ""}
            self._save_users()
            await reply(f"Benutzer {email} ({callsign}) angelegt.")
        elif cmd == "deluser":
            if len(parts) < 3:
                await reply("Verwendung: !web deluser <email>")
                return
            email = parts[2].lower()
            if email not in self.users:
                await reply(f"{email} nicht gefunden.")
                return
            del self.users[email]
            self._save_users()
            await reply(f"Benutzer {email} gelöscht.")
        elif cmd == "rooms":
            entries = [f"{m}={r.name}" for m, r in self.rooms.items()]
            await reply("Räume: " + (", ".join(entries) if entries else "—"))
        elif cmd == "status":
            entries = [f"{r.name}:{'OK' if r._connected else 'OFFLINE'}"
                       for r in self.rooms.values()]
            await reply(", ".join(entries) if entries else "keine Räume")
        else:
            await reply(f"Unbekannt: '{cmd}'. Tippe: !web help")

    def _set_room_callback(self, room: "FRNTXRoom"):
        room.on_message = self._handle_frn_command

    def load_rooms(self):
        path = Path(self.args.rooms)
        self._rooms_path = path
        if not path.exists():
            log.warning("Rooms file not found: %s", path)
            return
        with open(path) as f:
            data = json.load(f)
        for r in data.get("rooms", []):
            room = FRNTXRoom(
                name       = r["name"],
                frn_server = r.get("frn_server", self.args.frn_server),
                frn_port   = r.get("frn_port",   self.args.frn_port),
                email      = r["email"],
                password   = r["password"],
                callsign   = r["callsign"],
            )
            self._set_room_callback(room)
            self.rooms[r["mount"]] = room
        log.info("Configured %d rooms: %s", len(self.rooms), list(self.rooms))

    def _save_users(self):
        if not self._users_path:
            return
        rows = []
        for uname, info in self.users.items():
            row = {
                "username":     uname,
                "callsign":     info["callsign"],
                "is_admin":     info.get("is_admin", False),
                "default_room": info.get("default_room", ""),
                "frn_only":     info.get("frn_only", False),
            }
            if not info.get("frn_only"):
                row["password_hash"] = info["password_hash"]
            if info.get("voice"):
                row["voice"] = info["voice"]
            rows.append(row)
        data = {"users": rows}
        with open(self._users_path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        Path(self._users_path).chmod(0o600)   # Passwort-Hashes: nicht world-readable

    def _save_rooms(self):
        if not self._rooms_path:
            return
        data = {"rooms": [
            {
                "mount":      mount,
                "name":       r.name,
                "callsign":   r.callsign,
                "email":      r.email,
                "password":   r.password,
                "frn_server": r.server,
                "frn_port":   r.port,
            }
            for mount, r in self.rooms.items()
        ]}
        with open(self._rooms_path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")

    # ── Token management ───────────────────────────────────────────────────

    TOKEN_LIFETIME = 86400   # 24 Stunden

    def _token_for(self, username: str) -> str:
        token = secrets.token_hex(24)
        u = self.users[username]
        self.tokens[token] = {
            "user":     username,
            "callsign": u["callsign"],
            "is_admin": u.get("is_admin", False),
            "expires":  time.time() + self.TOKEN_LIFETIME,
        }
        self._save_tokens()
        return token

    # ── Clips ──────────────────────────────────────────────────────────────

    _CLIPS_DIR = Path(__file__).parent / "clips"

    def _load_clips(self) -> list[dict]:
        """Load clip list from config.ini [clips] section."""
        import configparser
        ini = configparser.ConfigParser()
        ini_path = Path(__file__).parent / "config.ini"
        if not ini_path.exists():
            return []
        ini.read(ini_path, encoding="utf-8")
        if not ini.has_section("clips"):
            return []
        clips = []
        for clip_id, value in ini["clips"].items():
            if "|" in value:
                label, text = value.split("|", 1)
            else:
                label = text = value
            label = label.strip()
            text  = text.strip()
            if label and text:
                has_rec = (self._CLIPS_DIR / f"{clip_id}.wav").exists()
                clips.append({"id": clip_id, "label": label,
                               "text": text, "has_recording": has_rec})
        return clips

    async def _get_clip_pcm(self, clip_id: str, clip_text: str,
                             lang: str = "de") -> bytes:
        """Return 8 kHz mono s16le PCM: prefers recorded file, falls back to TTS."""
        recorded = self._CLIPS_DIR / f"{clip_id}.wav"
        if recorded.exists():
            ffm = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", str(recorded),
                "-ar", "8000", "-ac", "1", "-f", "s16le", "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            pcm, _ = await ffm.communicate()
            return pcm
        return await self._gen_espeak_pcm(clip_text, lang)

    @staticmethod
    async def _gen_espeak_pcm(text: str, lang: str = "de") -> bytes:
        """Generate 8 kHz mono s16le PCM from text via espeak-ng + ffmpeg."""
        esp = await asyncio.create_subprocess_exec(
            "espeak-ng", "-v", lang, "-s", "145", "-a", "180", "--stdout", text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        wav_data, _ = await esp.communicate()
        ffm = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", "pipe:0",
            "-ar", "8000", "-ac", "1", "-f", "s16le", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        pcm, _ = await ffm.communicate(input=wav_data)
        return pcm

    @staticmethod
    def _speaker_id(username: str) -> str:
        """Benutzername → Sprecher-ID für den Voice-Server (a-z0-9_-)."""
        return re.sub(r"[^a-z0-9_\-]", "_", username.lower())[:40]

    def _user_speaker(self, username: str) -> str:
        """Sprecher-ID des Users, wenn er ein eigenes Sample hat, sonst default."""
        info = self.users.get((username or "").lower())
        if info and info.get("voice", {}).get("sample"):
            return self._speaker_id(username)
        return "default"

    _GTTS_MAX_CHARS = 180  # Sicherheitsmarge unter dem ~200-Zeichen-Limit des
                           # inoffiziellen Google-Translate-TTS-Endpunkts

    @classmethod
    def _split_for_gtts(cls, text: str) -> list[str]:
        """Text in Stücke <= _GTTS_MAX_CHARS teilen, bevorzugt an Satzgrenzen
        (nur bei zu langen Einzelsätzen zusätzlich an Wortgrenzen)."""
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        chunks: list[str] = []
        cur = ""

        def _flush():
            nonlocal cur
            if cur:
                chunks.append(cur)
                cur = ""

        for s in sentences:
            s = s.strip()
            if not s:
                continue
            if len(s) > cls._GTTS_MAX_CHARS:
                _flush()
                piece = ""
                for w in s.split():
                    if piece and len(piece) + len(w) + 1 > cls._GTTS_MAX_CHARS:
                        chunks.append(piece)
                        piece = w
                    else:
                        piece = f"{piece} {w}".strip()
                if piece:
                    chunks.append(piece)
                continue
            if cur and len(cur) + len(s) + 1 > cls._GTTS_MAX_CHARS:
                _flush()
            cur = f"{cur} {s}".strip()
        _flush()
        return chunks or [text[:cls._GTTS_MAX_CHARS]]

    async def _google_tts_pcm(self, text: str, lang: str = "de") -> bytes:
        """Sprachausgabe über den kostenlosen, inoffiziellen Google-Translate-
        TTS-Endpunkt (derselbe Trick wie Home Assistants google_translate-
        Plattform: kein API-Key, kein Google-Cloud-Konto). Sehr schnell
        (~0.2-0.4s), aber: feste Google-Standardstimme statt Klon/Charakter,
        und ein Längenlimit pro Anfrage -- längere Texte werden je Satz in
        Stücke geteilt, einzeln geholt+dekodiert und als PCM aneinandergehängt
        (Roh-PCM-Konkatenation ist unproblematisch, anders als MP3-Frames)."""
        import urllib.parse
        chunks  = self._split_for_gtts(text)
        timeout = aiohttp.ClientTimeout(total=15)
        pcm_parts = []
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            for chunk in chunks:
                q   = urllib.parse.quote(chunk)
                url = ("https://translate.google.com/translate_tts?"
                       f"ie=UTF-8&client=tw-ob&tl={lang}&q={q}")
                async with sess.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"Google-TTS {resp.status}")
                    mp3_bytes = await resp.read()
                ffm = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-i", "pipe:0",
                    "-af", "loudnorm=I=-16:TP=-1.5",
                    "-ar", "8000", "-ac", "1", "-f", "s16le", "pipe:1",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                pcm, _ = await ffm.communicate(input=mp3_bytes)
                pcm_parts.append(pcm)
        return b"".join(pcm_parts)

    async def _get_voice_pcm(self, text: str, lang: str = "de",
                             speaker: str = "default",
                             force_xtts: bool = False) -> bytes:
        """Liefert 8 kHz mono s16le PCM für die Sprachausgabe. Engine laut
        voice.tts_engine: "google" (kostenlos, schnell, feste Stimme) oder
        piper/xtts (eigene Stimme/Charakter, über voice.remote_url).

        force_xtts=True erzwingt XTTS über voice.xtts_url, unabhängig vom
        gerade in der Verwaltung ausgewählten Engine fuer Robert (Piper/XTTS/
        Google). Noetig fuer alles rund um geklonte PERSOENLICHE Stimmen
        (Automatik-Antwort in eigener Stimme, SPEAK_VOICE, Stimm-Sample
        hochladen/Hoerprobe) -- Piper und Google kennen keine hochgeladenen
        Referenz-Stimmen, nur XTTS kann das. Robert selbst (KI-Funker) nutzt
        weiterhin den frei waehlbaren Standard-Weg (remote_url)."""
        vcfg = self.cfg.get("voice", {})
        if not vcfg.get("enabled", False):
            raise RuntimeError("Voice-Funktion ist deaktiviert")
        if force_xtts:
            url = (vcfg.get("xtts_url") or "").strip()
        else:
            if (vcfg.get("tts_engine") or "").strip().lower() == "google":
                return await self._google_tts_pcm(text, lang)
            url = (vcfg.get("remote_url") or "").strip()
        if not url:
            raise RuntimeError("Voice-Funktion ist deaktiviert")
        payload = {"text": text, "language": lang, "speaker": speaker or "default"}
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"TTS-Dienst {resp.status}: {body[:120]}")
                wav_bytes = await resp.read()
        ffm = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", "pipe:0",
            # Lautheits-Normalisierung (EBU R128) -- verschiedene TTS-Engines/
            # Modelle (Piper "high" leiser als "medium", XTTS anders als
            # Piper) hatten spuerbar unterschiedliche Pegel. Gilt fuer
            # piper/xtts (die Google-Engine hat ihre eigene, s.o.).
            "-af", "loudnorm=I=-16:TP=-1.5",
            "-ar", "8000", "-ac", "1", "-f", "s16le", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        pcm, _ = await ffm.communicate(input=wav_bytes)
        return pcm

    # ── Auto-Antwort bei Namensnennung ─────────────────────────────────────

    async def on_transcript(self, room_name: str, callsign: str,
                            text: str, ts: float):
        """Von der Transkriptions-Pipeline nach jedem Transkript gerufen.

        Prüft die Automatik jedes Benutzers (eigene Trigger-Wörter, eigene
        Persona, eigene Stimme). Existiert keine Benutzer-Konfiguration,
        greift die globale auto_reply-Konfiguration (Alt-Verhalten).
        """
        try:
            ar_g = self.cfg.get("voice", {}).get("auto_reply", {})
            low = text.lower()
            # Eigene Sendungen/Parrot nicht beantworten (sonst Endlosschleife)
            cs = (callsign or "").lower()
            for pref in ar_g.get("ignore_callsigns",
                                 ["tx-", "stream-", "web-", "audio test"]):
                if cs.startswith(pref.lower()):
                    return
            room = next((r for r in self.rooms.values()
                         if r.name == room_name), None)
            if room is None:
                return

            # KI-Funker beobachtet jeden Durchgang (unabhängig von der
            # Namens-Automatik) und antwortet ggf. selbstständig.
            try:
                self._bot_observe(room, room_name, callsign, text, ts, low)
            except Exception as e:
                log.warning("KI-Funker: %s", e)

            if not ar_g.get("enabled", False):
                return  # Hauptschalter der Namens-Automatik

            # Kandidaten: Benutzer mit eigener Automatik, deren Wörter passen
            candidates: list[tuple[str | None, dict]] = []
            for uname, info in self.users.items():
                uar = info.get("voice", {}).get("auto_reply", {})
                if not uar.get("enabled"):
                    continue
                names = [n.lower() for n in uar.get("names", []) if n.strip()]
                if names and any(n in low for n in names):
                    candidates.append((uname, uar))
            # Fallback: globale Konfiguration (kein Benutzer hat gepasst)
            if not candidates:
                g_names = [n.lower() for n in ar_g.get("names", [])]
                if g_names and any(n in low for n in g_names):
                    candidates.append((None, ar_g))

            cooldown = float(ar_g.get("cooldown_s", 90))
            now = time.time()
            for uname, uar in candidates:
                key = f"{room_name}:{uname or '_global'}"
                if now - self._auto_reply_last.get(key, 0.0) < cooldown:
                    continue
                self._auto_reply_last[key] = now
                asyncio.create_task(self._handle_auto_reply(
                    room, room_name, uname, uar, text, callsign))
        except Exception as e:
            log.warning("Auto-Antwort-Fehler: %s", e)

    async def _handle_auto_reply(self, room: "FRNTXRoom", room_name: str,
                                 uname: str | None, uar: dict,
                                 heard: str, from_cs: str):
        """Vorschlag generieren und (je nach auto_send) senden oder vorschlagen.

        uname=None = globale Fallback-Konfiguration (Standard-Stimme).
        """
        try:
            ar_g    = self.cfg.get("voice", {}).get("auto_reply", {})
            persona = (uar.get("persona") or ar_g.get("persona") or "").strip()
            suggestion = await self._ollama_suggest(heard, persona)
            if not suggestion:
                return

            speaker   = self._user_speaker(uname) if uname else "default"
            auto_send = bool(uar.get("auto_send"))
            # Ohne eigenes Stimm-Sample darf nur ein Admin die Standard-Stimme
            # automatisch senden (die Standard-Stimme gehört dem Betreiber).
            if auto_send and speaker == "default" and uname is not None:
                if not self.users.get(uname, {}).get("is_admin"):
                    auto_send = False

            if auto_send:
                log.info("[%s] Auto-SENDEN für %s (gehört: %.50s): %.70s",
                         room_name, uname or "global", heard, suggestion)
                sent = await self._auto_send_voice(room, suggestion, speaker,
                                                   force_xtts=True)
                payload = {"type": "voice_autosent", "room": room_name,
                           "from": from_cs or "?", "heard": heard,
                           "text": suggestion, "ok": sent}
            else:
                log.info("[%s] Auto-Vorschlag für %s (gehört: %.50s): %.70s",
                         room_name, uname or "alle", heard, suggestion)
                payload = {"type": "voice_suggest", "room": room_name,
                           "from": from_cs or "?", "heard": heard,
                           "suggestion": suggestion}

            # Zustellung: gezielt an die Sessions des Benutzers, sonst an alle
            dead = set()
            for ws in list(room._chat_clients):
                if uname and self._ws_users.get(id(ws)) != uname:
                    continue
                try:
                    await ws.send_json(payload)
                except Exception:
                    dead.add(ws)
            room._chat_clients -= dead
        except Exception as e:
            log.warning("[%s] Auto-Antwort (%s): %s", room_name, uname, e)

    async def _auto_send_voice(self, room: "FRNTXRoom", text: str,
                               speaker: str = "default",
                               force_xtts: bool = False,
                               on_tx_start=None) -> bool:
        """Synthetisiert Text und sendet ihn direkt in den Raum (ohne WS-Client).

        Gleicher Ablauf wie SPEAK_VOICE: erst Stimme erzeugen, dann Sender
        tasten (kein toter Träger), Echtzeit-Sendeloop, sauberes end_tx.
        on_tx_start: optionaler Callback, wird genau beim Tasten des Senders
        aufgerufen (Beginn der eigentlichen Übertragung, nach der TTS-Synthese)
        -- fürs Debug-Panel, damit neben "fertig" auch "Sendebeginn" sichtbar
        ist. Kein Effekt auf andere Aufrufer (Default None = kein Callback).
        """
        try:
            vcfg = self.cfg.get("voice", {})
            pcm  = await self._get_voice_pcm(text, vcfg.get("language", "de"),
                                             speaker, force_xtts=force_xtts)
        except Exception as e:
            log.warning("[%s] Auto-Senden: TTS fehlgeschlagen: %s", room.name, e)
            return False
        try:
            async with room._tx_lock:
                await room.ensure_connected()
                ok = await room.request_tx(timeout=10.0)
                if not ok:
                    log.info("[%s] Auto-Senden: TX nicht genehmigt (Kanal belegt)",
                             room.name)
                    return False
                if on_tx_start:
                    on_tx_start()
                try:
                    for i in range(0, len(pcm), PCM_PACKET_BYTES):
                        await room.send_pcm(pcm[i:i + PCM_PACKET_BYTES])
                        await asyncio.sleep(PCM_PACKET_BYTES / (8000 * 2))
                finally:
                    await room.end_tx()
            return True
        except Exception as e:
            log.warning("[%s] Auto-Senden fehlgeschlagen: %s", room.name, e)
            return False

    async def _ollama_suggest(self, heard: str, persona: str = "") -> str:
        """Kurzen Antwortvorschlag von Ollama holen (keep_alive=0 → GPU
        wird nach der Generierung sofort wieder freigegeben)."""
        ar    = self.cfg.get("voice", {}).get("auto_reply", {})
        url   = (ar.get("ollama_url", "http://192.0.0.17:11434")).rstrip("/")
        model = ar.get("ollama_model", "llama3.2:3B")
        persona = (persona or ar.get("persona") or
                   "Du bist Jörg, ein CB-Funker aus Eickelborn (Kanal 74).").strip()
        prompt = (
            f"{persona}\n"
            f'Im Funk wurde gerade gesagt: "{heard}"\n'
            "Du wurdest angesprochen. Antworte kurz und locker in "
            "1-2 Sätzen, wie man im CB-Funk spricht. Immer Du-Form, nie Sie. "
            "Erfinde keine Details (keine Kanalnummern, Namen oder Orte, die "
            "nicht genannt wurden). "
            "Nur die Antwort selbst, keine Anführungszeichen, keine Erklärungen."
        )
        body = {"model": model, "prompt": prompt, "stream": False,
                "keep_alive": 0,
                "options": {"num_predict": 60, "temperature": 0.7}}
        hdrs = self._ollama_headers(ar.get("ollama_token"))
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(f"{url}/api/generate", json=body,
                                     headers=hdrs) as resp:
                    if resp.status != 200:
                        log.warning("Ollama HTTP %d: %s", resp.status,
                                    (await resp.text())[:120])
                        return ""
                    data = await resp.json()
            return (data.get("response") or "").strip().strip('"')
        except Exception as e:
            log.warning("Ollama nicht erreichbar (%s): %s", url, e)
            return ""

    # ── KI-Funker (autonomer Gesprächspartner) ─────────────────────────────

    # Platzhalter, den die UI statt des echten Tokens sieht/zurücksendet
    _TOKEN_MASK = "••••••••"

    # Standard-System-Anweisung für den KI-Funker. In der config unter
    # voice.bot.system_prompt überschreibbar. Platzhalter {name}/{persona}.
    # Der SKIP-Mechanismus (nur mit dem Wort SKIP antworten = schweigen) wird
    # vom Code ausgewertet und sollte in einer eigenen Anweisung erhalten
    # bleiben, sonst funkt der Bot in jeden Durchgang.
    _BOT_SYSTEM_DEFAULT = (
        "{persona}\n"
        "Du heißt {name} und funkst im lockeren CB-Jedermannfunk mit. Die "
        "Nachrichten sind mitgehörte Funksprüche im Format 'Rufzeichen: Text'; "
        "deine eigenen früheren Sendungen erscheinen als deine Nachrichten.\n"
        "Entscheide beim letzten Spruch, ob DU gemeint bist: dein Name {name} "
        "fällt, jemand fragt allgemein ob wer da/zu hören ist, jemand macht "
        "einen allgemeinen Anruf, oder ein Gespräch mit dir geht weiter. Reden "
        "zwei andere miteinander oder wird eine andere Station gerufen, bist "
        "du NICHT gemeint.\n"
        "Bist du nicht gemeint, antworte nur mit dem Wort: SKIP\n"
        "Bist du gemeint, antworte kurz und locker in 1-2 Sätzen, Du-Form. "
        "Erfinde nichts, was du nicht wissen kannst (du hast keine Sensoren). "
        "Gib nur den gesprochenen Text aus, ohne Rufzeichen-Präfix, "
        "Anführungszeichen oder Emojis (er wird vorgelesen)."
    )

    _BOT_DEFAULTS = {
        "enabled":  False,
        "name":     "Robert",
        "trigger":  ["robert", "roboter", "funk-roboter"],
        "speaker":  "damien_black",
        "persona":  ("Du bist Robert, ein freundlicher Funk-Roboter mit "
                     "künstlicher Intelligenz auf einem CB-Funk-Kanal in "
                     "Eickelborn. Du bist rund um die Uhr QRV und plauderst "
                     "gern kurz über Funk, Technik und das Wetter."),
        "cooldown_s": 20,
        "conversation_window_s": 180,
        "history_len": 10,
        # Ollama-Kontextfenster (num_ctx) -- muss zu dem passen, mit dem
        # andere Clients (z.B. Open WebUI) dasselbe Modell laden, sonst
        # erzwingt jede Abweichung einen kompletten Neu-Load (~10-15s),
        # siehe _llm_ollama. Konfigurierbar, da der Wert bei Bedarf noch
        # nach oben angepasst wird.
        "ollama_num_ctx": 97280,
        # Personen-Gedaechtnis: manuell gepflegte Notizen pro Name, siehe
        # _bot_build_prompt. Liste aus {"name": ..., "notes": ...}.
        "memory": [],
        # Notizbuch: Robert merkt sich SELBST Dinge per Tool-Call (siehe
        # _BOT_NOTE_TOOL/_bot_save_note), im Unterschied zum Personen-
        # Gedaechtnis oben (das pflegt der Admin manuell). Liste aus
        # {"ts": ..., "text": ...}, aelteste zuerst raus (siehe _bot_save_note).
        "notebook_enabled": False,
        "notebook": [],
        # System-Anweisung fürs Modell (leer = _BOT_SYSTEM_DEFAULT).
        # Platzhalter {name} und {persona} werden eingesetzt.
        "system_prompt": "",
        # Anbieter des Sprachmodells: "ollama" (lokal/eigener Server) oder
        # "gemini" (Google-Cloud, schneller). Bei gemini gelten gemini_*.
        "provider": "ollama",
        "gemini_api_key": "",
        "gemini_model": "gemini-flash-lite-latest",
        "ollama_url": "",
        "ollama_model": "qwen3:14b",
        # 0 = Modell nach jeder Antwort entladen (geteilte GPU), "2h" o.Ä. =
        # im RAM halten (eigener CPU-Server, spart den ~1 min Erst-Load)
        "ollama_keep_alive": 0,
        # Optionaler Zugangs-Token, wenn der Ollama-Server hinter einem
        # Passwort-Proxy steht (Basic-Auth, Benutzer "frn"). Leer = ohne.
        "ollama_token": "",
        "rooms": [],
        # Websuche: "Robert, such mal nach ..." / "Robert, google mal ..."
        # fragt die lokale SearXNG-Instanz ab, Treffer landen als Zusatz-
        # Kontext im System-Prompt (siehe _bot_websearch). Kostenlos, da
        # selbst gehostet — kein Cloud-Suchdienst mit Zusatzkosten.
        "websearch": {
            "enabled": False,
            "searxng_url": "http://127.0.0.1:8075/search",
            "max_results": 3,
        },
        # Sprachsteuerung über Funk: "<Name> aus" macht stumm, "<Name> start"
        # weckt wieder. Greift auch bei deaktiviertem Bot (sonst kein Wecken).
        "control": {
            "enabled": True,
            "off": ["aus", "sendepause", "schlafen", "ruhe", "funkstille"],
            "on":  ["start", "an", "aufwachen", "wach auf", "weiter"],
            "confirm": True,
            "reply_off": "Alles klar, ich halt mich raus. Meldet euch, wenn ihr mich braucht.",
            "reply_on":  "Bin wieder da. Was gibt es?",
        },
    }

    # Allgemeine Anrufe, auf die der Bot auch ohne Namensnennung reagiert.
    # Begrüßungen NUR wenn erkennbar an die Runde gerichtet (an alle/zusammen/
    # miteinander/die Runde) -- "Guten Morgen Gottfried" (an eine bestimmte
    # Person) oder ein blankes "Guten Morgen." sollen NICHT triggern, sonst
    # antwortet Robert auf jeden beliebigen Gruß im Kanal.
    _BOT_CALL_RE = re.compile(
        r"\bqrv\b|\bcq\w*\b"  # cq, cqcq, cqcqr, cqde... (allgemeiner Anruf,
                              # Whisper schreibt "CQ CQ" oft als ein Wort)
        r"|jemand\s+(da|dran|drauf|erreichbar|zu\s*h[öo]ren"
        r"|auf\s+dem\s+kanal|auf\s+der\s+frequenz)"
        r"|h[öo]rt\s+(mich|da|hier)\s+(irgend)?(jemand|einer|wer)\b"
        r"|(ist|is)\s+(da|hier)\s+((irgend)?jemand|einer|wer)\b"
        r"|(ist|is)\s+(einer|wer|jemand)\s+(da|hier)\b"
        r"|(einer|wer|keiner|niemand)\s+(da|hier|qrv|auf\s+dem\s+kanal)"
        r"|(guten\s+(morgen|tag|abend)|moin\s*moin|moin|hallo)\s+"
        r"(an\s+alle|zusammen|alle|die\s+runde|in\s+die\s+runde|miteinander)\b")

    # Auslöser für die Websuche, z.B. "Robert, such mal nach dem Wetter" oder
    # "google mal die Höhe vom Fernsehturm". "nach" ist bei such/durchsuch/
    # recherchier PFLICHT, um Alltagssätze wie "ich such noch meine Antenne"
    # nicht fälschlich als Suchauftrag zu werten.
    _BOT_SEARCH_RE = re.compile(
        r"(?:goo?g(?:le)?|gugl\w*)\s+(?:mal\s+)?(?:bitte\s+)?(.+)"
        r"|(?:such(?:e|st)?|durchsuch\w*|recherchier\w*)"
        r"\s+(?:mal\s+)?(?:bitte\s+)?nach\s+(.+)"
        r"|nach\s+(.+?)\s+(?:mal\s+)?such\w*\b",
        re.IGNORECASE)

    # Echtes Ollama-Tool-Calling als Ergaenzung zum Regex-Trigger oben: der
    # Regex fängt nur explizite Befehle ("such mal nach X"), das Modell kann
    # damit zusaetzlich SELBST erkennen, wenn es ein erwaehntes Thema nicht
    # kennt, und von sich aus nachschlagen -- bestaetigt getestet (Modell hat
    # "tools"-Capability laut /api/show, reagiert mit sauberem tool_calls
    # statt zu halluzinieren). Nur ~60 Prompt-Tokens Mehraufwand pro Anfrage.
    _BOT_WEBSEARCH_TOOL = [{
        "type": "function",
        "function": {
            "name": "websearch",
            "description": ("Durchsucht das Internet nach aktuellen Informationen zu "
                            "einem Thema/Begriff, den du nicht kennst oder wo du dir "
                            "unsicher bist."),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Suchbegriff"}},
                "required": ["query"],
            },
        },
    }]

    # Notizbuch-Tool: Robert kann sich per Tool-Call SELBST etwas dauerhaft
    # merken (im Unterschied zum Personen-Gedaechtnis, das der Admin manuell
    # pflegt). Bewusst als eigenstaendiges Tool statt automatisch aus jeder
    # Antwort abzuleiten -- das Modell entscheidet selbst, was wirklich
    # merkenswert ist, sonst wuerde bei jedem Smalltalk etwas gespeichert.
    _BOT_NOTE_TOOL = [{
        "type": "function",
        "function": {
            "name": "notiz_merken",
            "description": ("Speichert eine kurze Notiz dauerhaft in deinem eigenen "
                            "Notizbuch, die du dir fuer spaetere Gespraeche merken "
                            "willst (z.B. wiederkehrende Themen auf dem Kanal, "
                            "Ereignisse, Dinge die jemand erzaehlt hat). Nutze das "
                            "sparsam -- nur fuer wirklich merkenswerte Dinge, nicht "
                            "fuer jeden Smalltalk."),
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string",
                                        "description": "Kurze Notiz (1 Satz)"}},
                "required": ["text"],
            },
        },
    }]

    def _bot_cfg(self) -> dict:
        return {**self._BOT_DEFAULTS,
                **self.cfg.get("voice", {}).get("bot", {})}

    def _bot_is_own(self, room_name: str, text: str, ts: float) -> bool:
        """Eigene Sendung im Transkript wiedererkennen (Echo-Schutz):
        Aufnahmezeit fällt in ein eigenes Sendefenster ODER der Text ähnelt
        stark dem zuletzt selbst Gesagten."""
        low = text.lower()
        for t0, t1, sent in self._bot_own_tx.get(room_name, []):
            if t0 - 3.0 <= ts <= t1 + 3.0:
                return True
            if sent and difflib.SequenceMatcher(
                    None, low, sent.lower()).ratio() > 0.7:
                return True
        return False

    def resolve_known_text(self, room: str, ts: float) -> str | None:
        """Liefert den bereits bekannten Text einer eigenen Bot-Sendung, wenn
        die Aufnahmezeit in ein eigenes Sendefenster fällt — spart die Re-
        Transkription von Roberts eigener synthetisierter Stimme (siehe
        process_wav in frn_transcription.py). Nutzt NUR den Zeitfenster-
        Check (nicht den difflib-Textvergleich aus _bot_is_own, da hier noch
        kein Transkript vorliegt, das verglichen werden könnte)."""
        for t0, t1, sent in self._bot_own_tx.get(room, []):
            if t0 - 3.0 <= ts <= t1 + 3.0:
                return sent
        return None

    def bot_archive_callsign(self, room: str, callsign: str, ts: float,
                             text: str) -> str:
        """Eigene Bot-Sendungen in Log + Archiv unter dem Bot-Namen führen.

        Greift nur, wenn die Aufnahme kein Rufzeichen hat (Normalfall, die
        Sprecher-Zuordnung ist deaktiviert)."""
        if not callsign and self._bot_is_own(room, text, ts):
            return self._bot_cfg().get("name") or "Robert"
        return callsign

    # Kurze, alltagssprachlich mehrdeutige Kommandowörter brauchen ein enges
    # Abstandsfenster zum Namen -- "aus" kollidiert sonst mit dem trennbaren
    # Verb "aussehen" ("Robert, er sieht gut aus." löste faelschlich den
    # Aus-Befehl aus, weil "aus" nur 15 Zeichen von "Robert" entfernt stand).
    # Längere, eindeutigere Wörter (sendepause, schlafen, ...) behalten das
    # weite Fenster, da sie kaum in normalen Sätzen zufällig auftauchen.
    _BOT_CMD_TIGHT_GAP = {"aus": 8, "an": 8}
    _BOT_CMD_DEFAULT_GAP = 18

    # Sicherheitsnetz gegen Emojis in Roberts Antworten -- der Prompt verbietet
    # sie explizit ("wird vorgelesen"), aber manche Modelle haengen trotzdem
    # gelegentlich welche an (aehnlich dem "Robert: "-Praefix-Problem).
    _EMOJI_RE = re.compile(
        "["
        "\U0001F300-\U0001FAFF"  # Piktogramme, Emoticons, Transport, Symbole
        "\U00002600-\U000027BF"  # sonstige Symbole, Dingbats
        "\U0001F1E6-\U0001F1FF"  # Flaggen (Regional Indicators)
        "\U00002B00-\U00002BFF"  # weitere Symbole/Pfeile
        "\U0000FE0F"             # Variationsselektor
        "\U0000200D"             # Zero-Width-Joiner (verbindet Emoji-Sequenzen)
        "]+", flags=re.UNICODE)

    def _bot_command(self, bot: dict, name: str, low: str) -> str | None:
        """Erkennt Sprach-Steuerbefehle: '<Name> aus' → 'off', '<Name> start'
        → 'on'. Name und Kommandowort müssen nah beieinander stehen (gegen
        Fehlauslöser in normalen Sätzen). None = kein Kommando."""
        ctl = bot.get("control") or {}
        if not ctl.get("enabled", True):
            return None
        nm = re.escape((name or "robert").lower())
        if not re.search(rf"\b{nm}\b", low):
            return None

        def _hit(words) -> bool:
            for w in words:
                w_raw = (w or "").strip().lower()
                if not w_raw:
                    continue
                gap = self._BOT_CMD_TIGHT_GAP.get(w_raw, self._BOT_CMD_DEFAULT_GAP)
                w = re.escape(w_raw)
                # Name … Kommandowort ODER Kommandowort … Name, max `gap` Zeichen
                if re.search(rf"\b{nm}\b.{{0,{gap}}}?\b{w}\b", low) or \
                   re.search(rf"\b{w}\b.{{0,{gap}}}?\b{nm}\b", low):
                    return True
            return False

        if _hit(ctl.get("off", [])):
            return "off"
        if _hit(ctl.get("on", [])):
            return "on"
        return None

    def _bot_set_enabled(self, value: bool):
        """Bot ein-/ausschalten und in config.json persistieren (überlebt
        Neustart)."""
        self.cfg.setdefault("voice", {}).setdefault("bot", {})["enabled"] = bool(value)
        try:
            if self.args.config:
                p = Path(self.args.config)
                disk = json.loads(p.read_text(encoding="utf-8"))
                disk.setdefault("voice", {}).setdefault(
                    "bot", {})["enabled"] = bool(value)
                p.write_text(json.dumps(disk, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        except Exception as e:
            log.warning("KI-Funker enabled-Persistenz fehlgeschlagen: %s", e)

    async def _bot_apply_command(self, room: "FRNTXRoom", room_name: str,
                                 bot: dict, cmd: str):
        """Schaltet den Bot per Funkbefehl und quittiert per Stimme."""
        turn_on = (cmd == "on")
        self._bot_set_enabled(turn_on)
        log.info("[%s] KI-Funker per Funk %s", room_name,
                 "geweckt" if turn_on else "stummgeschaltet")
        ctl = bot.get("control") or {}
        if ctl.get("confirm", True):
            txt = (ctl.get("reply_on") if turn_on else ctl.get("reply_off")) or ""
            if txt:
                try:
                    await self._auto_send_voice(
                        room, txt, bot.get("speaker") or "default")
                except Exception as e:
                    log.warning("[%s] Steuerungs-Quittung: %s", room_name, e)

    def _bot_observe(self, room: "FRNTXRoom", room_name: str, callsign: str,
                     text: str, ts: float, low: str):
        """Verlauf pflegen und entscheiden, ob der KI-Funker antworten soll.

        Vorfilter (Name/allgemeiner Anruf/laufendes Gespräch) spart Ollama-
        Aufrufe; die eigentliche Entscheidung trifft das Modell (SKIP-Option).
        """
        bot  = self._bot_cfg()
        name = bot.get("name") or "Robert"
        own  = self._bot_is_own(room_name, text, ts)
        hist = self._room_hist.setdefault(room_name, [])
        hist.append((ts, f"{name} (du)" if own else (callsign or "Funker"),
                     text))
        del hist[:-max(4, int(bot.get("history_len", 10)))]
        if own:
            return   # eigenes Echo -- keine Spur, das ist kein "gehoerter" Funkspruch
        # Sprachsteuerung: greift AUCH bei deaktiviertem Bot (sonst kein Wecken).
        # No-Op (schon im Zielzustand) fällt durch zur normalen Antwort-Logik.
        cmd = self._bot_command(bot, name, low)
        if cmd == "off" and bot.get("enabled"):
            self.debug_trace_step(room_name, ts, "Bot-Trigger", "ok",
                                  detail="Sprachsteuerung: aus", final=True)
            asyncio.create_task(self._bot_apply_command(room, room_name, bot, "off"))
            return
        if cmd == "on" and not bot.get("enabled"):
            self.debug_trace_step(room_name, ts, "Bot-Trigger", "ok",
                                  detail="Sprachsteuerung: an", final=True)
            asyncio.create_task(self._bot_apply_command(room, room_name, bot, "on"))
            return
        if not bot.get("enabled"):
            self.debug_trace_step(room_name, ts, "Bot-Trigger", "skip",
                                  detail="KI-Funker deaktiviert", final=True)
            return
        rooms = bot.get("rooms") or []
        if rooms and room_name not in rooms:
            self.debug_trace_step(room_name, ts, "Bot-Trigger", "skip",
                                  detail="Raum nicht in Robert-Liste (z.B. Papagei-Testraum)",
                                  final=True)
            return
        now  = time.time()
        last = self._bot_last_reply.get(room_name, 0.0)
        if now - last < float(bot.get("cooldown_s", 20)):
            self.debug_trace_step(room_name, ts, "Bot-Trigger", "skip",
                                  detail="Cooldown aktiv", final=True)
            return
        in_conv  = (now - last) < float(bot.get("conversation_window_s", 180))
        triggers = [t.lower() for t in bot.get("trigger", []) if t.strip()]
        triggers.append(name.lower())
        name_or_call = any(t in low for t in triggers) or bool(self._BOT_CALL_RE.search(low))
        # Nach laengerer Funkstille ist so gut wie jeder (auch kurze/durch
        # Whisper verhunzte) Spruch praktisch immer ein allgemeiner Anruf
        # ("ist wer da?") -- Vorfilter sonst zu streng (kein Name/Anruf-Muster
        # im -- moeglicherweise falsch erkannten -- Text), Modell bekommt die
        # Nachricht in dem Fall nie zu Gesicht. Gleicher Schwellwert wie der
        # Pausen-Hinweis im Prompt (_bot_build_prompt GAP_NOTE_S), damit beides
        # zusammenpasst: Vorfilter laesst durch, Prompt erklaert warum.
        prev_ts      = hist[-2][0] if len(hist) >= 2 else None
        silence_s    = (ts - prev_ts) if prev_ts is not None else None
        long_silence = (silence_s is not None
                        and silence_s >= float(bot.get("silence_call_threshold_s", 300)))
        if not (in_conv or name_or_call or long_silence):
            self.debug_trace_step(room_name, ts, "Bot-Trigger", "skip",
                                  detail="nicht angesprochen (kein Name/Anruf, kein laufendes Gespräch)",
                                  final=True)
            return
        search_query = ""
        if (bot.get("websearch") or {}).get("enabled"):
            m = self._BOT_SEARCH_RE.search(text)
            if m:
                q = (m.group(1) or m.group(2) or m.group(3)
                     or "").strip(" .,!?;:\"'")
                if len(q) >= 3:
                    search_query = q[:200]
        reason = ("Gespräch läuft" if in_conv else
                 "Websuche erkannt" if search_query else
                 "Name/Anruf erkannt" if name_or_call else
                 "Anruf nach langer Funkstille")
        self.debug_trace_step(room_name, ts, "Bot-Trigger", "ok", detail=reason)
        asyncio.create_task(self._bot_reply(room, room_name, bot, search_query))

    async def _bot_reply(self, room: "FRNTXRoom", room_name: str, bot: dict,
                         search_query: str = ""):
        """Antwort generieren und senden (höchstens ein Lauf pro Raum)."""
        if room_name in self._bot_busy:
            return
        self._bot_busy.add(room_name)
        heard_ts = time.time()
        try:
            hist   = list(self._room_hist.get(room_name, []))
            if hist:
                heard_ts = hist[-1][0]   # Zeitpunkt des ausloesenden Funkspruchs
            answer = await self._bot_ollama(bot, hist, search_query=search_query,
                                            room_name=room_name, trace_ts=heard_ts)
            if not answer:
                log.info("[%s] KI-Funker: nicht gemeint (SKIP)", room_name)
                self.debug_trace_step(room_name, heard_ts, "Ergebnis", "skip",
                                      detail="Modell: SKIP (nicht gemeint)", final=True)
                return
            # Wiederhol-Schleifen-Bremse (2026-08-11): beobachtet, dass Robert
            # gelegentlich seine eigenen Persona-/System-Prompt-Regeln als
            # Antworttext ausgibt statt sie zu befolgen -- landet diese
            # Fehlantwort einmal im Verlauf, sieht das Modell sich selbst
            # diesen Text sagen und wiederholt ihn bei jedem folgenden
            # kurzen/kontextarmen Spruch nahezu identisch weiter (reproduziert
            # per Test bestaetigt). Bricht die Schleife spaetestens beim
            # zweiten Versuch ab, statt endlos live zu senden.
            own_prev = self._bot_own_tx.get(room_name, [])
            if own_prev:
                sim = difflib.SequenceMatcher(
                    None, answer.lower(), own_prev[-1][2].lower()).ratio()
                if sim > 0.7:
                    log.warning("[%s] KI-Funker: Antwort zu aehnlich zur letzten eigenen "
                               "(%.2f) -- unterdrueckt gegen Wiederhol-Schleife: %.80s",
                               room_name, sim, answer)
                    self.debug_trace_step(room_name, heard_ts, "Ergebnis", "skip",
                                          detail=f"Wiederholung unterdrueckt (Aehnlichkeit {sim:.2f})",
                                          final=True)
                    return
            log.info("[%s] KI-Funker antwortet: %.80s", room_name, answer)
            t0 = time.time()
            def _on_tx_start():
                tx_ts = time.time()
                log.info("[%s] KI-Funker Sendebeginn: %.1fs nach Antwort (TTS-Zeit)",
                         room_name, tx_ts - t0)
                self.debug_trace_step(room_name, heard_ts, "Sende-Start", "ok",
                                     tx_ts - t0, "Sender getastet, Übertragung beginnt")
            sent = await self._auto_send_voice(room, answer,
                                               bot.get("speaker") or "default",
                                               on_tx_start=_on_tx_start)
            t1   = time.time()
            log.info("[%s] KI-Funker TTS+Senden: %.1fs — Gesamt (gehört→gesendet): %.1fs",
                     room_name, t1 - t0, t1 - heard_ts)
            self.debug_trace_step(room_name, heard_ts, "TTS+Senden",
                                  "ok" if sent else "error", t1 - t0,
                                  answer if sent else "Senden fehlgeschlagen",
                                  final=True)
            if not sent:
                return
            self._bot_last_reply[room_name] = t1
            own = self._bot_own_tx.setdefault(room_name, [])
            own.append((t0, t1, answer))
            del own[:-6]
            name = bot.get("name") or "Robert"
            self._room_hist.setdefault(room_name, []).append(
                (t1, f"{name} (du)", answer))
            payload = {"type": "voice_autosent", "room": room_name,
                       "from": "KI-Funker", "text": answer, "ok": True,
                       "heard": hist[-1][2] if hist else ""}
            dead = set()
            for ws in list(room._chat_clients):
                try:
                    await ws.send_json(payload)
                except Exception:
                    dead.add(ws)
            room._chat_clients -= dead
        except Exception as e:
            log.warning("[%s] KI-Funker-Antwort fehlgeschlagen: %s",
                        room_name, e)
            self.debug_trace_step(room_name, heard_ts, "Fehler", "error",
                                  detail=str(e)[:200], final=True)
        finally:
            self._bot_busy.discard(room_name)

    @staticmethod
    def _ollama_headers(token: str | None):
        """Authorization-Header (Bearer) für einen Ollama-Server hinter
        Token-Proxy. Bearer, weil gängige Clients (z.B. Open WebUI) nur das
        können. Leerer/kein Token → None (offener Server, kein Header)."""
        token = (token or "").strip()
        return {"Authorization": f"Bearer {token}"} if token else None

    async def _bot_websearch(self, bot: dict, query: str) -> str:
        """Fragt die lokale SearXNG-Instanz ab (JSON-API) und liefert eine
        kurze Zusammenfassung der Top-Treffer fürs Prompt ("" bei Fehler/
        keinen Treffern). Läuft NUR lokal/im LAN (siehe pass_ip in SearXNGs
        limiter.toml) — kostenlos, kein Cloud-Suchdienst."""
        ws  = bot.get("websearch") or {}
        url = (ws.get("searxng_url")
               or "http://127.0.0.1:8075/search").strip()
        n   = int(ws.get("max_results", 3) or 3)
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url, params={"q": query,
                                                  "format": "json"}) as resp:
                    if resp.status != 200:
                        log.warning("KI-Funker Websuche HTTP %d", resp.status)
                        return ""
                    data = await resp.json()
        except Exception as e:
            log.warning("KI-Funker: Websuche fehlgeschlagen: %s", e)
            return ""
        results = (data.get("results") or [])[:max(1, n)]
        if not results:
            return ""
        lines = []
        for i, r in enumerate(results, 1):
            title   = (r.get("title") or "").strip()
            content = (r.get("content") or "").strip()
            lines.append(f"{i}. {title} — {content}"[:220])
        return "\n".join(lines)

    _NOTEBOOK_MAX = 60   # aelteste Notizen fallen raus, sonst waechst der Prompt

    def _bot_save_note(self, text: str) -> None:
        """Haengt eine Notiz ans Notizbuch (voice.bot.notebook) an -- im
        Speicher (fuer sofortige Wirkung) UND auf der Platte (ueberlebt
        Neustarts). Read-modify-write gegen die Disk-Datei wie in
        handle_admin_bot, damit ein zeitgleicher Admin-Edit nicht ueberschrieben
        wird -- Robert schreibt hier autonom, potenziell waehrend der Admin
        gerade was anderes speichert."""
        text = text.strip()[:300]
        if not text:
            return
        entry = {"ts": time.time(), "text": text}
        bot = self.cfg.setdefault("voice", {}).setdefault("bot", {})
        notebook = bot.setdefault("notebook", [])
        notebook.append(entry)
        del notebook[:-self._NOTEBOOK_MAX]
        log.info("KI-Funker Notizbuch: %.80s", text)
        try:
            if not self.args.config:
                return
            cfg_path = Path(self.args.config)
            disk = json.loads(cfg_path.read_text(encoding="utf-8"))
            blk = disk.setdefault("voice", {}).setdefault("bot", {})
            disk_notebook = blk.setdefault("notebook", [])
            disk_notebook.append(entry)
            del disk_notebook[:-self._NOTEBOOK_MAX]
            cfg_path.write_text(
                json.dumps(disk, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
        except Exception as e:
            log.warning("KI-Funker Notizbuch nicht auf Platte gespeichert: %s", e)

    def _bot_build_prompt(self, bot: dict, hist: list,
                          search_context: str = "") -> tuple[str, list]:
        """Baut System-Anweisung + Nachrichtenverlauf (provider-neutral).
        Nachrichten: role 'user' (fremde Funksprüche) / 'assistant' (eigene).
        search_context: optionale Websuche-Treffer, werden dem System-Prompt
        angehängt (nicht als eigene Chat-Nachricht, bleibt so provider-neutral)."""
        name = bot.get("name") or "Robert"
        own_tag = f"{name} (du)"
        # System-Anweisung aus der config (voice.bot.system_prompt), Platzhalter
        # {name}/{persona} eingesetzt.
        tmpl = bot.get("system_prompt") or self._BOT_SYSTEM_DEFAULT
        system = (tmpl.replace("{persona}", (bot.get("persona") or "").strip())
                      .replace("{name}", name))
        if search_context:
            system += ("\n\nAktuelle Websuche-Ergebnisse (nutze sie nur, "
                       "wenn sie zur Frage passen, fass sie kurz und locker "
                       "wie am Funk üblich zusammen, keine Web-Adressen "
                       "vorlesen):\n" + search_context)
        # Verlauf enthaelt sonst NUR Rufzeichen+Text, keine Zeit -- ein Spruch
        # von vor 6 Stunden sah fuers Modell genauso "gerade eben" gesagt aus
        # wie einer von vor 10 Sekunden. Fix: uralte Eintraege raus, echte
        # Pausen dazwischen als Marker sichtbar machen.
        STALE_DROP_S = 3600   # aelter als 1h -- fuer die aktuelle Lage irrelevant
        GAP_NOTE_S   = 300    # ab 5 Min. Pause einen Hinweis einfuegen (war 3 --
                               # zu kurz auf einem belebten Kanal, wo Antworten
                               # oft ein paar Minuten brauchen)
        now = time.time()
        recent = [(ts, who, txt)
                  for ts, who, txt in hist[-int(bot.get("history_len", 10)):]
                  if now - ts <= STALE_DROP_S]
        if recent:
            system += ("\n\nWenn eine Nachricht wie '[... 12 Minuten Pause ...]' "
                       "erscheint, ist seitdem eine Pause im Funkverkehr vergangen. "
                       "Smalltalk ANDERER Sprecher von davor ist meist nicht mehr "
                       "aktuell -- ABER: War deine eigene letzte Nachricht davor "
                       "eine Frage, und kommt danach (ohne dass zwischendurch "
                       "jemand anderes was Eigenes sagt) eine Antwort, ist das "
                       "trotz der Pause meist die Antwort auf genau diese Frage "
                       "-- geh darauf ein, tu nicht so als waere nichts gefragt "
                       "worden.\nKommt direkt nach so einer Pause ein kurzer oder "
                       "inhaltlich unklarer/unpassender Spruch (auch wenn er nur "
                       "aus 1-2 Woertern besteht oder wie \"keine Ahnung, ja\" o.ae. "
                       "wirkt -- Whisper verhoert kurze Sprueche nach Stille besonders "
                       "oft), ist das so gut wie sicher ein allgemeiner Anruf/Check, "
                       "ob ueberhaupt wer auf dem Kanal ist -- reagier entsprechend "
                       "(z.B. kurz melden), auch wenn der Wortlaut selbst nicht "
                       "danach aussieht.")
        # Personen-Gedaechtnis: manuell gepflegte Notizen pro Name (Admin-
        # Panel), da es hier KEIN verlaessliches Rufzeichen pro Aufnahme gibt
        # (Sprecher-Zuordnung ist deaktiviert, siehe bot_archive_callsign) --
        # Namen werden stattdessen im gesprochenen Text erkannt (simpler
        # Substring-Match, genau wie ein Mensch am Funk mitbekommt "ah, das
        # ist ja der Jörg"). Nur einspeisen, wenn der Name im aktuellen
        # Gespraechsfenster tatsaechlich vorkommt -- sonst wuerde bei vielen
        # Eintraegen der Prompt aufgeblaeht und Robert faengt an, unpassend
        # ueber abwesende Leute zu reden.
        memory = bot.get("memory") or []
        if memory and recent:
            window = " ".join(f"{who} {txt}" for _, who, txt in recent).lower()
            hits = [m for m in memory
                    if isinstance(m, dict) and (m.get("name") or "").strip()
                    and m["name"].strip().lower() in window
                    and (m.get("notes") or "").strip()]
            if hits:
                notes_txt = "\n".join(f"- {m['name'].strip()}: {m['notes'].strip()}"
                                      for m in hits)
                system += ("\n\nBekannte Infos zu Personen im aktuellen Gespräch "
                           "(nutze sie nur, wenn's natürlich passt, erzähl nicht "
                           "unaufgefordert alles auf einmal runter, und erwähne "
                           "nicht, dass du dir das notiert hast):\n" + notes_txt)
        # Notizbuch: eigene, per Tool-Call selbst gemerkte Notizen (siehe
        # _BOT_NOTE_TOOL/_bot_save_note) -- im Unterschied zum Personen-
        # Gedaechtnis oben IMMER eingespeist (nicht an einen im Gespraech
        # vorkommenden Namen gebunden), aber auf die letzten paar begrenzt,
        # sonst blaeht sich der Prompt mit der Zeit unbegrenzt auf.
        notebook = bot.get("notebook") or []
        if notebook:
            recent_notes = notebook[-12:]
            notes_txt = "\n".join(f"- {n['text'].strip()}" for n in recent_notes
                                  if isinstance(n, dict) and (n.get("text") or "").strip())
            if notes_txt:
                system += ("\n\nDeine eigenen bisherigen Notizen (was du dir "
                           "schon gemerkt hast, nutze es nur wenn's passt, "
                           "erwähne nicht, dass du dir das notiert hast):\n"
                           + notes_txt)
        messages = []
        prev_ts = None
        for ts, who, txt in recent:
            if prev_ts is not None and ts - prev_ts >= GAP_NOTE_S:
                gap_min = round((ts - prev_ts) / 60)
                messages.append({"role": "user",
                                  "content": f"[... {gap_min} Minuten Pause ...]"})
            if who == own_tag:
                messages.append({"role": "assistant", "content": txt})
            else:
                messages.append({"role": "user", "content": f"{who}: {txt}"})
            prev_ts = ts
        return system, messages

    async def _bot_ollama(self, bot: dict, hist: list, with_raw: bool = False,
                          search_query: str = "", room_name: str = "",
                          trace_ts: float | None = None):
        """Entscheidung + Antwort des KI-Funkers. Provider laut voice.bot.provider
        (ollama = lokal/eigener Server, gemini = Google-Cloud). Das Modell darf mit
        SKIP schweigen. Liefert "" wenn der Bot nicht antworten soll.
        with_raw=True → (verarbeitete Antwort, roher Modell-Output) für den Test.
        search_query: erkannter Websuche-Auftrag (siehe _BOT_SEARCH_RE) —
        wird vor dem LLM-Aufruf per SearXNG aufgelöst und in den Kontext
        eingespeist. room_name/trace_ts: Zuordnung fuers Debug-Panel, beide
        leer/None beim Trockentest (kein Tracing noetig)."""
        do_trace = bool(room_name) and trace_ts is not None
        search_context = ""
        if search_query:
            _t0 = time.time()
            search_context = await self._bot_websearch(bot, search_query)
            _sdt = time.time() - _t0
            log.info("KI-Funker Websuche: %.1fs", _sdt)
            if do_trace:
                self.debug_trace_step(room_name, trace_ts, "Websuche",
                                     "ok" if search_context else "warn", _sdt,
                                     search_context[:200] if search_context
                                     else "keine Treffer")
        system, messages = self._bot_build_prompt(bot, hist, search_context)
        provider = (bot.get("provider") or "ollama").strip().lower()
        _t0 = time.time()
        if provider == "gemini":
            raw = await self._llm_gemini(bot, system, messages)
        else:
            raw = await self._llm_ollama(bot, system, messages,
                                         room_name=room_name, trace_ts=trace_ts)
        _lldt = time.time() - _t0
        log.info("KI-Funker LLM (%s): %.1fs", provider, _lldt)
        ans = ""
        if raw:
            # <think>…</think> entfernen (Reasoning-Modelle wie qwen3 geben es aus)
            cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip().strip('"')
            # Manche Modelle haengen trotz Prompt-Anweisung ein "Robert: " vor
            # die Antwort (imitiert das "Rufzeichen: Text"-Format aus dem
            # Verlauf) -- als Sicherheitsnetz zusaetzlich zur Prompt-Regel
            # weg damit, sonst wird der Name-Prefix live vorgelesen.
            name = bot.get("name") or "Robert"
            cleaned = re.sub(rf"^{re.escape(name)}\s*:\s*", "", cleaned,
                             flags=re.IGNORECASE).strip()
            # Emojis raus (werden vorgelesen, Prompt-Verbot reicht nicht immer)
            cleaned = self._EMOJI_RE.sub("", cleaned)
            cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
            if cleaned and not cleaned.upper().startswith("SKIP"):
                ans = cleaned   # sonst: nicht gemeint / nichts zu sagen
        if do_trace:
            self.debug_trace_step(room_name, trace_ts, "LLM",
                                 "ok" if raw else "error", _lldt,
                                 (ans or raw or "(keine Antwort)")[:300])
        return (ans, raw or "") if with_raw else ans

    async def _llm_ollama(self, bot: dict, system: str, messages: list,
                          room_name: str = "", trace_ts: float | None = None) -> str:
        """Chat-Aufruf an einen Ollama-Server. Liefert rohen Antworttext ("" bei Fehler).

        Bei aktivierter Websuche wird dem Modell zusaetzlich zum Regex-Trigger
        (siehe _BOT_SEARCH_RE, faengt nur explizite "such mal nach X"-Befehle)
        das echte websearch-Tool angeboten -- das Modell kann so auch von sich
        aus nachschlagen, wenn es ein erwaehntes Thema nicht kennt, statt zu
        halluzinieren. Bei Tool-Call: zweite Anfrage mit Suchergebnis als
        tool-Nachricht, liefert dann die eigentliche Antwort."""
        ar    = self.cfg.get("voice", {}).get("auto_reply", {})
        url   = (bot.get("ollama_url") or ar.get("ollama_url")
                 or "http://192.0.0.17:11434").rstrip("/")
        do_trace = bool(room_name) and trace_ts is not None
        full_messages = [{"role": "system", "content": system}] + messages
        hdrs = self._ollama_headers(bot.get("ollama_token"))

        # Bereits geladenes Modell bevorzugen statt stur das konfigurierte
        # anzufordern -- vermeidet einen kompletten Neu-Load (~10-15s), wenn
        # z.B. gerade interaktiv über Open WebUI ein anderes Modell laeuft.
        # ACHTUNG: Persona/Prompt/Temperature sind speziell auf das konfigurierte
        # Modell (Aura-Medium alias gemma4) abgestimmt -- laeuft zufaellig ein
        # komplett anderes Modell, kann sich Robert dadurch spuerbar anders
        # verhalten. Faellt still auf das konfigurierte Modell zurueck, wenn
        # gerade nichts geladen ist oder die Abfrage fehlschlaegt.
        model   = bot.get("ollama_model") or "qwen3:14b"
        num_ctx = int(bot.get("ollama_num_ctx", 97280))
        try:
            ps_timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=ps_timeout) as sess:
                async with sess.get(f"{url}/api/ps", headers=hdrs) as resp:
                    if resp.status == 200:
                        loaded = (await resp.json()).get("models") or []
                        if loaded:
                            entry = loaded[0]
                            loaded_name = entry.get("name") or entry.get("model")
                            if loaded_name and loaded_name != model:
                                log.info("KI-Funker: nutze bereits geladenes Modell %s "
                                        "statt konfiguriertem %s (spart Neu-Load)",
                                        loaded_name, model)
                                model = loaded_name
                            # ACHTUNG (2026-08-10, wieder entfernt): das
                            # Kontextfenster des laufenden Modells NICHT mehr
                            # uebernehmen -- wenn ein anderer Client (Open
                            # WebUI) selbst mit wechselnden num_ctx anfragt,
                            # jagt dieser Code dem jeweils zuletzt gesehenen
                            # Wert hinterher und verursacht dadurch staendige
                            # Neu-Loads (Ping-Pong), statt sie zu vermeiden.
                            # Eigener fester Wert (ollama_num_ctx) ist stabiler.
        except Exception as e:
            log.debug("KI-Funker: /api/ps nicht erreichbar (%s) -- nutze konfiguriertes Modell/Kontext", e)
        body = {"model": model,
                "messages": full_messages,
                "stream": False, "keep_alive": bot.get("ollama_keep_alive", 0),
                # num_ctx: fest aus voice.bot.ollama_num_ctx (Default 97280).
                # Bewusst NICHT dynamisch vom gerade geladenen Modell
                # uebernommen (siehe Kommentar oben) -- das verursachte
                # Neu-Lade-Ping-Pong, wenn andere Clients (Open WebUI) selbst
                # wechselnde num_ctx anfragen.
                "options": {"num_predict": 150, "temperature": 0.4,
                            "num_ctx": num_ctx},
                # Immer aus: bei Thinking-Modellen (qwen3, gemma4, deepseek-r1, …)
                # frisst der Grübel-Block sonst das ganze num_predict-Budget auf
                # (done_reason="length" BEVOR ueberhaupt eine sichtbare Antwort
                # entsteht -- Robert "antwortet" dann mit leerem Text, wirkt wie
                # SKIP). Bei Modellen ohne Thinking-Modus wird das Feld einfach
                # ignoriert, schadet also nicht.
                "think": False}
        tools = []
        if (bot.get("websearch") or {}).get("enabled"):
            tools += self._BOT_WEBSEARCH_TOOL
        if bot.get("notebook_enabled"):
            tools += self._BOT_NOTE_TOOL
        if tools:
            body["tools"] = tools
        try:
            timeout = aiohttp.ClientTimeout(total=180)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(f"{url}/api/chat", json=body,
                                     headers=hdrs) as resp:
                    if resp.status != 200:
                        log.warning("KI-Funker Ollama HTTP %d: %s", resp.status,
                                    (await resp.text())[:120])
                        return ""
                    data = await resp.json()
        except Exception as e:
            log.warning("KI-Funker: Ollama nicht erreichbar (%s): %s", url, e)
            return ""

        msg = data.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return (msg.get("content") or "").strip()

        call = tool_calls[0]
        fn   = call.get("function") or {}
        name = fn.get("name") or ""
        args = fn.get("arguments") or {}
        if name == "notiz_merken":
            note = str(args.get("text") or "").strip()[:300]
            self._bot_save_note(note)
            result = "notiert" if note else "keine Notiz erhalten"
            if do_trace:
                self.debug_trace_step(room_name, trace_ts, "Notizbuch", "ok" if note else "warn",
                                     None, f"[Modell-Initiative] {note[:200]}")
        else:
            query = str(args.get("query") or "").strip()[:200]
            _t0 = time.time()
            result = await self._bot_websearch(bot, query) if query else ""
            _sdt = time.time() - _t0
            log.info("KI-Funker Websuche (Tool-Call, Modell-Initiative): %.1fs -- %r",
                     _sdt, query)
            if do_trace:
                self.debug_trace_step(room_name, trace_ts, "Websuche", "ok" if result else "warn",
                                     _sdt, f"[Modell-Initiative] {query}: " +
                                     (result[:200] if result else "keine Treffer"))
            result = result or "keine Treffer gefunden"
        follow = full_messages + [
            {"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls},
            {"role": "tool", "content": result or "keine Treffer gefunden"},
        ]
        body2 = {**body, "messages": follow}
        body2.pop("tools", None)   # zweite Runde: kein erneuter Tool-Call noetig
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(f"{url}/api/chat", json=body2,
                                     headers=hdrs) as resp:
                    if resp.status != 200:
                        log.warning("KI-Funker Ollama (nach Tool-Call) HTTP %d: %s",
                                    resp.status, (await resp.text())[:120])
                        return ""
                    data2 = await resp.json()
        except Exception as e:
            log.warning("KI-Funker: Ollama nicht erreichbar (Tool-Call-Folgeanfrage, %s): %s",
                       url, e)
            return ""
        return ((data2.get("message") or {}).get("content") or "").strip()

    async def _llm_gemini(self, bot: dict, system: str, messages: list) -> str:
        """Chat-Aufruf an Google Gemini (generateContent). Liefert rohen
        Antworttext ("" bei Fehler). Websuche/Grounding braucht bezahltes
        Kontingent und ist daher hier nicht aktiviert."""
        key = (bot.get("gemini_api_key") or "").strip()
        if not key:
            log.warning("KI-Funker: Gemini gewählt, aber kein gemini_api_key gesetzt")
            return ""
        model = bot.get("gemini_model") or "gemini-flash-lite-latest"
        contents = [{"role": "model" if m["role"] == "assistant" else "user",
                     "parts": [{"text": m["content"]}]} for m in messages]
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": contents,
                "generationConfig": {"temperature": 0.6, "maxOutputTokens": 200}}
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent")
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(url, json=body,
                                     headers={"x-goog-api-key": key}) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        msg = (data.get("error", {}) or {}).get("message", "")
                        log.warning("KI-Funker Gemini HTTP %d: %s",
                                    resp.status, str(msg)[:140])
                        return ""
        except Exception as e:
            log.warning("KI-Funker: Gemini nicht erreichbar: %s", e)
            return ""
        cands = data.get("candidates") or []
        if not cands:
            return ""
        parts = (cands[0].get("content", {}) or {}).get("parts", []) or []
        return "".join(p.get("text", "") for p in parts).strip()

    async def handle_voice_auto_reply(self, request):
        """GET: Automatik-Status; POST {enabled}: ein/aus (persistiert)."""
        token = self._token_from(request)
        if not self._validate_token(token):
            return web.json_response({"error": "unauthorized"}, status=401)
        ar = self.cfg.setdefault("voice", {}).setdefault("auto_reply", {})
        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"error": "bad request"}, status=400)
            ar["enabled"] = bool(body.get("enabled"))
            try:
                if not self.args.config:
                    raise RuntimeError("kein --config Pfad")
                cfg_path = Path(self.args.config)
                disk = json.loads(cfg_path.read_text(encoding="utf-8"))
                disk.setdefault("voice", {}).setdefault(
                    "auto_reply", {})["enabled"] = ar["enabled"]
                cfg_path.write_text(
                    json.dumps(disk, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
            except Exception as e:
                log.warning("auto_reply-Toggle nicht gespeichert: %s", e)
            log.info("Auto-Antwort %s",
                     "aktiviert" if ar["enabled"] else "deaktiviert")
        return web.json_response({"enabled": bool(ar.get("enabled", False))})

    def _voice_server_base(self) -> str:
        """Basis-URL des XTTS-Voice-Servers (xtts_url ohne /tts) fuer den
        Stimm-Sample-Upload -- IMMER XTTS, unabhaengig davon welche Engine
        Robert (remote_url) gerade nutzt, denn nur XTTS kennt hochgeladene
        Referenz-Stimmen (Piper/Google nicht)."""
        url = (self.cfg.get("voice", {}).get("xtts_url") or "").strip()
        return re.sub(r"/tts/?$", "", url)

    async def handle_voice_my_settings(self, request):
        """GET/POST: persönliche Automatik-Einstellungen des angemeldeten Users."""
        token = self._token_from(request)
        info  = self._validate_token(token)
        if not info:
            return web.json_response({"error": "unauthorized"}, status=401)
        uname = info["user"].lower()
        user  = self.users.get(uname)
        if not user:
            return web.json_response({"error": "unbekannter Benutzer"}, status=404)
        vb = user.setdefault("voice", {})
        ar = vb.setdefault("auto_reply", {})

        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"error": "bad request"}, status=400)

            def _strlist(v):
                if isinstance(v, str):
                    v = v.split(",")
                return [s.strip() for s in v if isinstance(s, str) and s.strip()]

            if "enabled" in body:
                ar["enabled"] = bool(body["enabled"])
            if "auto_send" in body:
                ar["auto_send"] = bool(body["auto_send"])
            if "names" in body:
                ar["names"] = _strlist(body["names"])
            if "persona" in body and isinstance(body["persona"], str):
                ar["persona"] = body["persona"].strip()
            self._save_users()
            log.info("Automatik-Einstellungen von %s gespeichert: names=%s "
                     "auto_send=%s", uname, ar.get("names"), ar.get("auto_send"))

        return web.json_response({
            "enabled":    bool(ar.get("enabled", False)),
            "auto_send":  bool(ar.get("auto_send", False)),
            "names":      ar.get("names", []),
            "persona":    ar.get("persona", ""),
            "has_sample": bool(vb.get("sample")),
            "speaker":    self._user_speaker(uname),
        })

    async def handle_voice_sample_upload(self, request):
        """POST: eigenes Stimm-Sample hochladen (Browser-Aufnahme, webm/wav).

        Wird auf 24 kHz mono normalisiert, lokal gespeichert und an den
        Voice-Server übertragen (dort: Latents-Cache-Invalidierung).
        """
        token = self._token_from(request)
        info  = self._validate_token(token)
        if not info:
            return web.json_response({"error": "unauthorized"}, status=401)
        uname = info["user"].lower()
        user  = self.users.get(uname)
        if not user:
            return web.json_response({"error": "unbekannter Benutzer"}, status=404)

        raw = await request.read()
        if len(raw) < 10000:
            return web.json_response({"error": "Aufnahme zu kurz"}, status=400)

        spk        = self._speaker_id(uname)
        voices_dir = Path(__file__).parent / "voices"
        voices_dir.mkdir(exist_ok=True)
        wav_path   = voices_dir / f"{spk}.wav"

        # Browser-Audio (webm/opus/wav) → 24 kHz mono s16, normalisiert
        ffm = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", "pipe:0",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ar", "24000", "-ac", "1", "-sample_fmt", "s16",
            str(wav_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await ffm.communicate(input=raw)
        if ffm.returncode != 0 or not wav_path.exists():
            return web.json_response({"error": "Audio-Konvertierung fehlgeschlagen"},
                                     status=400)
        import wave as _wave
        try:
            with _wave.open(str(wav_path), "rb") as wf:
                dur = wf.getnframes() / wf.getframerate()
        except Exception:
            wav_path.unlink(missing_ok=True)
            return web.json_response({"error": "ungültiges Audio"}, status=400)
        if dur < 5.0:
            wav_path.unlink(missing_ok=True)
            return web.json_response(
                {"error": f"Aufnahme zu kurz ({dur:.1f}s) — bitte mindestens "
                          f"10–20 Sekunden sprechen"}, status=400)

        # An Voice-Server übertragen
        base = self._voice_server_base()
        if not base:
            return web.json_response({"error": "Voice-Server nicht konfiguriert"},
                                     status=503)
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(f"{base}/speaker/{spk}",
                                     data=wav_path.read_bytes()) as resp:
                    rdata = await resp.json()
                    if resp.status != 200:
                        return web.json_response(
                            {"error": rdata.get("error", "Voice-Server-Fehler")},
                            status=502)
        except Exception as e:
            return web.json_response({"error": f"Voice-Server: {e}"}, status=502)

        user.setdefault("voice", {})["sample"] = f"voices/{spk}.wav"
        self._save_users()
        log.info("Stimm-Sample von %s gespeichert (%.1fs, Sprecher %s)",
                 uname, dur, spk)
        return web.json_response({"ok": True, "duration_s": round(dur, 1),
                                  "speaker": spk})

    async def handle_voice_preview(self, request):
        """POST {text}: kurze Hörprobe in der eigenen Stimme (WAV, kein Funk)."""
        token = self._token_from(request)
        info  = self._validate_token(token)
        if not info:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        text = (body.get("text") or "").strip()[:200]
        if not text:
            return web.json_response({"error": "kein Text"}, status=400)
        vcfg = self.cfg.get("voice", {})
        # Immer XTTS (nicht remote_url) -- die Hoerprobe soll die eigene
        # geklonte Stimme vorspielen, das kann nur XTTS, egal welche Engine
        # Robert gerade nutzt.
        url  = (vcfg.get("xtts_url") or "").strip()
        if not vcfg.get("enabled", False) or not url:
            return web.json_response({"error": "Voice deaktiviert"}, status=503)
        speaker = self._user_speaker(info["user"])
        payload = {"text": text, "language": vcfg.get("language", "de"),
                   "speaker": speaker}
        try:
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(url, json=payload) as resp:
                    if resp.status != 200:
                        return web.json_response(
                            {"error": (await resp.text())[:120]}, status=502)
                    wav = await resp.read()
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)
        return web.Response(body=wav, content_type="audio/wav")

    _AUTO_REPLY_DEFAULTS = {
        "enabled":          False,
        "auto_send":        False,
        "names":            ["jörg"],
        "ignore_callsigns": ["tx-", "stream-", "web-", "audio test"],
        "cooldown_s":       90,
        "ollama_url":       "http://192.0.0.17:11434",
        "ollama_model":     "llama3.2:3B",
        "ollama_token":     "",
        "persona":          "Du bist Jörg, ein CB-Funker aus Eickelborn (Kanal 74).",
    }

    async def handle_admin_auto_reply(self, request):
        """GET: alle Automatik-Einstellungen; POST: speichern (Admin)."""
        _, err = await self._require_admin(request)
        if err:
            return err
        ar = self.cfg.setdefault("voice", {}).setdefault("auto_reply", {})

        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"error": "bad request"}, status=400)

            def _strlist(v):
                if isinstance(v, str):
                    v = v.split(",")
                return [s.strip() for s in v if isinstance(s, str) and s.strip()]

            if "enabled" in body:
                ar["enabled"] = bool(body["enabled"])
            if "auto_send" in body:
                ar["auto_send"] = bool(body["auto_send"])
            if "names" in body:
                names = _strlist(body["names"])
                if names:
                    ar["names"] = names
            if "ignore_callsigns" in body:
                ar["ignore_callsigns"] = _strlist(body["ignore_callsigns"])
            if "cooldown_s" in body:
                try:
                    ar["cooldown_s"] = max(0, min(3600, float(body["cooldown_s"])))
                except (TypeError, ValueError):
                    pass
            for key in ("ollama_url", "ollama_model", "persona"):
                if key in body and isinstance(body[key], str):
                    ar[key] = body[key].strip()

            try:
                if not self.args.config:
                    raise RuntimeError("kein --config Pfad")
                cfg_path = Path(self.args.config)
                disk = json.loads(cfg_path.read_text(encoding="utf-8"))
                blk = disk.setdefault("voice", {}).setdefault("auto_reply", {})
                blk.update({k: v for k, v in ar.items() if not k.startswith("_")})
                cfg_path.write_text(
                    json.dumps(disk, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
            except Exception as e:
                log.warning("Automatik-Einstellungen nicht gespeichert: %s", e)
                return web.json_response({"error": f"Speichern fehlgeschlagen: {e}"},
                                         status=500)
            log.info("Automatik-Einstellungen gespeichert: names=%s model=%s",
                     ar.get("names"), ar.get("ollama_model"))

        out = dict(self._AUTO_REPLY_DEFAULTS)
        out.update({k: v for k, v in ar.items() if not k.startswith("_")})
        return web.json_response(out)

    async def handle_admin_auto_reply_models(self, request):
        """Verfügbare Ollama-Modelle vom konfigurierten Server (Admin)."""
        _, err = await self._require_admin(request)
        if err:
            return err
        ar  = self.cfg.get("voice", {}).get("auto_reply", {})
        url = (request.rel_url.query.get("url")
               or ar.get("ollama_url", "http://192.0.0.17:11434")).rstrip("/")
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(f"{url}/api/tags") as resp:
                    data = await resp.json()
            models = sorted(
                ({"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1)}
                 for m in data.get("models", [])),
                key=lambda m: m["size_gb"])
            return web.json_response({"models": models})
        except Exception as e:
            return web.json_response({"models": [], "error": str(e)})

    async def handle_admin_auto_reply_test(self, request):
        """Testlauf: Satz einwerfen → Ollama-Vorschlag zurück (Admin).

        Persona-Priorität: body.persona > eigene User-Persona > global.
        """
        info, err = await self._require_admin(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        text = (body.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "kein Text"}, status=400)
        persona = (body.get("persona") or "").strip()
        if not persona:
            u = self.users.get(info["user"].lower(), {})
            persona = u.get("voice", {}).get("auto_reply", {}).get("persona", "")
        t0 = time.time()
        suggestion = await self._ollama_suggest(text, persona)
        if not suggestion:
            return web.json_response(
                {"error": "Ollama lieferte keine Antwort (Logs prüfen)"}, status=502)
        return web.json_response({"suggestion": suggestion,
                                  "seconds": round(time.time() - t0, 1)})

    async def handle_admin_bot(self, request):
        """GET: KI-Funker-Einstellungen; POST: speichern (Admin)."""
        _, err = await self._require_admin(request)
        if err:
            return err
        bot = self.cfg.setdefault("voice", {}).setdefault("bot", {})

        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"error": "bad request"}, status=400)

            def _strlist(v):
                if isinstance(v, str):
                    v = v.split(",")
                return [s.strip() for s in v if isinstance(s, str) and s.strip()]

            if "enabled" in body:
                bot["enabled"] = bool(body["enabled"])
            for key in ("name", "persona", "ollama_url", "ollama_model",
                        "provider", "gemini_model", "system_prompt"):
                if key in body and isinstance(body[key], str):
                    bot[key] = body[key].strip()
            if "gemini_api_key" in body and isinstance(body["gemini_api_key"], str):
                k = body["gemini_api_key"].strip()
                if k != self._TOKEN_MASK:   # Maske = unverändert lassen
                    bot["gemini_api_key"] = k
            if "speaker" in body and isinstance(body["speaker"], str):
                # Sprecher-IDs sind strikt [a-z0-9_-]: Tippfehler wie
                # "Aaron,dreschner" leise reparieren statt spaeter TTS-400
                bot["speaker"] = re.sub(
                    r"[^a-z0-9_\-]", "_", body["speaker"].strip().lower())
            if "ollama_keep_alive" in body and isinstance(
                    body["ollama_keep_alive"], (str, int, float)):
                bot["ollama_keep_alive"] = body["ollama_keep_alive"]
            if "ollama_token" in body and isinstance(body["ollama_token"], str):
                tok = body["ollama_token"].strip()
                # Maske aus dem GET = unverändert lassen; leer = löschen
                if tok != self._TOKEN_MASK:
                    bot["ollama_token"] = tok
            if "trigger" in body:
                bot["trigger"] = _strlist(body["trigger"])
            if "rooms" in body:
                bot["rooms"] = _strlist(body["rooms"])
            if "memory" in body and isinstance(body["memory"], list):
                mem = []
                for m in body["memory"]:
                    if not isinstance(m, dict):
                        continue
                    nm = (m.get("name") or "").strip()
                    nt = (m.get("notes") or "").strip()
                    if nm and nt:
                        mem.append({"name": nm[:60], "notes": nt[:500]})
                bot["memory"] = mem[:50]   # grosszuegige Obergrenze gegen Prompt-Aufblaehung
            if "notebook_enabled" in body:
                bot["notebook_enabled"] = bool(body["notebook_enabled"])
            if "notebook" in body and isinstance(body["notebook"], list):
                # Admin darf hier nur LOESCHEN/kuerzen (Checkbox-Liste im Panel) --
                # Robert selbst schreibt per Tool-Call ueber _bot_save_note dazu.
                nb = []
                for n in body["notebook"]:
                    if not isinstance(n, dict):
                        continue
                    tx = (n.get("text") or "").strip()
                    if not tx:
                        continue
                    try:
                        ts = float(n.get("ts") or time.time())
                    except (TypeError, ValueError):
                        ts = time.time()
                    nb.append({"ts": ts, "text": tx[:300]})
                bot["notebook"] = nb[-self._NOTEBOOK_MAX:]
            for key, hi in (("cooldown_s", 3600),
                            ("conversation_window_s", 3600),
                            ("history_len", 30),
                            ("ollama_num_ctx", 262144)):   # Modell-Max laut /api/tags
                if key in body:
                    try:
                        bot[key] = max(0, min(hi, float(body[key])))
                    except (TypeError, ValueError):
                        pass

            try:
                if not self.args.config:
                    raise RuntimeError("kein --config Pfad")
                cfg_path = Path(self.args.config)
                disk = json.loads(cfg_path.read_text(encoding="utf-8"))
                blk = disk.setdefault("voice", {}).setdefault("bot", {})
                blk.update({k: v for k, v in bot.items()
                            if not k.startswith("_")})
                cfg_path.write_text(
                    json.dumps(disk, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
            except Exception as e:
                log.warning("KI-Funker-Einstellungen nicht gespeichert: %s", e)
                return web.json_response(
                    {"error": f"Speichern fehlgeschlagen: {e}"}, status=500)
            log.info("KI-Funker-Einstellungen gespeichert: enabled=%s name=%s "
                     "speaker=%s model=%s", bot.get("enabled"),
                     bot.get("name"), bot.get("speaker"),
                     bot.get("ollama_model"))

        out = dict(self._BOT_DEFAULTS)
        out.update({k: v for k, v in bot.items() if not k.startswith("_")})
        # Geheimnisse nie im Klartext ausliefern — nur "gesetzt/nicht gesetzt"
        out["ollama_token"]   = self._TOKEN_MASK if bot.get("ollama_token") else ""
        out["gemini_api_key"] = self._TOKEN_MASK if bot.get("gemini_api_key") else ""
        # Leere System-Anweisung → Standardvorlage anzeigen (zum Anpassen)
        if not (out.get("system_prompt") or "").strip():
            out["system_prompt"] = self._BOT_SYSTEM_DEFAULT
        return web.json_response(out)

    async def handle_admin_bot_test(self, request):
        """Trockenlauf: Satz einwerfen → Entscheidung + Antwort (sendet NICHT).

        Optional body.room = echten Raumverlauf als Kontext nutzen."""
        _, err = await self._require_admin(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        text = (body.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "kein Text"}, status=400)
        bot       = self._bot_cfg()
        room_name = (body.get("room") or "").strip()
        hist      = list(self._room_hist.get(room_name, []))
        hist.append((time.time(), body.get("from") or "Testfunker", text))
        search_query = (body.get("search_query") or "").strip()
        if not search_query and (bot.get("websearch") or {}).get("enabled"):
            m = self._BOT_SEARCH_RE.search(text)
            if m:
                q = (m.group(1) or m.group(2) or m.group(3) or "").strip(" .,!?;:\"'")
                if len(q) >= 3:
                    search_query = q[:200]
        t0 = time.time()
        answer, raw = await self._bot_ollama(bot, hist, with_raw=True,
                                             search_query=search_query)
        return web.json_response({
            "would_reply": bool(answer),
            "answer": answer or "SKIP",
            "raw": raw,
            "search_query": search_query,
            "seconds": round(time.time() - t0, 1)})

    async def handle_admin_gemini_models(self, request):
        """Verfügbare Gemini-Modell-IDs (Text-Chat) für das Dropdown.
        Verhindert, dass versehentlich ein Anzeigename statt der API-ID
        eingetragen wird."""
        _, err = await self._require_admin(request)
        if err:
            return err
        key = (self._bot_cfg().get("gemini_api_key") or "").strip()
        if not key:
            return web.json_response({"models": [], "error": "kein API-Key"})
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": key}) as resp:
                    data = await resp.json()
            skip = ("tts", "image", "embedding", "aqa", "vision")
            models = sorted(
                m["name"].replace("models/", "")
                for m in data.get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])
                and not any(s in m["name"].lower() for s in skip))
            return web.json_response({"models": models})
        except Exception as e:
            return web.json_response({"models": [], "error": str(e)})

    _CROSSLINK_MASK = "••••••••"

    async def handle_admin_crosslink(self, request):
        """GET/POST fuer die Raum-Crosslink-Bruecke (frn_crosslink.py als
        eigener systemd-Dienst frn-crosslink.service). Diese App laeuft als
        root, daher kein Sudoers-Umweg noetig fuer systemctl."""
        _, err = await self._require_admin(request)
        if err:
            return err
        cl = self.cfg.setdefault("crosslink", {})

        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"error": "bad request"}, status=400)
            for key in ("email", "callsign", "room_a", "room_b"):
                if key in body and isinstance(body[key], str):
                    cl[key] = body[key].strip()
            if "password" in body and isinstance(body["password"], str):
                pw = body["password"].strip()
                if pw != self._CROSSLINK_MASK:
                    cl["password"] = pw
            if "enabled" in body:
                cl["enabled"] = bool(body["enabled"])
            try:
                cfg_path = Path(self.args.config)
                disk = json.loads(cfg_path.read_text(encoding="utf-8"))
                disk.setdefault("crosslink", {}).update(
                    {k: v for k, v in cl.items() if not k.startswith("_")})
                cfg_path.write_text(
                    json.dumps(disk, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
            except Exception as e:
                return web.json_response(
                    {"error": f"Speichern fehlgeschlagen: {e}"}, status=500)
            # Dienst passend zum enabled-Flag schalten
            try:
                if cl.get("enabled"):
                    subprocess.run(["systemctl", "enable", "--now", "frn-crosslink"],
                                   capture_output=True, timeout=15)
                else:
                    subprocess.run(["systemctl", "disable", "--now", "frn-crosslink"],
                                   capture_output=True, timeout=15)
            except Exception as e:
                log.warning("Crosslink-Dienst konnte nicht umgeschaltet werden: %s", e)

        r = subprocess.run(["systemctl", "is-active", "frn-crosslink"],
                          capture_output=True, text=True, timeout=10)
        active = r.stdout.strip() == "active"
        out = dict(cl)
        out["password"] = self._CROSSLINK_MASK if cl.get("password") else ""
        out["active"] = active
        return web.json_response(out)

    async def handle_admin_tts(self, request):
        """GET: aktive TTS-Engine + URLs; POST {engine: piper|xtts|google}: umschalten.

        piper/xtts schalten voice.remote_url zwischen dem lokalen Piper-Dienst
        und dem XTTS-Voice-Clone (GPU-Box) um. google nutzt den kostenlosen,
        inoffiziellen Google-Translate-TTS-Endpunkt (siehe _google_tts_pcm) --
        braucht keine remote_url, die bleibt beim zuletzt aktiven Wert stehen.
        """
        _, err = await self._require_admin(request)
        if err:
            return err
        v = self.cfg.setdefault("voice", {})
        piper = (v.get("piper_url") or "http://127.0.0.1:9003/tts").strip()
        xtts  = (v.get("xtts_url") or v.get("_remote_url_xtts_backup")
                 or "http://192.0.0.17:9002/tts").strip()

        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"error": "bad request"}, status=400)
            engine = (body.get("engine") or "").strip().lower()
            if engine not in ("piper", "xtts", "google"):
                return web.json_response(
                    {"error": "engine muss 'piper', 'xtts' oder 'google' sein"},
                    status=400)
            update = {"tts_engine": engine, "piper_url": piper, "xtts_url": xtts}
            if engine in ("piper", "xtts"):
                update["remote_url"] = piper if engine == "piper" else xtts
            v.update(update)
            try:
                cfg_path = Path(self.args.config)
                disk = json.loads(cfg_path.read_text(encoding="utf-8"))
                disk.setdefault("voice", {}).update(update)
                cfg_path.write_text(
                    json.dumps(disk, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
            except Exception as e:
                return web.json_response(
                    {"error": f"Speichern fehlgeschlagen: {e}"}, status=500)
            log.info("Sprachausgabe umgestellt auf %s (%s)", engine,
                     update.get("remote_url", "kostenloser Google-Endpunkt"))

        engine = v.get("tts_engine")
        if not engine:   # aus aktiver URL ableiten (Alt-Configs ohne tts_engine)
            cur = (v.get("remote_url") or "").strip()
            engine = "xtts" if cur == xtts else "piper"
        return web.json_response({"engine": engine,
                                  "piper_url": piper, "xtts_url": xtts})

    async def _disconnect_user_tx(self, email: str):
        """Trennt alle persistenten User-TX-Verbindungen für eine E-Mail-Adresse."""
        to_del = [k for k in self._user_tx_conns if k[0] == email]
        for k in to_del:
            conn = self._user_tx_conns.pop(k)
            try:
                await conn.disconnect()
            except Exception:
                pass

    def _validate_token(self, token: str) -> dict | None:
        info = self.tokens.get(token)
        if not info:
            return None
        if time.time() > info["expires"]:
            del self.tokens[token]
            self._save_tokens()
            return None
        # Sliding window — Token bei jeder Nutzung verlängern
        info["expires"] = time.time() + self.TOKEN_LIFETIME
        return info

    @staticmethod
    def _token_from(request) -> str:
        """Token aus X-Token-Header (bevorzugt) oder WS-Subprotocol holen.

        Tokens gehören NICHT in die URL: Query-Params landen im Traefik-
        Access-Log. Browser-WebSockets können keine Header setzen — dort
        schickt das Frontend das Token als Subprotocol 'frn.token.<hex>'.
        Der Query-Param bleibt als Legacy-Fallback (alte offene Tabs).
        """
        tok = request.headers.get("X-Token", "")
        if tok:
            return tok
        for p in request.headers.get("Sec-WebSocket-Protocol", "").split(","):
            p = p.strip()
            if p.startswith("frn.token."):
                return p[len("frn.token."):]
        return request.rel_url.query.get("token", "")

    def _client_ip(self, request) -> str:
        """Echte Client-IP: hinter Traefik ist request.remote nur die
        Docker-Gateway-IP — die echte IP steht in X-Forwarded-For."""
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return request.remote or "?"

    def _mqtt_cfg(self) -> dict | None:
        """MQTT-Zugangsdaten aus config.ini [transcription] (gleiche Quelle wie
        die Transkription — kein doppeltes Passwort). Einmal gecacht."""
        if hasattr(self, "_mqtt_cfg_cache"):
            return self._mqtt_cfg_cache
        cfg = None
        try:
            import configparser
            ini = Path(__file__).parent / "config.ini"
            if ini.exists():
                cp = configparser.ConfigParser()
                cp.read(ini)
                if cp.has_section("transcription") and cp.has_option("transcription", "mqtt_broker"):
                    t = cp["transcription"]
                    cfg = {
                        "broker":   t.get("mqtt_broker", "localhost"),
                        "port":     t.getint("mqtt_port", 1883),
                        "user":     t.get("mqtt_user", ""),
                        "password": t.get("mqtt_password", ""),
                        "prefix":   t.get("mqtt_topic_prefix", "Home/FRN").rstrip("/"),
                    }
        except Exception as e:
            log.warning("MQTT-Config lesen fehlgeschlagen: %s", e)
        self._mqtt_cfg_cache = cfg
        return cfg

    async def _notify_new_user(self, username: str, callsign: str, ip: str, how: str):
        """Bei Neuanmeldung eine MQTT-Nachricht auf <prefix>/new_login posten.
        Nicht-blockierend (Publish läuft im ThreadPool). Fehler werden nur
        geloggt — eine Anmeldung darf daran nie scheitern."""
        m = self._mqtt_cfg()
        if not m:
            return
        try:
            import paho.mqtt.publish as publish
            payload = json.dumps({
                "user":     username,
                "callsign": callsign,
                "ip":       ip,
                "auth":     how,   # "local" | "frn"
                "time":     time.strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False)
            topic = f"{m['prefix']}/new_login"
            auth  = {"username": m["user"], "password": m["password"]} if m["user"] else None
            loop  = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: publish.single(topic, payload=payload, hostname=m["broker"],
                                       port=m["port"], auth=auth, qos=0, retain=False))
            log.info("Neuanmeldung gemeldet → MQTT %s: %s (%s)", topic, username, ip)
        except Exception as e:
            log.warning("Neuanmeldung-MQTT fehlgeschlagen: %s", e)

    # ── FRN authentication ─────────────────────────────────────────────────

    async def _fetch_frn_networks(self, email: str, password: str) -> list:
        """Connect to FRN server and return list of available room names.

        After the text-mode CT/AL handshake the server sends a binary stream.
        We watch for MARKER_NETWORKS (0x05) and parse the count + N name lines.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.args.frn_server, self.args.frn_port),
                timeout=5.0,
            )
        except Exception as e:
            log.warning("FRN discover: connection failed: %s", e)
            return []

        inbuf: bytes = b""

        async def read_more(timeout=1.0):
            nonlocal inbuf
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
                if chunk:
                    inbuf += chunk
                    return True
            except asyncio.TimeoutError:
                pass
            return False

        async def get_line(timeout=3.0):
            nonlocal inbuf
            loop     = asyncio.get_event_loop()
            deadline = loop.time() + timeout
            while True:
                idx = inbuf.find(b"\r\n")
                if idx >= 0:
                    line = inbuf[:idx].decode(errors="replace")
                    inbuf = inbuf[idx + 2:]
                    return line
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                await read_more(timeout=min(remaining, 0.5))

        try:
            ct = (
                f"CT:"
                f"<VX>{FRN_PROTO_VERSION}</VX>"
                f"<EA>{email}</EA>"
                f"<PW>{password}</PW>"
                f"<ON>{email.split('@')[0][:8].upper()}</ON>"
                f"<CL>{FRN_TYPE_PC_ONLY}</CL>"
                f"<BC>0</BC>"
                f"<DS>NetDiscover</DS>"
                f"<NN>DE</NN>"
                f"<CT>Stream</CT>"
                f"<NT></NT>"
                f"\r\n"
            )
            writer.write(ct.encode())
            await writer.drain()

            await get_line(timeout=5)           # version line
            al_line = await get_line(timeout=5) # AL result line
            if not al_line:
                return []
            m  = re.search(r"<AL>(.*?)</AL>", al_line)
            al = m.group(1) if m else "?"
            if al not in ("OK", "ADMIN", "OWNER", "NETOWNER"):
                log.warning("FRN discover: auth failed AL=%s", al)
                return []

            writer.write(b"RX0\r\n")
            await writer.drain()

            # Process binary marker stream until we get MARKER_NETWORKS
            loop     = asyncio.get_event_loop()
            deadline = loop.time() + 8.0
            networks: list = []

            while loop.time() < deadline:
                if not inbuf:
                    if not await read_more(timeout=0.5):
                        break

                if not inbuf:
                    continue

                marker = inbuf[0]
                inbuf  = inbuf[1:]

                if marker == 0x00:  # MARKER_KEEPALIVE
                    pass

                elif marker == 0x03:  # MARKER_CLIENTS — 2 extra bytes + count + N lines
                    while len(inbuf) < 2:
                        if not await read_more(timeout=1.0):
                            return networks
                    inbuf = inbuf[2:]
                    cs = await get_line(timeout=2)
                    if cs is None:
                        return networks
                    try:
                        for _ in range(int(cs.strip())):
                            await get_line(timeout=2)
                    except ValueError:
                        pass

                elif marker == 0x05:  # MARKER_NETWORKS — count + N name lines
                    cs = await get_line(timeout=2)
                    if cs is None:
                        return networks
                    try:
                        count = int(cs.strip())
                    except ValueError:
                        return networks
                    for _ in range(count):
                        line = await get_line(timeout=2)
                        if line is None:
                            break
                        # Network name is in <NT>…</NT>; fall back to raw text
                        nm = re.search(r"<NT>(.*?)</NT>", line)
                        if not nm:
                            nm = re.search(r"<\w+>(.*?)<", line)
                        name = nm.group(1) if nm else line.strip()
                        if name:
                            networks.append(name)
                    log.info("FRN networks discovered: %s", networks)
                    return networks

                elif marker in (0x01, 0x04, 0x06, 0x07, 0x08, 0x09, 0x0A):
                    # Other line-list markers — skip count + N lines
                    cs = await get_line(timeout=1)
                    if cs is None:
                        break
                    try:
                        for _ in range(int(cs.strip())):
                            await get_line(timeout=1)
                    except ValueError:
                        pass

                # Unknown / binary-only markers: just continue consuming

            return networks

        except Exception as e:
            log.warning("FRN discover error: %s", e)
            return []
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _discover_rooms(self):
        """Auto-populate rooms from FRN server if no rooms loaded from tx_rooms.json.

        Reads ``frn_stream_account`` from config.json:
          {
            "frn_stream_account": {
              "email":            "stream@example.de",
              "password":         "secret",
              "callsign_prefix":  "WEB"   // optional, default "WEB"
            }
          }
        Each discovered network gets a mount name derived from the FRN room name.
        """
        if self.rooms:
            return  # rooms already configured — nothing to do

        acct = self.cfg.get("frn_stream_account", {})
        email    = acct.get("email",    "").strip()
        password = acct.get("password", "").strip()
        if not email or not password:
            log.info("No rooms configured and no frn_stream_account — starting empty")
            return

        prefix   = acct.get("callsign_prefix", "WEB")
        log.info("Auto-discovering FRN networks via %s …", email)
        networks = await self._fetch_frn_networks(email, password)
        if not networks:
            log.warning("FRN network discovery returned no rooms")
            return

        for i, name in enumerate(networks):
            # derive a safe mount name (lowercase alphanum, max 20 chars)
            mount = re.sub(r"[^a-z0-9]+", "", name.lower())[:20] or f"room{i + 1}"
            if mount in self.rooms:
                mount = f"{mount}{i + 1}"
            room = FRNTXRoom(
                name       = name,
                frn_server = self.args.frn_server,
                frn_port   = self.args.frn_port,
                email      = email,
                password   = password,
                callsign   = f"{prefix}-{i + 1:02d}",
            )
            self._set_room_callback(room)
            self.rooms[mount] = room
        log.info("Auto-configured %d rooms: %s", len(self.rooms), list(self.rooms))

    async def _try_frn_auth(self, email: str, password: str, callsign: str) -> bool:
        """Validate credentials by making a test connection to the FRN server.
        Returns True if the server responds with AL=OK (or ADMIN/OWNER/NETOWNER)."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.args.frn_server, self.args.frn_port),
                timeout=5.0,
            )
        except Exception as e:
            log.warning("FRN auth: connection failed: %s", e)
            return False

        try:
            ct = (
                f"CT:"
                f"<VX>{FRN_PROTO_VERSION}</VX>"
                f"<EA>{email}</EA>"
                f"<PW>{password}</PW>"
                f"<ON>{callsign}</ON>"
                f"<CL>{FRN_TYPE_PC_ONLY}</CL>"
                f"<BC>0</BC>"
                f"<DS>WebAuth</DS>"
                f"<NN>DE</NN>"
                f"<CT>Stream</CT>"
                f"<NT></NT>"
                f"\r\n"
            )
            writer.write(ct.encode())
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=5)  # version line
            result_raw = await asyncio.wait_for(reader.readline(), timeout=5)
            result = result_raw.decode(errors="replace")
            m  = re.search(r"<AL>(.*?)</AL>", result)
            al = m.group(1) if m else "?"
            log.info("FRN auth for %s: AL=%s", email, al)
            if al in ("OK", "ADMIN", "OWNER", "NETOWNER"):
                return True
            # AL=BLOCK kann bedeuten dass wir selbst noch verbunden sind —
            # eigene persistente Verbindung gilt als Beweis gültiger Credentials
            if al == "BLOCK":
                own_conn = any(k[0] == email for k in self._user_tx_conns)
                if own_conn:
                    log.info("FRN auth %s: AL=BLOCK, aber eigene Verbindung aktiv — OK", email)
                    return True
            return False
        except Exception as e:
            log.warning("FRN auth error: %s", e)
            return False
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _require_admin(self, request) -> tuple[dict | None, web.Response | None]:
        token = self._token_from(request)
        info  = self._validate_token(token)
        if not info:
            return None, web.json_response({"error": "unauthorized"}, status=401)
        if not info.get("is_admin"):
            return None, web.json_response({"error": "forbidden"}, status=403)
        return info, None

    # ── HTTP handlers ──────────────────────────────────────────────────────

    async def handle_root(self, request):
        html_path = Path(__file__).parent.parent / "web" / "tx_page.html"
        if not html_path.exists():
            html_path = Path(__file__).parent / "tx_page.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="TX server running. tx_page.html not found.",
                            content_type="text/html")

    async def handle_worklet(self, request):
        js_path = Path(__file__).parent.parent / "web" / "tx_processor.js"
        if not js_path.exists():
            js_path = Path(__file__).parent / "tx_processor.js"
        if js_path.exists():
            return web.FileResponse(js_path, headers={
                "Content-Type": "application/javascript"
            })
        return web.Response(status=404, text="tx_processor.js not found")

    async def handle_login(self, request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)

        username = body.get("username", "").strip().lower()
        password = body.get("password", "")
        # auth_mode: "local" | "frn" | "both" (default: "both")
        auth_mode = self.cfg.get("auth", {}).get("mode", "both")

        # Rate-Limit: max. 5 Fehlversuche pro IP und Minute. Die 1s-Verzögerung
        # unten gilt nur pro Request — parallele Versuche umgehen sie sonst.
        # Wichtig auch, weil der FRN-Auth-Pfad Credentials ans Sysman durchreicht
        # (sonst Passwort-Orakel für fremde FRN-Konten).
        ip  = self._client_ip(request)
        now = time.time()
        fails = [t for t in self._login_fails.get(ip, ()) if now - t < 60]
        self._login_fails[ip] = fails
        if len(fails) >= 5:
            log.warning("Login rate-limit für %s (user=%r)", ip, username)
            return web.json_response(
                {"error": "Zu viele Fehlversuche — bitte eine Minute warten"},
                status=429)

        # ── 1. Lokale Authentifizierung ──────────────────────────────────
        if auth_mode in ("local", "both"):
            user = self.users.get(username)
            if user and verify_password(password, user.get("password_hash", "")):
                # Legacy-SHA256-Hashes bei erfolgreichem Login auf PBKDF2 migrieren
                if needs_rehash(user.get("password_hash", "")):
                    user["password_hash"] = hash_password(password)
                    self._save_users()
                    log.info("Passwort-Hash für '%s' auf PBKDF2 migriert", username)
                token = self._token_for(username)
                rooms = [{"mount": m, "name": r.name} for m, r in self.rooms.items()]
                return web.json_response({
                    "token":        token,
                    "callsign":     user["callsign"],
                    "is_admin":     user.get("is_admin", False),
                    "default_room": user.get("default_room", ""),
                    "rooms":        rooms,
                })

        # ── 2. FRN-Authentifizierung ──────────────────────────────────────
        # Benutzername = FRN-E-Mail-Adresse, Callsign = Teil vor dem @
        if auth_mode in ("frn", "both"):
            # Callsign: aus optionalem Feld oder aus E-Mail ableiten
            callsign = body.get("callsign", "").strip()
            if not callsign:
                callsign = username.split("@")[0].upper()
            ok = await self._try_frn_auth(username, password, callsign)
            if ok:
                # Präferenzen aus gespeichertem FRN-Eintrag laden (falls vorhanden)
                prefs = self.users.get(username, {})
                if prefs.get("frn_only"):
                    callsign = prefs.get("callsign") or callsign
                elif username not in self.users:
                    # Erster Login: Nutzer automatisch als frn_only anlegen
                    self.users[username] = {
                        "callsign":     callsign,
                        "is_admin":     False,
                        "default_room": "",
                        "frn_only":     True,
                    }
                    self._save_users()
                    log.info("FRN auto-created user '%s' (%s)", username, callsign)
                    prefs = self.users[username]
                    # Neuanmeldung melden → du entscheidest, ob abgesichert wird.
                    await self._notify_new_user(username, callsign, ip, "frn")
                token = secrets.token_hex(24)
                self.tokens[token] = {
                    "user":         username,
                    "callsign":     callsign,
                    "is_admin":     False,   # FRN-User bekommen keinen Admin-Zugang
                    "expires":      time.time() + self.TOKEN_LIFETIME,
                    "frn_email":    username,   # für eigene TX-Verbindung
                    "frn_password": password,   # nur im RAM, nicht auf Disk
                }
                rooms = [{"mount": m, "name": r.name} for m, r in self.rooms.items()]
                log.info("FRN login: %s (%s)", username, callsign)
                return web.json_response({
                    "token":        token,
                    "callsign":     callsign,
                    "is_admin":     False,
                    "default_room": prefs.get("default_room", ""),
                    "rooms":        rooms,
                })

        self._login_fails[ip].append(time.time())
        if len(self._login_fails) > 1000:   # Speicher begrenzen (alte IPs raus)
            cutoff = time.time() - 60
            self._login_fails = {k: [t for t in v if t > cutoff]
                                 for k, v in self._login_fails.items()}
            self._login_fails = {k: v for k, v in self._login_fails.items() if v}
        await asyncio.sleep(1)
        return web.json_response(
            {"error": "Ungültiger Benutzername oder Passwort"}, status=401)

    async def handle_logout(self, request):
        token = request.headers.get("X-Token", "")
        info  = self.tokens.get(token)
        if info:
            email = info.get("frn_email", "") or info.get("user", "")
            await self._disconnect_user_tx(email)
            del self.tokens[token]
            self._save_tokens()
        return web.json_response({"ok": True})


    async def handle_rooms(self, request):
        token = self._token_from(request)
        if not self._validate_token(token):
            return web.json_response({"error": "unauthorized"}, status=401)
        rooms = [{"mount": m, "name": r.name} for m, r in self.rooms.items()]
        return web.json_response({"rooms": rooms})

    async def handle_config(self, request):
        """Return non-sensitive config for the frontend."""
        ui      = self.cfg.get("ui", {})
        icecast = self.cfg.get("icecast", {})
        # ui.streams in config.json ist optional — Fallback auf tx_rooms.json
        streams = ui.get("streams") or [
            {"name": r.name, "mount": mount, "channel": f"CH-{i+1:02d}"}
            for i, (mount, r) in enumerate(self.rooms.items())
        ]
        return web.json_response({
            "title":        ui.get("title",    "FRN Webstreams"),
            "subtitle":     ui.get("subtitle", "Free Radio Network"),
            "streams":      streams,
            "icecast_host": icecast.get("host", "localhost"),
            "icecast_port": icecast.get("port", 8000),
            "tx_timeout":   self.cfg.get("frn", {}).get("tx_timeout", 180),
            "voice_enabled": bool(self.cfg.get("voice", {}).get("enabled", False)
                                  and (self.cfg.get("voice", {}).get("remote_url") or "").strip()),
        })

    # ── Admin API handlers ─────────────────────────────────────────────────

    async def handle_admin_users_list(self, request):
        _, err = await self._require_admin(request)
        if err:
            return err
        return web.json_response({"users": [
            {"username":     u,
             "callsign":     d["callsign"],
             "is_admin":     d.get("is_admin", False),
             "default_room": d.get("default_room", ""),
             "frn_only":     d.get("frn_only", False)}
            for u, d in self.users.items()
        ]})

    async def handle_admin_users_create(self, request):
        _, err = await self._require_admin(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)

        username = body.get("username", "").strip().lower()
        callsign = body.get("callsign", "").strip()
        password = body.get("password", "")
        is_admin = bool(body.get("is_admin", False))
        frn_only = bool(body.get("frn_only", False))

        if not username:
            return web.json_response({"error": "username required"}, status=400)
        if not frn_only:
            if not password:
                return web.json_response({"error": "password required"}, status=400)
            if len(password) < 4:
                return web.json_response({"error": "password too short (min 4)"}, status=400)

        entry = {
            "callsign":     callsign or username.split("@")[0].upper(),
            "is_admin":     is_admin,
            "default_room": body.get("default_room", ""),
            "frn_only":     frn_only,
        }
        if not frn_only:
            entry["password_hash"] = hash_password(password)
        self.users[username] = entry
        self._save_users()
        log.info("Admin: created user '%s'", username)
        return web.json_response({"ok": True})

    async def handle_admin_users_update(self, request):
        _, err = await self._require_admin(request)
        if err:
            return err
        username = request.match_info["username"].lower()
        if username not in self.users:
            return web.json_response({"error": "not found"}, status=404)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)

        u = self.users[username]
        if "callsign" in body:
            u["callsign"] = body["callsign"].strip()
        if "password" in body and body["password"]:
            if len(body["password"]) < 4:
                return web.json_response({"error": "password too short"}, status=400)
            u["password_hash"] = hash_password(body["password"])
        if "is_admin" in body:
            u["is_admin"] = bool(body["is_admin"])
        if "default_room" in body:
            u["default_room"] = body["default_room"]
        if "callsign" in body and body["callsign"]:
            u["callsign"] = body["callsign"].strip()

        self._save_users()
        log.info("Admin: updated user '%s'", username)
        return web.json_response({"ok": True})

    async def handle_admin_users_delete(self, request):
        info, err = await self._require_admin(request)
        if err:
            return err
        username = request.match_info["username"].lower()
        if username == info["user"]:
            return web.json_response({"error": "Eigenen Account nicht löschbar"}, status=400)
        if username not in self.users:
            return web.json_response({"error": "not found"}, status=404)
        del self.users[username]
        # invalidate any active tokens for this user
        to_del = [t for t, v in self.tokens.items() if v["user"] == username]
        for t in to_del:
            del self.tokens[t]
        self._save_users()
        log.info("Admin: deleted user '%s'", username)
        return web.json_response({"ok": True})

    async def handle_admin_rooms_list(self, request):
        _, err = await self._require_admin(request)
        if err:
            return err
        result = []
        for mount, r in self.rooms.items():
            d = r.to_dict()
            d["mount"] = mount
            result.append(d)
        return web.json_response({"rooms": result})

    async def handle_admin_rooms_create(self, request):
        _, err = await self._require_admin(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)

        mount    = body.get("mount", "").strip().lower()
        name     = body.get("name", "").strip()
        callsign = body.get("callsign", "").strip()
        email    = body.get("email", "").strip()
        password = body.get("password", "").strip()
        frn_srv  = body.get("frn_server", self.args.frn_server)
        frn_port = int(body.get("frn_port", self.args.frn_port))

        if not all([mount, name, email, password]):
            return web.json_response(
                {"error": "mount, name, email, password required"}, status=400)
        if not re.match(r"^[a-z0-9_-]+$", mount):
            return web.json_response(
                {"error": "mount: only a-z, 0-9, _ and - allowed"}, status=400)
        if mount in self.rooms:
            return web.json_response({"error": f"mount '{mount}' already exists"}, status=409)

        room = FRNTXRoom(
            name=name, frn_server=frn_srv, frn_port=frn_port,
            email=email, password=password,
            callsign=callsign or f"TX-{mount.title()}")
        self._set_room_callback(room)
        self.rooms[mount] = room
        self._save_rooms()
        log.info("Admin: created room '%s' → /%s", name, mount)
        return web.json_response({"ok": True})

    async def handle_admin_rooms_delete(self, request):
        _, err = await self._require_admin(request)
        if err:
            return err
        mount = request.match_info["mount"]
        if mount not in self.rooms:
            return web.json_response({"error": "not found"}, status=404)
        room = self.rooms.pop(mount)
        asyncio.create_task(room.disconnect())
        self._save_rooms()
        log.info("Admin: deleted room '%s'", mount)
        return web.json_response({"ok": True})

    async def handle_admin_status(self, request):
        _, err = await self._require_admin(request)
        if err:
            return err
        return web.json_response({
            "rooms": [
                {
                    "mount":     mount,
                    "name":      r.name,
                    "connected": r._connected,
                    "tx_locked": r._tx_lock.locked(),
                }
                for mount, r in self.rooms.items()
            ],
            "active_tokens": len(self.tokens),
            "users":         len(self.users),
            "frn_server":    self.args.frn_server,
            "frn_port":      self.args.frn_port,
        })

    async def handle_admin_debug_traces(self, request):
        """GET /api/admin/debug-traces — Ablaufverfolgung fuer die letzten
        gehoerten Durchsagen: pro Durchsage eine Spur mit einzelnen Schritten
        (Aufnahme/Whisper/Bot-Trigger/Websuche/LLM/TTS+Senden), je mit Status
        (ok/warn/error/skip) und Zeitmessung, neueste zuerst."""
        _, err = await self._require_admin(request)
        if err:
            return err
        traces = []
        for tr in self._debug_traces:
            traces.append({
                "room":     tr["room"],
                "ts":       tr["ts"],
                "started":  tr["started"],
                "done":     tr["done"],
                "total_s":  tr["total_s"],
                "steps":    tr["steps"],
            })
        return web.json_response({"traces": traces})

    async def handle_admin_debug_audio(self, request):
        """GET /api/admin/debug-audio/{name} — spielt die WAV-Aufnahme zu
        einem "Aufnahme"-Schritt im Debug-Panel ab. Name wird auf den reinen
        Dateinamen reduziert (kein Pfad-Escape aus wav_dir möglich)."""
        _, err = await self._require_admin(request)
        if err:
            return err
        name = Path(request.match_info.get("name", "")).name
        if not name or not name.lower().endswith(".wav"):
            return web.json_response({"error": "ungültiger Dateiname"}, status=400)
        wav_dir = Path(self.cfg.get("transcription", {}).get(
            "wav_dir", "/opt/FRN/recordings"))
        wav_path = wav_dir / name
        if not wav_path.exists():
            return web.json_response({"error": "Aufnahme nicht mehr vorhanden"},
                                     status=404)
        return web.FileResponse(wav_path)

    async def handle_admin_overview(self, request):
        """GET /api/admin/overview — alle wichtigen Einstellungen auf einen
        Blick (live aus config.json), gruppiert, mit Angabe wo man ändert.

        Reine Lese-Ansicht — geändert wird weiter im jeweiligen Fach-Reiter.
        Passwörter/Tokens werden NICHT ausgegeben.
        """
        _, err = await self._require_admin(request)
        if err:
            return err
        cfg   = self.cfg
        voice = cfg.get("voice", {})
        bot   = {**self._BOT_DEFAULTS, **voice.get("bot", {})}
        ar    = {**self._AUTO_REPLY_DEFAULTS, **voice.get("auto_reply", {})}
        wh    = cfg.get("whisper", {})

        def _host(url: str) -> str:
            return re.sub(r"^https?://", "", (url or "").strip()).rstrip("/") \
                   or "—"

        groups = [
            {"title": "KI-Funker (Robert)", "tab": "bot", "icon": "🤖",
             "items": [
                ("Aktiv",            "AN" if bot.get("enabled") else "AUS",
                                     bool(bot.get("enabled"))),
                ("Name",             bot.get("name"), None),
                ("Trigger-Wörter",   ", ".join(bot.get("trigger", [])), None),
                ("Stimme",           bot.get("speaker"), None),
                ("KI-Modell",        bot.get("ollama_model"), None),
                ("KI-Server",        _host(bot.get("ollama_url")), None),
                ("Cooldown",         f"{int(bot.get('cooldown_s', 0))} s", None),
                ("Gesprächsfenster", f"{int(bot.get('conversation_window_s', 0))} s", None),
                ("Räume",            ", ".join(bot.get("rooms")) or "alle", None),
             ]},
            {"title": "Automatik (Namens-Antwort)", "tab": "autoreply", "icon": "💬",
             "items": [
                ("Aktiv",       "AN" if ar.get("enabled") else "AUS",
                                bool(ar.get("enabled"))),
                ("Trigger-Namen", ", ".join(ar.get("names", [])), None),
                ("KI-Modell",   ar.get("ollama_model"), None),
                ("KI-Server",   _host(ar.get("ollama_url")), None),
                ("Cooldown",    f"{int(ar.get('cooldown_s', 0))} s", None),
             ]},
            {"title": "Stimme (Text-zu-Sprache)", "tab": None, "icon": "🎙",
             "items": [
                ("Aktiv",   "AN" if voice.get("enabled") else "AUS",
                            bool(voice.get("enabled"))),
                ("Sprache", voice.get("language", "de"), None),
                ("TTS-Server", _host(voice.get("remote_url")), None),
             ]},
            {"title": "Transkription (Whisper)", "tab": None, "icon": "📝",
             "items": [
                ("Modell",  wh.get("model", "—"), None),
                ("Sprache", wh.get("language", "de"), None),
                ("Whisper-Server", _host(wh.get("remote_url")) + " (leer = lokal auf dem Pi)"
                                   if not wh.get("remote_url") else _host(wh.get("remote_url")),
                                   None),
             ]},
            {"title": "FRN-Server & Räume", "tab": "server", "icon": "📡",
             "items": [
                ("FRN-Server", f"{self.args.frn_server}:{self.args.frn_port}", None),
                ("Räume", ", ".join(
                    f"{r.name}{'●' if r._connected else '○'}"
                    for r in self.rooms.values()) or "—", None),
                ("Benutzer", str(len(self.users)), None),
             ]},
        ]
        return web.json_response({"groups": groups})

    async def handle_admin_server_get(self, request):
        """GET /api/admin/server — return current FRN server."""
        _, err = await self._require_admin(request)
        if err:
            return err
        return web.json_response({
            "frn_server": self.args.frn_server,
            "frn_port":   self.args.frn_port,
        })

    async def handle_admin_server_set(self, request):
        """POST /api/admin/server — switch all rooms to a different FRN server."""
        _, err = await self._require_admin(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        new_host = str(body.get("frn_server", "")).strip()
        new_port = body.get("frn_port", self.args.frn_port)
        try:
            new_port = int(new_port)
            if not (1 <= new_port <= 65535):
                raise ValueError
        except (ValueError, TypeError):
            return web.json_response({"error": "Ungültiger Port"}, status=400)
        if not new_host:
            return web.json_response({"error": "Kein Server angegeben"}, status=400)

        new_email    = str(body.get("frn_email",    "")).strip() or None
        new_password = str(body.get("frn_password", "")).strip() or None

        old_host = self.args.frn_server
        old_port = self.args.frn_port
        same_server = (new_host == old_host and new_port == old_port)
        if same_server and not new_email and not new_password:
            return web.json_response({"status": "unchanged"})

        log.info("Admin: FRN-Server wechsel %s:%d → %s:%d%s",
                 old_host, old_port, new_host, new_port,
                 f" (credentials: {new_email})" if new_email else "")

        self.args.frn_server = new_host
        self.args.frn_port   = new_port

        # Reconnect all rooms that use the old default server
        for mount, room in list(self.rooms.items()):
            if room.server == old_host and room.port == old_port:
                room.server = new_host
                room.port   = new_port
                if new_email:
                    room.email    = new_email
                if new_password:
                    room.password = new_password
                room._connected = False
                if room._reader_task:
                    room._reader_task.cancel()
                if room._keepalive_task:
                    room._keepalive_task.cancel()
                if room._writer:
                    try:
                        room._writer.close()
                    except Exception:
                        pass
                asyncio.create_task(room.ensure_connected())
                log.info("Raum %s → neu verbinden mit %s:%d%s",
                         mount, new_host, new_port,
                         f" als {new_email}" if new_email else "")

        return web.json_response({
            "status":     "switching",
            "frn_server": new_host,
            "frn_port":   new_port,
        })

    @staticmethod
    def _build_frn_register_msg(callsign: str, name: str, email: str,
                                 city: str, description: str = "FRN WebTX",
                                 band_channel: str = "PC Only",
                                 country: str = "Germany") -> bytes:
        on_field   = f"{callsign}, {name}"
        city_field = f"{city} - -"
        fields = [
            (0, email), (2, on_field), (3, band_channel),
            (4, description), (5, country), (6, city_field),
        ]
        u8tf = "".join(f"{fid:X}{len(v):02X}{v}" for fid, v in fields)
        msg = (
            f"IG:<ON>{on_field}</ON><EA>{email}</EA>"
            f"<BC>{band_channel}</BC><DS>{description}</DS>"
            f"<NN>{country}</NN><CT>{city_field}</CT>"
            f"<U8TF>{u8tf}</U8TF>\r\n"
        )
        return msg.encode()

    async def handle_admin_register(self, request):
        """POST /api/admin/register — request a new FRN account password via sysman."""
        _, err = await self._require_admin(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        callsign = str(body.get("callsign", "")).strip().upper()
        name     = str(body.get("name",     "")).strip()
        email    = str(body.get("email",    "")).strip()
        city     = str(body.get("city",     "")).strip()
        if not callsign or not name or not email or not city:
            return web.json_response({"error": "callsign, name, email und city sind Pflichtfelder"}, status=400)

        # Prüfe ob die MX-Server der Email-Domain per IPv4 erreichbar sind (Sysman ist IPv4-only)
        email_domain = email.split("@")[-1]
        ipv4_warning = None
        try:
            import socket as _socket, struct as _struct

            def _dns_mx(domain):
                """Minimaler DNS-MX-Query (UDP, kein dnspython nötig)."""
                header = b'\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00'
                qname = b''.join(bytes([len(p)]) + p.encode() for p in domain.split('.')) + b'\x00'
                s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
                s.settimeout(3)
                try:
                    s.sendto(header + qname + b'\x00\x0f\x00\x01', ('8.8.8.8', 53))
                    resp = s.recv(512)
                finally:
                    s.close()
                # Antwort parsen: Labels nach den Antwort-RRs lesen
                offset = 12  # Header überspringen
                # Question überspringen
                while resp[offset] != 0:
                    offset += resp[offset] + 1
                offset += 5  # 0x00 + QTYPE + QCLASS
                # Antwort-RRs lesen
                hosts = []
                ancount = _struct.unpack('>H', resp[6:8])[0]
                for _ in range(ancount):
                    if offset >= len(resp):
                        break
                    # Name (ggf. pointer)
                    if resp[offset] & 0xC0 == 0xC0:
                        offset += 2
                    else:
                        while resp[offset] != 0:
                            offset += resp[offset] + 1
                        offset += 1
                    rtype, _, _, rdlen = _struct.unpack('>HHIH', resp[offset:offset+10])
                    offset += 10
                    if rtype == 15:  # MX
                        pref_end = offset + 2
                        # MX-Hostname dekodieren (mit pointer support)
                        name, pos = [], pref_end
                        while pos < len(resp) and resp[pos] != 0:
                            if resp[pos] & 0xC0 == 0xC0:
                                ptr = _struct.unpack('>H', resp[pos:pos+2])[0] & 0x3FFF
                                pos = ptr
                            else:
                                length = resp[pos]
                                name.append(resp[pos+1:pos+1+length].decode())
                                pos += 1 + length
                        hosts.append('.'.join(name))
                    offset += rdlen
                return hosts

            mx_hosts = []
            try:
                mx_hosts = _dns_mx(email_domain)
            except Exception:
                pass
            if not mx_hosts:
                mx_hosts = [email_domain]
            has_ipv4 = False
            for mx in mx_hosts:
                try:
                    if _socket.getaddrinfo(mx.rstrip('.'), 25, family=_socket.AF_INET,
                                           type=_socket.SOCK_STREAM):
                        has_ipv4 = True
                        break
                except Exception:
                    pass
            if not has_ipv4:
                ipv4_warning = (f"'{email_domain}' hat keine IPv4-Mailserver — "
                                "der FRN-Sysman kann dort keine E-Mail zustellen. "
                                "Bitte Gmail, iCloud oder einen anderen großen Anbieter verwenden.")
        except Exception:
            pass  # Im Zweifel keine Warnung

        sysman_host = "sysman.freeradionetwork.de"
        sysman_port = 10025
        msg = self._build_frn_register_msg(callsign, name, email, city)
        log.info("FRN-Registrierung: %s <%s> via %s:%d", callsign, email, sysman_host, sysman_port)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(sysman_host, sysman_port), timeout=10
            )
            writer.write(msg)
            await writer.drain()
            response = await asyncio.wait_for(reader.readline(), timeout=10)
            writer.close()
            await writer.wait_closed()
        except Exception as e:
            log.warning("FRN-Registrierung fehlgeschlagen: %s", e)
            return web.json_response({"error": f"Verbindung zum Sysman fehlgeschlagen: {e}"}, status=502)

        resp_text = response.strip().decode(errors="replace")
        log.info("FRN-Sysman Antwort: %r", resp_text)
        if resp_text.upper() == "OK":
            result = {"status": "ok", "email": email}
            if ipv4_warning:
                result["warning"] = ipv4_warning
            return web.json_response(result)
        elif resp_text.upper() == "NOK":
            return web.json_response({
                "error": "Sysman hat abgelehnt (NOK). Mögliche Ursachen: "
                         "Rufzeichen oder E-Mail bereits vergeben, IP-Sperre nach vorheriger Registrierung, "
                         "oder der Account ist gerade online. "
                         "Bitte direkt auf freeradionetwork.de registrieren."
            }, status=400)
        else:
            return web.json_response({"error": resp_text or "Unbekannte Sysman-Antwort"}, status=400)

    async def handle_clips(self, request):
        """Return list of configured quick-send clips (requires valid token)."""
        token = self._token_from(request)
        if not self._validate_token(token):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"clips": self._load_clips()})

    async def handle_clip_recording_upload(self, request):
        """POST /api/clips/{id}/recording — save custom audio for a clip."""
        import re as _re
        token = self._token_from(request)
        if not self._validate_token(token):
            return web.json_response({"error": "unauthorized"}, status=401)

        clip_id = request.match_info["id"]
        if not _re.match(r'^[a-z0-9_]{1,40}$', clip_id):
            return web.json_response({"error": "Ungültige Clip-ID"}, status=400)
        if not any(c["id"] == clip_id for c in self._load_clips()):
            return web.json_response({"error": "Clip nicht gefunden"}, status=404)

        data = await request.read()
        if len(data) < 200:
            return web.json_response({"error": "Audio zu kurz"}, status=400)
        if len(data) > 10 * 1024 * 1024:
            return web.json_response({"error": "Audio zu groß (max 10 MB)"}, status=400)

        self._CLIPS_DIR.mkdir(exist_ok=True)
        tmp  = self._CLIPS_DIR / f"_{clip_id}.tmp"
        dest = self._CLIPS_DIR / f"{clip_id}.wav"
        try:
            tmp.write_bytes(data)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", str(tmp),
                "-ar", "8000", "-ac", "1", str(dest),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        finally:
            tmp.unlink(missing_ok=True)

        if not dest.exists() or dest.stat().st_size < 100:
            return web.json_response({"error": "Konvertierung fehlgeschlagen"}, status=500)

        log.info("Clip '%s': eigene Aufnahme gespeichert (%d Bytes)", clip_id, dest.stat().st_size)
        return web.json_response({"ok": True})

    async def handle_clip_recording_delete(self, request):
        """DELETE /api/clips/{id}/recording — remove custom recording, fall back to TTS."""
        import re as _re
        token = self._token_from(request)
        if not self._validate_token(token):
            return web.json_response({"error": "unauthorized"}, status=401)

        clip_id = request.match_info["id"]
        if not _re.match(r'^[a-z0-9_]{1,40}$', clip_id):
            return web.json_response({"error": "Ungültige Clip-ID"}, status=400)

        recorded = self._CLIPS_DIR / f"{clip_id}.wav"
        if recorded.exists():
            recorded.unlink()
            log.info("Clip '%s': eigene Aufnahme gelöscht → TTS", clip_id)
        return web.json_response({"ok": True})

    async def handle_frn_networks(self, request):
        """Return list of available FRN room names (requires valid token).

        Uses the configured ``frn_stream_account`` credentials to query the FRN
        server, or falls back to the names of already-loaded rooms.
        """
        token = self._token_from(request)
        if not self._validate_token(token):
            return web.json_response({"error": "unauthorized"}, status=401)

        acct     = self.cfg.get("frn_stream_account", {})
        email    = acct.get("email",    "").strip()
        password = acct.get("password", "").strip()

        if email and password:
            networks = await self._fetch_frn_networks(email, password)
        else:
            # Fall back to currently loaded rooms
            networks = [r.name for r in self.rooms.values()]

        return web.json_response({"networks": networks})

    async def handle_stream_proxy(self, request):
        """Proxy Icecast stream → same-origin for Web Audio API (local/direct access)."""
        mount   = request.match_info["mount"]
        icecast = self.cfg.get("icecast", {})
        url     = f"http://{icecast.get('host','localhost')}:{icecast.get('port',8000)}/{mount}.mp3"
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(connect=5, total=None)) as s:
                async with s.get(url) as ice:
                    if ice.status != 200:
                        return web.Response(status=ice.status)
                    resp = web.StreamResponse(headers={
                        "Content-Type": "audio/mpeg",
                        "Cache-Control": "no-cache",
                        "Access-Control-Allow-Origin": "*",
                    })
                    await resp.prepare(request)
                    try:
                        async for chunk in ice.content.iter_chunked(8192):
                            await resp.write(chunk)
                    except (asyncio.CancelledError, ConnectionResetError):
                        pass
                    return resp
        except Exception as e:
            log.debug("Stream proxy [%s]: %s", mount, e)
            return web.Response(status=502, text="Stream unavailable")

    async def handle_room_clients(self, request):
        """Return list of clients currently in a room (requires valid token).

        Triggers a FRN connection for the room if it is not yet connected,
        so the client list arrives as soon as possible.
        """
        token = self._token_from(request)
        if not self._validate_token(token):
            return web.json_response({"error": "unauthorized"}, status=401)
        mount = request.match_info["mount"]
        room  = self.rooms.get(mount)
        if not room:
            return web.json_response({"error": "not found"}, status=404)

        # Ensure we have a live connection so MARKER_CLIENTS updates flow in
        if not room._connected:
            try:
                await asyncio.wait_for(room.ensure_connected(), timeout=5.0)
                # Wait briefly for the initial client-list packet
                for _ in range(15):
                    if room._clients:
                        break
                    await asyncio.sleep(0.2)
            except Exception as e:
                log.debug("room clients connect error [%s]: %s", mount, e)

        return web.json_response({
            "mount":   mount,
            "name":    room.name,
            "clients": [
                {
                    "callsign": c.get("ON", "?"),
                    "desc":     c.get("DS", ""),
                    "type":     c.get("CL", "2"),  # 0=crosslink 1=gateway 2=PC
                }
                for c in room._clients
            ],
        })

    async def handle_rx_ws(self, request):
        """WebSocket endpoint that streams decoded PCM audio from an FRN room.

        The client receives raw s16le PCM frames at 8 kHz mono (3200 bytes each,
        200 ms per frame). Use Web Audio API on the browser side to schedule
        and play the buffers.
        """
        token = self._token_from(request)
        if not self._validate_token(token):
            return web.Response(status=401, text="Unauthorized")

        mount = request.rel_url.query.get("room", "")
        room  = self.rooms.get(mount)
        if not room:
            return web.Response(status=404, text=f"Room '{mount}' not found")

        if room._gsm_dec is None:
            return web.Response(status=503, text="GSM decoder not available")

        ws = web.WebSocketResponse(protocols=("frn",))
        await ws.prepare(request)

        if not room._connected:
            try:
                await asyncio.wait_for(room.ensure_connected(), timeout=5.0)
            except Exception as e:
                await ws.close(message=f"FRN connect failed: {e}".encode())
                return ws

        room._rx_clients.add(ws)
        log.info("RX WS connected: room=%s total_rx=%d", mount, len(room._rx_clients))
        try:
            async for _msg in ws:
                pass   # keep connection alive; client sends nothing
        except Exception:
            pass
        finally:
            room._rx_clients.discard(ws)
            log.info("RX WS closed: room=%s total_rx=%d", mount, len(room._rx_clients))

        return ws

    # ── WebSocket ──────────────────────────────────────────────────────────

    async def handle_ws(self, request):
        token = self._token_from(request)
        info  = self._validate_token(token)
        if not info:
            return web.Response(status=401, text="Unauthorized")

        mount = request.rel_url.query.get("room", "")
        room  = self.rooms.get(mount)
        if not room:
            return web.Response(status=404, text=f"Room '{mount}' not found")

        ws = web.WebSocketResponse(protocols=("frn",))
        await ws.prepare(request)
        callsign = info["callsign"]
        log.info("WS connected: user=%s room=%s", info["user"], mount)

        in_tx        = False
        waiting_tx   = False          # TX beantragt, Genehmigung ausstehend
        pre_buf: list[bytes] = []     # PCM-Puffer während der Wartezeit
        _tx_approval_task = None
        src_rate     = 48000
        native_buf   = np.array([], dtype=np.float32)
        native_block = 960
        block_8k     = 160

        frn_email    = info.get("frn_email", "")
        frn_password = info.get("frn_password", "")
        user_key     = (frn_email, mount) if frn_email else None
        # Bestehende User-TX-Verbindung wiederverwenden (kein Disconnect zwischen Drücken)
        user_tx_conn = self._user_tx_conns.get(user_key) if user_key else None
        tx_conn      = user_tx_conn or room

        room._chat_clients.add(ws)
        self._ws_users[id(ws)] = info["user"].lower()
        try:
            await ws.send_json({"type": "ready", "callsign": callsign,
                                "room": room.name})

            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        data = {"cmd": msg.data}

                    cmd = data.get("cmd", "")

                    if cmd == "PTT_START":
                        src_rate     = int(data.get("sampleRate", 48000))
                        native_block = max(1, int(block_8k * src_rate / 8000))
                        native_buf   = np.array([], dtype=np.float32)
                        pre_buf      = []

                        if room._tx_lock.locked():
                            await ws.send_json({"type": "error",
                                                "msg": "Raum belegt (jemand sendet)"})
                            continue

                        # Lock sofort belegen — kein yield zwischen check und acquire
                        # (asyncio.Lock.acquire() ist atomisch wenn Lock frei ist)
                        await room._tx_lock.acquire()
                        waiting_tx = True
                        await ws.send_json({"type": "tx_waiting"})

                        async def _request_and_notify():
                            nonlocal in_tx, waiting_tx, pre_buf, user_tx_conn, tx_conn
                            ok = False
                            try:
                                try:
                                    # Eigene FRN-Verbindung aufbauen (lazy, Retry bei BLOCK)
                                    if frn_email and frn_password:
                                        # Bestehende Verbindung für diesen Raum wiederverwenden
                                        existing = self._user_tx_conns.get(user_key)
                                        if existing and existing._connected:
                                            user_tx_conn = existing
                                            tx_conn = existing
                                        else:
                                            # Verbindungen für andere Räume (gleiche Email) trennen
                                            for k in list(self._user_tx_conns):
                                                if k[0] == frn_email and k != user_key:
                                                    old = self._user_tx_conns.pop(k)
                                                    try:
                                                        await old.disconnect()
                                                    except Exception:
                                                        pass
                                                    log.info("[%s] User-TX anderer Raum getrennt: %s",
                                                             room.name, k[1])
                                            for attempt in range(4):
                                                try:
                                                    conn = FRNTXRoom(
                                                        name=room.name,
                                                        frn_server=room.server,
                                                        frn_port=room.port,
                                                        email=frn_email,
                                                        password=frn_password,
                                                        callsign=callsign,
                                                    )
                                                    await conn.ensure_connected()
                                                    user_tx_conn = conn
                                                    tx_conn = conn
                                                    self._user_tx_conns[user_key] = conn
                                                    log.info("[%s] User-TX verbunden: %s (%s)",
                                                             room.name, info["user"], callsign)
                                                    break
                                                except Exception as e:
                                                    if "BLOCK" in str(e) and attempt < 3:
                                                        log.info("[%s] AL=BLOCK — warte 8s (Versuch %d/4)",
                                                                 room.name, attempt + 1)
                                                        await asyncio.sleep(8)
                                                    else:
                                                        log.warning("[%s] User-TX fehlgeschlagen (%s) — Fallback",
                                                                    room.name, e)
                                                        break
                                    ok = await tx_conn.request_tx()
                                finally:
                                    room._tx_lock.release()
                                    waiting_tx = False

                                if ok:
                                    in_tx = True
                                    for chunk in pre_buf:
                                        await tx_conn.send_pcm(chunk)
                                    pre_buf = []
                                    try:
                                        await ws.send_json({"type": "tx_active", "beep": True})
                                    except Exception:
                                        # WS already closed — release TX immediately
                                        await tx_conn.end_tx()
                                        in_tx = False
                                elif not asyncio.current_task().cancelled():
                                    pre_buf = []
                                    await ws.send_json({"type": "error",
                                                        "msg": "TX nicht genehmigt (Kanal belegt)"})
                            except asyncio.CancelledError:
                                raise
                            except Exception as e:
                                log.warning("[%s] TX-Task Fehler: %s", room.name, e)
                                try:
                                    await ws.send_json({"type": "error", "msg": f"TX-Fehler: {e}"})
                                except Exception:
                                    pass

                        _tx_approval_task = asyncio.create_task(_request_and_notify())

                    elif cmd == "PTT_STOP":
                        was_active = in_tx or waiting_tx
                        waiting_tx = False
                        if _tx_approval_task and not _tx_approval_task.done():
                            _tx_approval_task.cancel()
                            _tx_approval_task = None
                        if in_tx:
                            await tx_conn.end_tx()
                            in_tx = False
                        if was_active:
                            await ws.send_json({"type": "tx_stopped"})

                    elif cmd == "CHAT":
                        text = str(data.get("text", "")).strip()
                        if text:
                            await room.ensure_connected()
                            await room.send_text(text)
                            # Echo back to all chat clients including sender
                            await room._dispatch_message(callsign, text)

                    elif cmd == "PLAY_CLIP":
                        if in_tx or waiting_tx:
                            await ws.send_json({"type": "error",
                                                "msg": "PTT aktiv — bitte erst loslassen"})
                            continue
                        if room._tx_lock.locked():
                            await ws.send_json({"type": "error",
                                                "msg": "Raum belegt (jemand sendet)"})
                            continue

                        clip_id   = data.get("id", "")
                        clip_list = self._load_clips()
                        clip      = next((c for c in clip_list if c["id"] == clip_id), None)
                        if not clip:
                            await ws.send_json({"type": "error",
                                                "msg": f"Clip '{clip_id}' nicht gefunden"})
                            continue

                        clip_text = clip["text"].replace("{callsign}", callsign)
                        clip_lang = "de"

                        await room._tx_lock.acquire()
                        waiting_tx = True
                        await ws.send_json({"type": "tx_waiting"})

                        async def _play_clip_task(ct=clip_text, cl=clip_lang):
                            nonlocal in_tx, waiting_tx, user_tx_conn, tx_conn
                            ok = False
                            try:
                                try:
                                    if frn_email and frn_password:
                                        existing = self._user_tx_conns.get(user_key)
                                        if existing and existing._connected:
                                            user_tx_conn = existing
                                            tx_conn = existing
                                        else:
                                            for k in list(self._user_tx_conns):
                                                if k[0] == frn_email and k != user_key:
                                                    old = self._user_tx_conns.pop(k)
                                                    try:
                                                        await old.disconnect()
                                                    except Exception:
                                                        pass
                                            for attempt in range(4):
                                                try:
                                                    conn = FRNTXRoom(
                                                        name=room.name,
                                                        frn_server=room.server,
                                                        frn_port=room.port,
                                                        email=frn_email,
                                                        password=frn_password,
                                                        callsign=callsign,
                                                    )
                                                    await conn.ensure_connected()
                                                    user_tx_conn = conn
                                                    tx_conn = conn
                                                    self._user_tx_conns[user_key] = conn
                                                    break
                                                except Exception as e:
                                                    if "BLOCK" in str(e) and attempt < 3:
                                                        await asyncio.sleep(8)
                                                    else:
                                                        break
                                    ok = await tx_conn.request_tx(timeout=10.0)
                                finally:
                                    room._tx_lock.release()
                                    waiting_tx = False

                                if ok:
                                    in_tx = True
                                    await ws.send_json({"type": "tx_active", "beep": True})
                                    try:
                                        pcm = await self._get_clip_pcm(clip_id, ct, cl)
                                        for i in range(0, len(pcm), PCM_PACKET_BYTES):
                                            await tx_conn.send_pcm(pcm[i:i + PCM_PACKET_BYTES])
                                            await asyncio.sleep(PCM_PACKET_BYTES / (8000 * 2))
                                    except asyncio.CancelledError:
                                        raise  # PTT_STOP übernimmt end_tx + tx_stopped
                                    except Exception as e:
                                        log.warning("[%s] Clip-PCM-Fehler: %s", room.name, e)
                                    finally:
                                        if in_tx:
                                            await tx_conn.end_tx()
                                            in_tx = False
                                    if not asyncio.current_task().cancelled():
                                        await ws.send_json({"type": "tx_stopped"})
                                elif not asyncio.current_task().cancelled():
                                    await ws.send_json({"type": "error",
                                                        "msg": "TX nicht genehmigt (Kanal belegt)"})
                            except asyncio.CancelledError:
                                raise
                            except Exception as e:
                                log.warning("[%s] Clip-Task Fehler: %s", room.name, e)
                                try:
                                    await ws.send_json({"type": "error", "msg": f"TX-Fehler: {e}"})
                                except Exception:
                                    pass

                        _tx_approval_task = asyncio.create_task(_play_clip_task())

                    elif cmd == "SPEAK_VOICE":
                        if in_tx or waiting_tx:
                            await ws.send_json({"type": "error",
                                                "msg": "PTT aktiv — bitte erst loslassen"})
                            continue
                        if room._tx_lock.locked():
                            await ws.send_json({"type": "error",
                                                "msg": "Raum belegt (jemand sendet)"})
                            continue
                        vcfg = self.cfg.get("voice", {})
                        if not (vcfg.get("enabled", False)
                                and (vcfg.get("xtts_url") or "").strip()):
                            await ws.send_json({"type": "error",
                                                "msg": "Sprach-Funktion ist deaktiviert"})
                            continue

                        voice_text = (data.get("text") or "").strip()
                        if not voice_text:
                            await ws.send_json({"type": "error", "msg": "Kein Text"})
                            continue
                        if len(voice_text) > 500:
                            voice_text = voice_text[:500]
                        voice_lang = vcfg.get("language", "de")

                        await room._tx_lock.acquire()
                        waiting_tx = True
                        await ws.send_json({"type": "voice_synth"})

                        async def _speak_voice_task(vt=voice_text, vl=voice_lang):
                            nonlocal in_tx, waiting_tx, user_tx_conn, tx_conn
                            ok = False
                            try:
                                # Stimme ERST synthetisieren (dauert einige Sekunden), bevor
                                # der Sender getastet wird — sonst toter Träger während TTS.
                                try:
                                    pcm = await self._get_voice_pcm(
                                        vt, vl, self._user_speaker(info["user"]),
                                        force_xtts=True)
                                except Exception as e:
                                    room._tx_lock.release()
                                    waiting_tx = False
                                    log.warning("[%s] Voice-TTS-Fehler: %s", room.name, e)
                                    if not asyncio.current_task().cancelled():
                                        await ws.send_json({"type": "error",
                                                            "msg": f"Sprach-Fehler: {e}"})
                                    return

                                await ws.send_json({"type": "tx_waiting"})
                                try:
                                    if frn_email and frn_password:
                                        existing = self._user_tx_conns.get(user_key)
                                        if existing and existing._connected:
                                            user_tx_conn = existing
                                            tx_conn = existing
                                        else:
                                            for k in list(self._user_tx_conns):
                                                if k[0] == frn_email and k != user_key:
                                                    old = self._user_tx_conns.pop(k)
                                                    try:
                                                        await old.disconnect()
                                                    except Exception:
                                                        pass
                                            for attempt in range(4):
                                                try:
                                                    conn = FRNTXRoom(
                                                        name=room.name,
                                                        frn_server=room.server,
                                                        frn_port=room.port,
                                                        email=frn_email,
                                                        password=frn_password,
                                                        callsign=callsign,
                                                    )
                                                    await conn.ensure_connected()
                                                    user_tx_conn = conn
                                                    tx_conn = conn
                                                    self._user_tx_conns[user_key] = conn
                                                    break
                                                except Exception as e:
                                                    if "BLOCK" in str(e) and attempt < 3:
                                                        await asyncio.sleep(8)
                                                    else:
                                                        break
                                    ok = await tx_conn.request_tx(timeout=10.0)
                                finally:
                                    room._tx_lock.release()
                                    waiting_tx = False

                                if ok:
                                    in_tx = True
                                    await ws.send_json({"type": "tx_active", "beep": True})
                                    try:
                                        for i in range(0, len(pcm), PCM_PACKET_BYTES):
                                            await tx_conn.send_pcm(pcm[i:i + PCM_PACKET_BYTES])
                                            await asyncio.sleep(PCM_PACKET_BYTES / (8000 * 2))
                                    except asyncio.CancelledError:
                                        raise
                                    except Exception as e:
                                        log.warning("[%s] Voice-PCM-Fehler: %s", room.name, e)
                                    finally:
                                        if in_tx:
                                            await tx_conn.end_tx()
                                            in_tx = False
                                    if not asyncio.current_task().cancelled():
                                        await ws.send_json({"type": "tx_stopped"})
                                elif not asyncio.current_task().cancelled():
                                    await ws.send_json({"type": "error",
                                                        "msg": "TX nicht genehmigt (Kanal belegt)"})
                            except asyncio.CancelledError:
                                raise
                            except Exception as e:
                                log.warning("[%s] Voice-Task Fehler: %s", room.name, e)
                                try:
                                    await ws.send_json({"type": "error", "msg": f"TX-Fehler: {e}"})
                                except Exception:
                                    pass

                        _tx_approval_task = asyncio.create_task(_speak_voice_task())

                elif msg.type == web.WSMsgType.BINARY:
                    if not in_tx and not waiting_tx:
                        continue
                    pcm_in = np.frombuffer(msg.data, dtype="<i2").astype(np.float32)
                    if src_rate == 8000:
                        pcm_8k = pcm_in.astype("<i2").tobytes()
                        if waiting_tx:
                            pre_buf.append(pcm_8k)
                        else:
                            await tx_conn.send_pcm(pcm_8k)
                    else:
                        native_buf = np.append(native_buf, pcm_in)
                        while len(native_buf) >= native_block:
                            chunk      = native_buf[:native_block]
                            native_buf = native_buf[native_block:]
                            resampled  = sp_resample(chunk, block_8k)
                            pcm_bytes  = np.clip(resampled, -32768, 32767).astype("<i2").tobytes()
                            if waiting_tx:
                                pre_buf.append(pcm_bytes)
                            else:
                                await tx_conn.send_pcm(pcm_bytes)

                elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break

        except Exception as e:
            log.error("WS error: %s", e)
        finally:
            # Cancel pending TX-approval task first
            if _tx_approval_task and not _tx_approval_task.done():
                _tx_approval_task.cancel()
            # Send RX0 if TX0 was already sent but WS closed before PTT_STOP.
            # This covers both: in_tx (TX active) and waiting_tx (TX0 sent,
            # TX_APPROVE not yet received). Without RX0 the FRN server keeps
            # the client locked in TX-pending state and won't approve the next TX0.
            if in_tx or waiting_tx:
                try:
                    await tx_conn.end_tx()
                except Exception:
                    pass
            # user_tx_conn bleibt am Leben (in self._user_tx_conns) für nächsten PTT-Druck
            room._chat_clients.discard(ws)
            self._ws_users.pop(id(ws), None)
            log.info("WS closed: user=%s", info["user"])

        return ws

    # ── Archiv-Handler ─────────────────────────────────────────────────────

    async def handle_archive_page(self, request):
        html_path = Path(__file__).parent / "archive_page.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="Archive page not found.", content_type="text/html")

    async def handle_debug_page(self, request):
        """GET /debug -- eigenstaendige Seite fuer die Ablaufverfolgung
        (Login + API-Aufrufe wie das DEBUG-Reiter im Hauptpanel, nur als
        eigenes Fenster/Tab nutzbar, siehe debug_page.html)."""
        html_path = Path(__file__).parent / "debug_page.html"
        if html_path.exists():
            return web.FileResponse(html_path)
        return web.Response(text="Debug page not found.", content_type="text/html")

    async def handle_archive_api(self, request):
        if not _ARCHIVE_AVAILABLE:
            return web.json_response({"error": "archive not available"}, status=503)
        q      = request.rel_url.query
        limit  = min(int(q.get("limit",  100)), 500)
        offset = int(q.get("offset", 0))
        room   = q.get("room",   "")
        search = q.get("search", "")
        date_from = q.get("from", "")
        date_to   = q.get("to",   "")
        loop = asyncio.get_running_loop()
        entries, total = await loop.run_in_executor(
            None, _archive.query_entries, limit, offset, room, search, date_from, date_to
        )
        rooms = await loop.run_in_executor(None, _archive.get_rooms)
        # Warteschlange: .meta-Dateien die noch nicht transkribiert wurden
        wav_dir = Path(self.cfg.get("transcription", {}).get("wav_dir", "/opt/FRN/recordings"))
        pending = len(list(wav_dir.glob("*.meta")))
        return web.json_response({"entries": entries, "total": total, "rooms": rooms,
                                  "pending": pending})

    async def handle_archive_stats(self, request):
        if not _ARCHIVE_AVAILABLE:
            return web.json_response({"error": "archive not available"}, status=503)
        days = int(request.rel_url.query.get("days", 30))
        loop = asyncio.get_running_loop()
        stats = await loop.run_in_executor(None, _archive.get_stats, days)
        return web.json_response(stats)

    async def handle_archive_comments_get(self, request):
        if not _ARCHIVE_AVAILABLE:
            return web.json_response({"error": "archive not available"}, status=503)
        entry_id = int(request.match_info["id"])
        loop = asyncio.get_running_loop()
        comments = await loop.run_in_executor(None, _archive.get_comments, entry_id)
        return web.json_response({"comments": comments})

    async def handle_archive_comments_post(self, request):
        if not _ARCHIVE_AVAILABLE:
            return web.json_response({"error": "archive not available"}, status=503)
        entry_id = int(request.match_info["id"])
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        text = str(body.get("text", "")).strip()
        if not text:
            return web.json_response({"error": "text required"}, status=400)
        if len(text) > 500:
            return web.json_response({"error": "text too long (max 500)"}, status=400)
        loop = asyncio.get_running_loop()
        cid = await loop.run_in_executor(None, _archive.add_comment, entry_id, text)
        return web.json_response({"ok": True, "id": cid})

    async def handle_archive_chat_api(self, request):
        if not _ARCHIVE_AVAILABLE:
            return web.json_response({"error": "archive not available"}, status=503)
        q      = request.rel_url.query
        limit  = min(int(q.get("limit", 100)), 500)
        offset = int(q.get("offset", 0))
        room   = q.get("room",   "")
        search = q.get("search", "")
        loop = asyncio.get_running_loop()
        messages, total = await loop.run_in_executor(
            None, _archive.query_chat_messages, limit, offset, room, search
        )
        rooms = await loop.run_in_executor(None, _archive.get_chat_rooms)
        return web.json_response({"messages": messages, "total": total, "rooms": rooms})

    async def handle_archive_audio(self, request):
        if not _ARCHIVE_AVAILABLE:
            return web.Response(status=503)
        filename  = request.match_info["filename"]
        # Sicherheit: kein Pfad-Traversal
        if "/" in filename or "\\" in filename or ".." in filename:
            return web.Response(status=400)
        audio_path = _archive.AUDIO_DIR / filename
        if not audio_path.exists():
            return web.Response(status=404)
        return web.FileResponse(audio_path, headers={"Content-Type": "audio/ogg"})

    # ── CORS middleware ────────────────────────────────────────────────────

    @web.middleware
    async def cors_middleware(self, request, handler):
        if request.method == "OPTIONS":
            return web.Response(headers={
                "Access-Control-Allow-Origin":  "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, X-Token",
            })
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    # ── App ────────────────────────────────────────────────────────────────

    async def _on_startup(self, _app):
        await self._discover_rooms()

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self.cors_middleware])
        app.router.add_route("OPTIONS", "/{path:.*}", lambda r: web.Response())
        app.on_startup.append(self._on_startup)

        # Public
        app.router.add_get ("/",                   self.handle_root)
        app.router.add_get ("/tx_processor.js",    self.handle_worklet)
        app.router.add_post("/api/login",           self.handle_login)
        app.router.add_post("/api/logout",          self.handle_logout)
        app.router.add_get ("/api/rooms",           self.handle_rooms)
        app.router.add_get ("/api/config",          self.handle_config)
        app.router.add_get ("/stream/{mount}.mp3",            self.handle_stream_proxy)
        app.router.add_get   ("/api/clips",                          self.handle_clips)
        app.router.add_post  ("/api/clips/{id}/recording",          self.handle_clip_recording_upload)
        app.router.add_delete("/api/clips/{id}/recording",          self.handle_clip_recording_delete)
        app.router.add_get ("/api/frn-networks",              self.handle_frn_networks)
        app.router.add_get ("/api/rooms/{mount}/clients",    self.handle_room_clients)
        app.router.add_get ("/api/voice/auto-reply",         self.handle_voice_auto_reply)
        app.router.add_post("/api/voice/auto-reply",         self.handle_voice_auto_reply)
        app.router.add_get ("/api/voice/my-settings",        self.handle_voice_my_settings)
        app.router.add_post("/api/voice/my-settings",        self.handle_voice_my_settings)
        app.router.add_post("/api/voice/sample",             self.handle_voice_sample_upload)
        app.router.add_post("/api/voice/preview",            self.handle_voice_preview)
        app.router.add_get ("/ws",                           self.handle_ws)
        app.router.add_get ("/rx",                           self.handle_rx_ws)

        # Debug (eigene Seite -- User-Wunsch 2026-08-13, war nur Reiter im
        # Hauptpanel und liess sich schwer als eigenes Fenster/Tab offen halten)
        app.router.add_get("/debug",                          self.handle_debug_page)

        # Archiv
        app.router.add_get("/archive",                        self.handle_archive_page)
        app.router.add_get("/api/archive",                    self.handle_archive_api)
        app.router.add_get("/api/archive/audio/{filename}",   self.handle_archive_audio)
        app.router.add_get("/api/archive/chat",               self.handle_archive_chat_api)
        app.router.add_get("/api/archive/stats",              self.handle_archive_stats)
        app.router.add_get ("/api/archive/{id}/comments",    self.handle_archive_comments_get)
        app.router.add_post("/api/archive/{id}/comments",    self.handle_archive_comments_post)

        # Admin (require token + is_admin)
        app.router.add_get   ("/api/admin/users",           self.handle_admin_users_list)
        app.router.add_post  ("/api/admin/users",           self.handle_admin_users_create)
        app.router.add_put   ("/api/admin/users/{username}", self.handle_admin_users_update)
        app.router.add_patch ("/api/admin/users/{username}", self.handle_admin_users_update)
        app.router.add_delete("/api/admin/users/{username}", self.handle_admin_users_delete)

        app.router.add_get   ("/api/admin/rooms",         self.handle_admin_rooms_list)
        app.router.add_post  ("/api/admin/rooms",         self.handle_admin_rooms_create)
        app.router.add_delete("/api/admin/rooms/{mount}", self.handle_admin_rooms_delete)

        app.router.add_get("/api/admin/status", self.handle_admin_status)
        app.router.add_get("/api/admin/debug-traces", self.handle_admin_debug_traces)
        app.router.add_get("/api/admin/debug-audio/{name}", self.handle_admin_debug_audio)
        app.router.add_get("/api/admin/overview", self.handle_admin_overview)

        app.router.add_get ("/api/admin/server",   self.handle_admin_server_get)
        app.router.add_post("/api/admin/server",   self.handle_admin_server_set)
        app.router.add_post("/api/admin/register", self.handle_admin_register)

        app.router.add_get ("/api/admin/auto-reply",        self.handle_admin_auto_reply)
        app.router.add_post("/api/admin/auto-reply",        self.handle_admin_auto_reply)
        app.router.add_get ("/api/admin/auto-reply/models", self.handle_admin_auto_reply_models)
        app.router.add_post("/api/admin/auto-reply/test",   self.handle_admin_auto_reply_test)
        app.router.add_get ("/api/admin/bot",       self.handle_admin_bot)
        app.router.add_post("/api/admin/bot",       self.handle_admin_bot)
        app.router.add_post("/api/admin/bot/test",  self.handle_admin_bot_test)
        app.router.add_get ("/api/admin/tts",       self.handle_admin_tts)
        app.router.add_post("/api/admin/tts",       self.handle_admin_tts)
        app.router.add_get ("/api/admin/crosslink", self.handle_admin_crosslink)
        app.router.add_post("/api/admin/crosslink", self.handle_admin_crosslink)
        app.router.add_get ("/api/admin/gemini-models", self.handle_admin_gemini_models)

        return app


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="FRN Web TX Server")
    parser.add_argument("--config",     default=None,
                        help="Path to config.json (overrides defaults)")
    parser.add_argument("--host",       default="0.0.0.0")
    parser.add_argument("--port",       type=int, default=8765)
    parser.add_argument("--frn-server", default="localhost")
    parser.add_argument("--frn-port",   type=int, default=10024)
    parser.add_argument("--users",
        default=str(Path(__file__).parent.parent / "config" / "tx_users.json"))
    parser.add_argument("--rooms",
        default=str(Path(__file__).parent.parent / "config" / "tx_rooms.json"))
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
    )

    server = TXServer(args)
    server.load_config()
    server.load_users()
    server.load_rooms()

    # ── Transkriptions-Pipeline initialisieren ────────────────────────────────
    if _TRANSCRIPTION_AVAILABLE:
        transcfg = server.cfg.get("transcription", {})
        # Fallback: config.ini einlesen falls kein JSON-Config
        if not transcfg:
            import configparser
            ini = configparser.ConfigParser()
            ini_path = Path(__file__).parent / "config.ini"
            if ini_path.exists():
                ini.read(ini_path)
                if ini.has_section("transcription"):
                    transcfg = dict(ini["transcription"])
        if transcfg.get("enabled", "yes").lower() in ("yes", "true", "1"):
            pipeline = TranscriptionPipeline.__new__(TranscriptionPipeline)
            pipeline.cfg      = transcfg
            pipeline.wav_dir  = Path(transcfg.get("wav_dir", "/opt/FRN/recordings"))
            pipeline.wav_dir.mkdir(parents=True, exist_ok=True)
            pipeline.log_file = Path(transcfg.get("log_file",
                                                    "/opt/FRN/stream/transcription.log"))
            # Auto-Antwort-Hook: Namensnennung → Ollama-Vorschlag an Web-Clients
            pipeline.on_transcript = server.on_transcript
            pipeline.resolve_callsign = server.bot_archive_callsign
            pipeline.resolve_known_text = server.resolve_known_text
            pipeline.debug_trace = server.debug_trace_step
            log.info("Transkription aktiviert (Aufnahmen via frn_stream.py)")

            # Tasks erst im laufenden Loop starten (on_startup)
            async def _start_pipeline(app):
                pipeline._setup_cleanup()

            # Ohne das hier laeuft der Meta-Watcher (alle 2s, Backlog-Aufholung)
            # bei SIGTERM einfach unbeirrt weiter -- aiohttp kennt den Task nicht
            # und wartet nicht auf ihn, aber der Prozess haengt trotzdem, bis
            # systemd nach TimeoutStopSec (90s) mit SIGKILL nachhilft. Sauber
            # abbrechen statt totschlagen lassen.
            async def _stop_pipeline(app):
                log.info("Beende Transkriptions-Pipeline (Meta-Watcher/Cleanup)...")
                for t in (getattr(pipeline, "_task_cleanup", None),
                          getattr(pipeline, "_task_meta", None)):
                    if t:
                        t.cancel()
                try:
                    # Obergrenze: falls die Kuendigung ausnahmsweise mitten in
                    # einer laufenden Whisper-Anfrage (Thread-Pool, nicht
                    # sofort abbrechbar) haengen bleibt, trotzdem spaetestens
                    # nach 5s weitermachen statt wieder auf den 90s-SIGKILL
                    # von systemd zu warten.
                    await asyncio.wait_for(
                        asyncio.gather(pipeline._task_cleanup, pipeline._task_meta,
                                       return_exceptions=True),
                        timeout=5.0)
                except asyncio.TimeoutError:
                    log.warning("Meta-Watcher reagiert nicht auf Abbruch (5s) — "
                                "beende trotzdem weiter.")

            app = server.build_app()
            app.on_startup.append(_start_pipeline)
            app.on_cleanup.append(_stop_pipeline)
        else:
            log.info("Transkription deaktiviert (enabled=no)")
            app = server.build_app()
    else:
        log.info("frn_transcription.py nicht gefunden — Transkription deaktiviert")
        app = server.build_app()

    # SIGTERM/SIGINT-Sicherheitsnetz: normalerweise reicht aiohttps eigener
    # Graceful-Shutdown (raise GracefulExit über den Signal-Handler). Aber ein
    # laufender Whisper-Remote-Call haengt in einem Thread-Pool-Worker in einem
    # blockierenden urllib.request.urlopen(..., timeout=280) -- ein
    # ThreadPoolExecutor-Future, das schon laeuft, laesst sich von außen nicht
    # abbrechen (siehe process_wav in frn_transcription.py). Trifft SIGTERM
    # genau in so ein offenes Zeitfenster, wartet aiohttps runner.cleanup()
    # brav auf dieses eine Future, bis es (im schlimmsten Fall nach 280s)
    # natuerlich endet -- lange genug, dass systemd (TimeoutStopSec 90s)
    # vorher SIGKILLt. Deshalb hier zusaetzlich ein hartes Zeitlimit: nach
    # 15s ohne sauberes Ende erzwingen wir os._exit() selbst, statt auf den
    # harten Kill von systemd zu warten.
    async def _install_shutdown_watchdog(app):
        loop = asyncio.get_event_loop()

        def _on_alarm(signum, frame):
            log.warning("Shutdown haengt (>15s, vermutlich laufender "
                        "Whisper-Request) -- erzwinge Beendigung.")
            os._exit(1)

        def _on_term():
            signal.signal(signal.SIGALRM, _on_alarm)
            signal.alarm(15)
            raise web.GracefulExit()

        try:
            loop.add_signal_handler(signal.SIGINT, _on_term)
            loop.add_signal_handler(signal.SIGTERM, _on_term)
        except NotImplementedError:
            pass  # z.B. Windows

    app.on_startup.append(_install_shutdown_watchdog)

    web.run_app(app, host=args.host, port=args.port,
                access_log=log if args.debug else None)


if __name__ == "__main__":
    main()
