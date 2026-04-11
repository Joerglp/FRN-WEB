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
import ctypes
import ctypes.util
import hashlib
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

import numpy as np
from aiohttp import web
from scipy.signal import resample as sp_resample

log = logging.getLogger("frn_tx")

# ── FRN protocol constants ──────────────────────────────────────────────────
FRN_PROTO_VERSION = "2014003"
FRN_TYPE_PC_ONLY  = "2"
MARKER_KEEPALIVE  = 0x00
MARKER_TX_APPROVE = 0x01
MARKER_SOUND      = 0x02
MARKER_CLIENTS    = 0x03
GSM_OPT_WAV49     = 4

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
                        buf = buf[328:]
                        progress = True

                    elif marker == MARKER_CLIENTS:          # 0x03 — variable length
                        clients, new_buf = self._try_parse_clients(buf)
                        if clients is not None:
                            self._clients = clients
                            buf = new_buf
                            progress = True
                        else:
                            break                           # need more data

                    else:
                        buf = buf[1:]                       # skip unknown byte
                        progress = True

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("[%s] Reader: %s", self.name, e)
            self._connected = False

    async def request_tx(self) -> bool:
        if not self._connected:
            await self.ensure_connected()
        self._tx_approved.clear()
        self._writer.write(b"TX0\r\n")
        await self._writer.drain()
        try:
            await asyncio.wait_for(self._tx_approved.wait(), timeout=5.0)
            self._pcm_buf = b""
            return True
        except asyncio.TimeoutError:
            log.warning("[%s] TX approve timeout", self.name)
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

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def hmac_compare(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


# ── Server ───────────────────────────────────────────────────────────────────

class TXServer:
    def __init__(self, args):
        self.args       = args
        self.cfg        = {}   # parsed config.json
        self.users: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}
        self.rooms: dict[str, FRNTXRoom] = {}
        self._users_path: Path | None = None
        self._rooms_path: Path | None = None

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
            self.users[u["username"]] = {
                "callsign":      u.get("callsign", u["username"].upper()),
                "password_hash": u["password_hash"],
                "is_admin":      u.get("is_admin", False),
            }
        log.info("Loaded %d users", len(self.users))

    def load_rooms(self):
        path = Path(self.args.rooms)
        self._rooms_path = path
        if not path.exists():
            log.warning("Rooms file not found: %s", path)
            return
        with open(path) as f:
            data = json.load(f)
        for r in data.get("rooms", []):
            self.rooms[r["mount"]] = FRNTXRoom(
                name       = r["name"],
                frn_server = r.get("frn_server", self.args.frn_server),
                frn_port   = r.get("frn_port",   self.args.frn_port),
                email      = r["email"],
                password   = r["password"],
                callsign   = r["callsign"],
            )
        log.info("Configured %d rooms: %s", len(self.rooms), list(self.rooms))

    def _save_users(self):
        if not self._users_path:
            return
        data = {"users": [
            {
                "username":      uname,
                "callsign":      info["callsign"],
                "password_hash": info["password_hash"],
                "is_admin":      info.get("is_admin", False),
            }
            for uname, info in self.users.items()
        ]}
        with open(self._users_path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")

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

    def _token_for(self, username: str) -> str:
        token = secrets.token_hex(24)
        u = self.users[username]
        self.tokens[token] = {
            "user":     username,
            "callsign": u["callsign"],
            "is_admin": u.get("is_admin", False),
            "expires":  time.time() + 3600,
        }
        return token

    def _validate_token(self, token: str) -> dict | None:
        info = self.tokens.get(token)
        if not info:
            return None
        if time.time() > info["expires"]:
            del self.tokens[token]
            return None
        return info

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
            self.rooms[mount] = FRNTXRoom(
                name       = name,
                frn_server = self.args.frn_server,
                frn_port   = self.args.frn_port,
                email      = email,
                password   = password,
                callsign   = f"{prefix}-{i + 1:02d}",
            )
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
            return al in ("OK", "ADMIN", "OWNER", "NETOWNER")
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
        token = request.rel_url.query.get("token", "")
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

        username = body.get("username", "").strip()
        password = body.get("password", "")
        # auth_mode: "local" | "frn" | "both" (default: "both")
        auth_mode = self.cfg.get("auth", {}).get("mode", "both")

        # ── 1. Lokale Authentifizierung ──────────────────────────────────
        if auth_mode in ("local", "both"):
            user = self.users.get(username)
            if user and hmac_compare(hash_password(password), user["password_hash"]):
                token = self._token_for(username)
                rooms = [{"mount": m, "name": r.name} for m, r in self.rooms.items()]
                return web.json_response({
                    "token":    token,
                    "callsign": user["callsign"],
                    "is_admin": user.get("is_admin", False),
                    "rooms":    rooms,
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
                # Ephemeres Token — kein Eintrag in tx_users.json nötig
                token = secrets.token_hex(24)
                self.tokens[token] = {
                    "user":     username,
                    "callsign": callsign,
                    "is_admin": False,   # FRN-User bekommen keinen Admin-Zugang
                    "expires":  time.time() + 3600,
                }
                rooms = [{"mount": m, "name": r.name} for m, r in self.rooms.items()]
                log.info("FRN login: %s (%s)", username, callsign)
                return web.json_response({
                    "token":    token,
                    "callsign": callsign,
                    "is_admin": False,
                    "rooms":    rooms,
                })

        await asyncio.sleep(1)
        return web.json_response(
            {"error": "Ungültiger Benutzername oder Passwort"}, status=401)

    async def handle_rooms(self, request):
        token = request.rel_url.query.get("token", "")
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
        })

    # ── Admin API handlers ─────────────────────────────────────────────────

    async def handle_admin_users_list(self, request):
        _, err = await self._require_admin(request)
        if err:
            return err
        return web.json_response({"users": [
            {"username": u, "callsign": d["callsign"],
             "is_admin": d.get("is_admin", False)}
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

        username = body.get("username", "").strip()
        callsign = body.get("callsign", "").strip()
        password = body.get("password", "")
        is_admin = bool(body.get("is_admin", False))

        if not username or not password:
            return web.json_response({"error": "username and password required"}, status=400)
        if len(password) < 4:
            return web.json_response({"error": "password too short (min 4)"}, status=400)

        self.users[username] = {
            "callsign":      callsign or username.upper(),
            "password_hash": hash_password(password),
            "is_admin":      is_admin,
        }
        self._save_users()
        log.info("Admin: created user '%s'", username)
        return web.json_response({"ok": True})

    async def handle_admin_users_update(self, request):
        _, err = await self._require_admin(request)
        if err:
            return err
        username = request.match_info["username"]
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

        self._save_users()
        log.info("Admin: updated user '%s'", username)
        return web.json_response({"ok": True})

    async def handle_admin_users_delete(self, request):
        info, err = await self._require_admin(request)
        if err:
            return err
        username = request.match_info["username"]
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

        self.rooms[mount] = FRNTXRoom(
            name=name, frn_server=frn_srv, frn_port=frn_port,
            email=email, password=password,
            callsign=callsign or f"TX-{mount.title()}")
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
        })

    async def handle_frn_networks(self, request):
        """Return list of available FRN room names (requires valid token).

        Uses the configured ``frn_stream_account`` credentials to query the FRN
        server, or falls back to the names of already-loaded rooms.
        """
        token = request.rel_url.query.get("token", "")
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

    async def handle_room_clients(self, request):
        """Return list of clients currently in a room (requires valid token).

        Triggers a FRN connection for the room if it is not yet connected,
        so the client list arrives as soon as possible.
        """
        token = request.rel_url.query.get("token", "")
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

    # ── WebSocket ──────────────────────────────────────────────────────────

    async def handle_ws(self, request):
        token = request.rel_url.query.get("token", "")
        info  = self._validate_token(token)
        if not info:
            return web.Response(status=401, text="Unauthorized")

        mount = request.rel_url.query.get("room", "")
        room  = self.rooms.get(mount)
        if not room:
            return web.Response(status=404, text=f"Room '{mount}' not found")

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        callsign = info["callsign"]
        log.info("WS connected: user=%s room=%s", info["user"], mount)

        in_tx        = False
        src_rate     = 48000
        native_buf   = np.array([], dtype=np.float32)
        native_block = 960
        block_8k     = 160

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

                        if room._tx_lock.locked():
                            await ws.send_json({"type": "error",
                                                "msg": "Raum belegt (jemand sendet)"})
                            continue
                        async with room._tx_lock:
                            in_tx = True
                            try:
                                ok = await room.request_tx()
                                if ok:
                                    await ws.send_json({"type": "tx_active"})
                                else:
                                    await ws.send_json({"type": "error",
                                                        "msg": "TX nicht genehmigt"})
                                    in_tx = False
                            except Exception as e:
                                log.error("TX request error: %s", e)
                                await ws.send_json({"type": "error", "msg": str(e)})
                                in_tx = False

                    elif cmd == "PTT_STOP":
                        if in_tx:
                            await room.end_tx()
                            in_tx = False
                            await ws.send_json({"type": "tx_stopped"})

                elif msg.type == web.WSMsgType.BINARY:
                    if not in_tx:
                        continue
                    pcm_in = np.frombuffer(msg.data, dtype="<i2").astype(np.float32)
                    if src_rate == 8000:
                        await room.send_pcm(pcm_in.astype("<i2").tobytes())
                    else:
                        native_buf = np.append(native_buf, pcm_in)
                        while len(native_buf) >= native_block:
                            chunk      = native_buf[:native_block]
                            native_buf = native_buf[native_block:]
                            resampled  = sp_resample(chunk, block_8k)
                            pcm_bytes  = np.clip(resampled, -32768, 32767).astype("<i2").tobytes()
                            await room.send_pcm(pcm_bytes)

                elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break

        except Exception as e:
            log.error("WS error: %s", e)
        finally:
            if in_tx:
                await room.end_tx()
            log.info("WS closed: user=%s", info["user"])

        return ws

    # ── CORS middleware ────────────────────────────────────────────────────

    @web.middleware
    async def cors_middleware(self, request, handler):
        if request.method == "OPTIONS":
            return web.Response(headers={
                "Access-Control-Allow-Origin":  "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
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
        app.router.add_get ("/api/rooms",           self.handle_rooms)
        app.router.add_get ("/api/config",          self.handle_config)
        app.router.add_get ("/api/frn-networks",              self.handle_frn_networks)
        app.router.add_get ("/api/rooms/{mount}/clients",    self.handle_room_clients)
        app.router.add_get ("/ws",                           self.handle_ws)

        # Admin (require token + is_admin)
        app.router.add_get   ("/api/admin/users",           self.handle_admin_users_list)
        app.router.add_post  ("/api/admin/users",           self.handle_admin_users_create)
        app.router.add_put   ("/api/admin/users/{username}", self.handle_admin_users_update)
        app.router.add_delete("/api/admin/users/{username}", self.handle_admin_users_delete)

        app.router.add_get   ("/api/admin/rooms",         self.handle_admin_rooms_list)
        app.router.add_post  ("/api/admin/rooms",         self.handle_admin_rooms_create)
        app.router.add_delete("/api/admin/rooms/{mount}", self.handle_admin_rooms_delete)

        app.router.add_get("/api/admin/status", self.handle_admin_status)

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

    app = server.build_app()
    web.run_app(app, host=args.host, port=args.port,
                access_log=log if args.debug else None)


if __name__ == "__main__":
    main()
