#!/usr/bin/env python3
"""
FRN-Crosslink — verbindet zwei FRN-Räume (z.B. Eickelborn-CH74 und
Eickelborn-Freenet) und leitet Audio in beide Richtungen weiter.

Läuft als eigener systemd-Dienst, standardmäßig AUS. Nutzt dieselbe
FRNTXRoom-Klasse wie der TX-Server, verbindet sich aber mit einer EIGENEN
FRN-Kennung (nicht der Stream-/WebTX-Kennung aus tx_rooms.json — sonst
Kontokollision "already online").

Echo-Schutz: Audio, das der Crosslink selbst gerade in einen Raum sendet,
wird beim Empfang aus DEMSELBEN Raum nicht noch einmal zurück in den
anderen Raum weitergeleitet (Zeitfenster-basiert, wie beim KI-Funker).
"""

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

import frn_tx_server as m

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("frn_crosslink")

IDLE_TIMEOUT_S = 1.2   # keine neue Audio-Chunk fuer X Sekunden -> end_tx()
OWN_ECHO_WINDOW_S = 2.5   # Aufnahmen aus diesem Fenster nach eigenem Senden ignorieren


class Bridge:
    """Verbindet room_a <-> room_b, leitet Audio bidirektional weiter."""

    def __init__(self, room_a: m.FRNTXRoom, room_b: m.FRNTXRoom):
        self.room_a = room_a
        self.room_b = room_b
        self._tx_lock = {room_a.name: asyncio.Lock(), room_b.name: asyncio.Lock()}
        self._tx_active = {room_a.name: False, room_b.name: False}
        self._last_chunk_ts = {room_a.name: 0.0, room_b.name: 0.0}
        self._own_send_until = {room_a.name: 0.0, room_b.name: 0.0}
        self._idle_task: asyncio.Task | None = None

    async def start(self):
        await self.room_a.ensure_connected()
        await self.room_b.ensure_connected()
        self.room_a._recorder = _FeedAdapter(self, self.room_a, self.room_b)
        self.room_b._recorder = _FeedAdapter(self, self.room_b, self.room_a)
        self._idle_task = asyncio.create_task(self._idle_watcher())
        log.info("Crosslink aktiv: %s <-> %s", self.room_a.name, self.room_b.name)

    async def stop(self):
        if self._idle_task:
            self._idle_task.cancel()
        for room in (self.room_a, self.room_b):
            room._recorder = None
            try:
                await room.end_tx()
            except Exception:
                pass
            await room.disconnect()
        log.info("Crosslink gestoppt")

    async def relay(self, src: m.FRNTXRoom, dst: m.FRNTXRoom,
                    pcm: bytes, callsign: str):
        now = time.time()
        # Eigenes gerade gesendetes Echo nicht zurueckspiegeln
        if now < self._own_send_until.get(src.name, 0):
            return
        self._last_chunk_ts[dst.name] = now
        async with self._tx_lock[dst.name]:
            if not self._tx_active[dst.name]:
                ok = await dst.request_tx(timeout=5)
                if not ok:
                    log.warning("[%s] TX abgelehnt (Crosslink von %s)",
                               dst.name, src.name)
                    return
                self._tx_active[dst.name] = True
                log.info("Crosslink: %s -> %s gestartet (%s)",
                         src.name, dst.name, callsign or "?")
            await dst.send_pcm(pcm)
        # Echo-Schutz: solange wir selbst in dst senden, kommendes Audio aus
        # dst fuer die naechsten OWN_ECHO_WINDOW_S ignorieren
        self._own_send_until[dst.name] = now + OWN_ECHO_WINDOW_S

    async def _idle_watcher(self):
        try:
            while True:
                await asyncio.sleep(0.4)
                now = time.time()
                for room in (self.room_a, self.room_b):
                    if (self._tx_active[room.name]
                            and now - self._last_chunk_ts[room.name] > IDLE_TIMEOUT_S):
                        async with self._tx_lock[room.name]:
                            if self._tx_active[room.name]:
                                await room.end_tx()
                                self._tx_active[room.name] = False
                                log.info("Crosslink: Sendung in %s beendet (Stille)",
                                        room.name)
        except asyncio.CancelledError:
            pass


class _FeedAdapter:
    """Erfuellt die .feed(pcm, callsign)-Schnittstelle, die FRNTXRoom fuer
    self._recorder erwartet (siehe SessionRecorder), leitet aber an die
    Bridge weiter statt aufzuzeichnen."""

    def __init__(self, bridge: Bridge, src: m.FRNTXRoom, dst: m.FRNTXRoom):
        self.bridge = bridge
        self.src = src
        self.dst = dst

    def feed(self, pcm: bytes, callsign: str = ""):
        asyncio.create_task(self.bridge.relay(self.src, self.dst, pcm, callsign))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/opt/FRN/stream/config.json")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    cl = cfg.get("crosslink", {})
    if not cl.get("enabled"):
        log.info("Crosslink in config.json deaktiviert (crosslink.enabled=false) — beende.")
        return

    email    = cl.get("email", "")
    password = cl.get("password", "")
    callsign = cl.get("callsign", "CROSSLINK")
    room_a_name = cl.get("room_a", "")
    room_b_name = cl.get("room_b", "")
    frn_server  = cfg.get("frn", {}).get("server", "localhost")
    frn_port    = cfg.get("frn", {}).get("port", 10024)

    if not (email and password and room_a_name and room_b_name):
        log.error("crosslink-Config unvollstaendig (email/password/room_a/room_b) — beende.")
        return

    room_a = m.FRNTXRoom(room_a_name, frn_server, frn_port, email, password,
                        f"{callsign}-A")
    room_b = m.FRNTXRoom(room_b_name, frn_server, frn_port, email, password,
                        f"{callsign}-B")
    bridge = Bridge(room_a, room_b)
    await bridge.start()

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await bridge.stop()


if __name__ == "__main__":
    asyncio.run(main())
