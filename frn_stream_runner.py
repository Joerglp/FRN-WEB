#!/usr/bin/env python3
"""
FRN Stream Runner — Docker entrypoint
Liest config/config.json + config/tx_rooms.json und startet pro Raum
einen frn_stream.py + ffmpeg Prozess. Watchdog startet abgestürzte Streams neu.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE        = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("CONFIG",    BASE / "config" / "config.json"))
ROOMS_PATH  = Path(os.environ.get("ROOMS",     BASE / "config" / "tx_rooms.json"))
STREAM_PY   = BASE / "frn_stream.py"
WAV_DIR     = os.environ.get("WAV_DIR", "/opt/FRN/recordings")


def load_config():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    with open(ROOMS_PATH) as f:
        rooms_data = json.load(f)
    return cfg, rooms_data.get("rooms", [])


def start_stream(room_cfg, cfg):
    frn_server = os.environ.get("FRN_SERVER",  str(cfg["frn"]["server"]))
    frn_port   = os.environ.get("FRN_PORT",    str(cfg["frn"]["port"]))
    ice_host   = os.environ.get("ICECAST_HOST", cfg["icecast"]["host"])
    ice_port   = os.environ.get("ICECAST_PORT", str(cfg["icecast"]["port"]))
    ice_pass   = os.environ.get("ICECAST_PASS", cfg["icecast"]["source_password"])

    mount    = room_cfg["mount"]
    room     = room_cfg["name"]
    callsign = room_cfg.get("callsign", f"Stream-{mount.title()}")
    email    = room_cfg.get("email",    f"stream-{mount}@local")
    password = room_cfg.get("password", "streampass")
    record   = os.environ.get("ENABLE_RECORDING", "yes") == "yes"

    ice_url = f"icecast://source:{ice_pass}@{ice_host}:{ice_port}/{mount}.mp3"

    stream_cmd = [
        "python3", str(STREAM_PY),
        "--server", frn_server, "--port", frn_port,
        "--room", room, "--callsign", callsign,
        "--email", email, "--password", password,
    ]
    if record:
        stream_cmd += ["--record", "--wav-dir", WAV_DIR]

    ffmpeg_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "s16le", "-ar", "8000", "-ac", "1", "-i", "pipe:0",
        "-codec:a", "libmp3lame", "-b:a", "64k", "-ar", "22050", "-ac", "1",
        "-content_type", "audio/mpeg",
        "-ice_name", f"FRN {room}",
        "-ice_description", f"FRN Room: {room}",
        "-ice_genre", "Amateur Radio",
        "-f", "mp3", ice_url,
    ]

    print(f"[runner] Starting: {room} → /{mount}.mp3 (record={record})", flush=True)

    stream_proc = subprocess.Popen(stream_cmd, stdout=subprocess.PIPE)
    ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdin=stream_proc.stdout)
    stream_proc.stdout.close()

    return stream_proc, ffmpeg_proc


def stop_stream(procs):
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass


def main():
    cfg, rooms = load_config()
    if not rooms:
        print("[runner] Keine Räume in tx_rooms.json konfiguriert.", file=sys.stderr)
        sys.exit(1)

    processes = {}
    for room_cfg in rooms:
        processes[room_cfg["mount"]] = (room_cfg, start_stream(room_cfg, cfg))

    while True:
        time.sleep(15)
        # Config neu laden (live-Änderungen an tx_rooms.json übernehmen)
        try:
            cfg, rooms = load_config()
        except Exception as e:
            print(f"[runner] Config-Fehler: {e}", flush=True)

        for mount, (room_cfg, procs) in list(processes.items()):
            stream_proc, ffmpeg_proc = procs
            if stream_proc.poll() is not None or ffmpeg_proc.poll() is not None:
                print(f"[runner] Stream '{mount}' abgestürzt, Neustart …", flush=True)
                stop_stream(procs)
                time.sleep(5)
                new_room_cfg = next((r for r in rooms if r["mount"] == mount), room_cfg)
                processes[mount] = (new_room_cfg, start_stream(new_room_cfg, cfg))


if __name__ == "__main__":
    main()
