#!/usr/bin/env python3
"""
FRN Stream Runner (Docker entrypoint)
Liest config/config.json und startet für jeden konfigurierten
Stream einen frn_stream.py + ffmpeg Prozess.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.json"
SCRIPT_DIR  = Path(__file__).parent

def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)

def start_stream(room_cfg, cfg):
    frn_server  = os.environ.get("FRN_SERVER",  cfg["frn"]["server"])
    frn_port    = os.environ.get("FRN_PORT",    str(cfg["frn"]["port"]))
    ice_host    = os.environ.get("ICECAST_HOST", cfg["icecast"]["host"])
    ice_port    = os.environ.get("ICECAST_PORT", str(cfg["icecast"]["port"]))
    ice_pass    = os.environ.get("ICECAST_PASS", cfg["icecast"]["source_password"])

    mount    = room_cfg["mount"]
    room     = room_cfg["name"]
    callsign = room_cfg.get("callsign", f"Stream-{mount.title()}")
    email    = room_cfg.get("email",    f"stream-{mount}@local")
    password = room_cfg.get("password", "streampass")

    ice_url = f"icecast://source:{ice_pass}@{ice_host}:{ice_port}/{mount}.mp3"

    cmd = (
        f"python3 {SCRIPT_DIR}/frn_stream.py"
        f" --server {frn_server} --port {frn_port}"
        f" --room \"{room}\" --callsign \"{callsign}\""
        f" --email \"{email}\" --password \"{password}\""
        f" | ffmpeg -hide_banner -loglevel warning"
        f" -f s16le -ar 8000 -ac 1 -i pipe:0"
        f" -codec:a libmp3lame -b:a 64k -ar 22050 -ac 1"
        f" -content_type audio/mpeg"
        f" -ice_name \"FRN {room}\""
        f" -ice_description \"FRN Room: {room}\""
        f" -ice_genre \"Amateur Radio\""
        f" -f mp3 \"{ice_url}\""
    )
    print(f"[runner] Starting stream: {room} → /{mount}.mp3", flush=True)
    return subprocess.Popen(cmd, shell=True)

def main():
    cfg = load_config()
    rooms = cfg.get("streams", cfg.get("ui", {}).get("streams", []))
    if not rooms:
        print("[runner] No streams configured in config.json", file=sys.stderr)
        sys.exit(1)

    processes = {}
    for room_cfg in rooms:
        processes[room_cfg["mount"]] = start_stream(room_cfg, cfg)

    # Watchdog: restart failed streams
    while True:
        time.sleep(15)
        for mount, proc in list(processes.items()):
            if proc.poll() is not None:
                print(f"[runner] Stream '{mount}' died (rc={proc.returncode}), restarting…",
                      flush=True)
                room_cfg = next(
                    (r for r in rooms if r["mount"] == mount), None)
                if room_cfg:
                    processes[mount] = start_stream(room_cfg, cfg)

if __name__ == "__main__":
    main()
