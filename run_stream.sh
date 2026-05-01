#!/bin/bash
# FRN Audio Stream Wrapper
# Usage: run_stream.sh <room_name> <mountpoint> <callsign> <email> <password>
#
# Starts a Python FRN client piped to FFmpeg, streaming to Icecast.
# Automatically reconnects on failure.

set -euo pipefail

ROOM="${1:?Usage: run_stream.sh <room> <mount> <callsign> <email> <password>}"
MOUNT="${2:?Missing mountpoint}"
CALLSIGN="${3:-Stream-${MOUNT}}"
EMAIL="${4:-stream-${MOUNT}@local}"
PASSWORD="${5:-streampass}"

# FRN server
FRN_SERVER="${FRN_SERVER:-localhost}"
FRN_PORT="${FRN_PORT:-10024}"

# Icecast
ICECAST_HOST="${ICECAST_HOST:-localhost}"
ICECAST_PORT="${ICECAST_PORT:-8005}"
ICECAST_PASS="${ICECAST_PASS:-ICECAST_SOURCE_PW}"
ICECAST_URL="icecast://source:${ICECAST_PASS}@${ICECAST_HOST}:${ICECAST_PORT}/${MOUNT}.mp3"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Aufnahme aktivieren wenn Verzeichnis existiert (TX-Server transkribiert zentral)
RECORD_FLAG=""
WAV_DIR="${WAV_DIR:-/opt/FRN/recordings}"
if [ "${ENABLE_RECORDING:-yes}" = "yes" ]; then
    RECORD_FLAG="--record --wav-dir ${WAV_DIR}"
fi

echo "[$(date)] Starting stream: room='${ROOM}' mount='/${MOUNT}.mp3' record=${ENABLE_RECORDING:-yes}" >&2

while true; do
    echo "[$(date)] Connecting FRN client to ${FRN_SERVER}:${FRN_PORT} room '${ROOM}'..." >&2

    python3 "${SCRIPT_DIR}/frn_stream.py" \
        --server "${FRN_SERVER}" \
        --port "${FRN_PORT}" \
        --room "${ROOM}" \
        --email "${EMAIL}" \
        --password "${PASSWORD}" \
        --callsign "${CALLSIGN}" \
        ${RECORD_FLAG} \
    | ffmpeg -hide_banner -loglevel warning \
        -f s16le -ar 8000 -ac 1 -i pipe:0 \
        -codec:a libmp3lame -b:a 64k -ar 22050 -ac 1 \
        -content_type audio/mpeg \
        -ice_name "FRN ${ROOM}" \
        -ice_description "FRN Room: ${ROOM}" \
        -ice_genre "Amateur Radio" \
        -f mp3 "${ICECAST_URL}" \
    || true

    echo "[$(date)] Stream '${ROOM}' disconnected, restarting in 45s..." >&2
    sleep 45
done
