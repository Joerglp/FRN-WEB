# FRN-WEB

Browser-basierter **PTT-Sender, Stream-Empfänger und Gesprächsarchiv** für das [Free Radio Network (FRN)](http://www.freeradionetwork.de/).

## Features

- **Mithören** — Icecast-Streams direkt im Browser (alle konfigurierten Räume)
- **Senden (PTT)** — Push-to-Talk über Mikrofon, GSM 06.10 kodiert, in den FRN-Raum
- **Login** — mit lokalem Account (`tx_users.json`) oder direkt mit FRN-Zugangsdaten
- **Admin-Panel** — Benutzer und Räume live verwalten ohne Server-Neustart
- **Auto-Discovery** — Räume automatisch vom FRN-Server lesen, keine feste Raumliste nötig
- **Transkription** *(optional)* — Spracherkennung via [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CPU, kein GPU nötig)
- **Funkarchiv** *(optional)* — Chat-Verlauf aller Übertragungen mit Audioplayer, durchsuchbar
- **Komprimiertes Audio** — WAV-Aufnahmen werden als Opus gespeichert (~10× kleiner)
- **Docker-Support** — kompletter Stack mit `docker compose up`

## Voraussetzungen

- Laufender **FRN_Server** (Java JAR, nicht in diesem Repo enthalten)
- **Icecast2** — im Docker-Stack enthalten, oder extern
- Python ≥ 3.11 mit `aiohttp`, `numpy`, `scipy`
- `libgsm1` und `ffmpeg` installiert

## Schnellstart (Docker)

```bash
# 1. Repo klonen
git clone https://github.com/Joerglp/FRN-WEB.git
cd FRN-WEB

# 2. Konfiguration anpassen
cp .env.example .env
cp config/tx_users.json.example config/tx_users.json
# Ersten Admin-Benutzer anlegen:
python3 server/tx_add_user.py admin "DL0XYZ"

# 3. config/config.json editieren
#    → frn.server auf die Adresse des FRN_Server setzen
#    → frn_stream_account mit FRN-Zugangsdaten füllen (für Auto-Discovery)

# 4. Starten (ohne Whisper)
docker compose up -d

# 4b. Starten MIT Whisper-Transkription
WITH_WHISPER=true docker compose up -d --build
```

Danach erreichbar unter:
- **Web-UI / PTT:** `http://localhost:8765`
- **Funkarchiv:** `http://localhost:8765/archive`
- **Icecast:** `http://localhost:8000`

## Whisper-Transkription (optional)

Alle Übertragungen werden automatisch transkribiert und im **Funkarchiv** gespeichert.

### Aktivieren

```bash
# In .env setzen:
WITH_WHISPER=true

# Neu bauen und starten:
docker compose up -d --build
```

Beim ersten Start lädt der Server das Whisper-Modell herunter (~1,5 GB für `medium`) und speichert es im `whisper-cache` Docker-Volume. Danach bleibt es erhalten.

### Konfiguration (`config/config.json`)

```json
"transcription": {
  "enabled": "yes",
  "whisper_model": "medium",
  "whisper_language": "de",
  "wav_dir": "/opt/FRN/recordings",
  "max_age_days": 2,
  "mqtt_broker": "localhost",
  "mqtt_port": 1883,
  "mqtt_topic_prefix": "Home/FRN"
}
```

| Modell | Größe | Qualität | RAM (CPU) |
|--------|-------|----------|-----------|
| `tiny` | 75 MB | niedrig | ~400 MB |
| `base` | 145 MB | mittel | ~600 MB |
| `small` | 480 MB | gut | ~1 GB |
| `medium` | 1,5 GB | sehr gut | ~2,5 GB |

> **Raspberry Pi 5:** `medium` mit `int8` läuft stabil, benötigt aber ~3 GB RAM + Swap.

### Funkarchiv

Das Archiv ist unter `/archive` erreichbar und zeigt alle Übertragungen als Chat-Verlauf:

- Filter nach Raum, Datum und Suchbegriff
- Inline-Audioplayer (Opus, ~10× kleiner als WAV)
- Auto-Refresh alle 30 Sekunden
- Callsigns farblich unterschieden

## Schnellstart (ohne Docker)

```bash
pip install aiohttp numpy scipy
# Optionales Whisper:
pip install faster-whisper paho-mqtt

# libgsm + ffmpeg installieren:
apt install libgsm1 ffmpeg

python3 server/frn_tx_server.py \
    --config config/config.json \
    --users  config/tx_users.json \
    --rooms  config/tx_rooms.json

# Streams (in separaten Terminals oder als systemd-Services):
bash server/run_stream.sh "Quasel-Ecke" quasel TX-Quasel tx-q@local streampass
```

## Konfiguration

### `config/config.json`

| Schlüssel | Beschreibung |
|-----------|-------------|
| `frn.server` | Adresse des FRN_Server.jar |
| `frn.port` | Port des FRN_Server (Standard: 10024) |
| `icecast.host` | Icecast-Adresse |
| `icecast.source_password` | Passwort für Stream-Einspeisung |
| `auth.mode` | `local` / `frn` / `both` — Login-Methode |
| `frn_stream_account.email` | FRN-Zugangsdaten für TX und Room-Discovery |
| `frn_stream_account.password` | Passwort des Stream-Accounts |
| `ui.title` | Titel der Web-Oberfläche |
| `transcription.enabled` | Transkription aktivieren (`yes`/`no`) |
| `transcription.whisper_model` | Modellgröße (`tiny`/`base`/`small`/`medium`) |
| `transcription.whisper_language` | Sprache (z.B. `de`, `en`) |

### `config/tx_users.json`

Web-Benutzer mit SHA-256-Passwort-Hashes. Neuen Admin-Benutzer anlegen:

```bash
python3 server/tx_add_user.py <benutzername> <callsign>
```

Oder über das Admin-Panel im Browser (⚙ ADMIN nach Login).

> **Hinweis:** Der ⚙ ADMIN-Button erscheint nur bei Benutzern mit `"is_admin": true`.
> FRN-Direktlogin erhält normalen Zugang (kein Admin).

### `config/tx_rooms.json` (optional)

Manuelle Raumliste. Leer lassen (`{"rooms":[]}`) damit der Server die Räume beim Start
automatisch vom FRN-Server entdeckt (`frn_stream_account` muss gesetzt sein).

## Admin-Panel

Nach Login mit einem Admin-Account: **⚙ ADMIN**-Button oben rechts.

| Tab | Funktion |
|-----|---------|
| BENUTZER | Web-Benutzer anlegen / löschen |
| RÄUME | TX-Räume hinzufügen / entfernen (live, kein Neustart) |
| STATUS | FRN-Verbindungsstatus aller Räume + aktive Tokens |

## Architektur

```
Browser
  │  WebSocket (PCM audio)              HTTP (Audio-Stream)
  │  HTTP (Archiv, Admin-API)                  ▲
  ▼                                            │
frn_tx_server.py ──── TX0/TX1 ────► FRN_Server.jar :10024
  │  (HTTP + WS, :8765)                        │
  │                                   (Audio-Routing)
  │  ┌─────────────────────────────────────────┘
  │  │
  │  └── frn_stream.py (pro Raum)
  │            │ PCM pipe
  │            ▼
  │       ffmpeg (GSM → MP3)
  │            │ Icecast source
  │            ▼
  │       Icecast2 :8000
  │
  ├── frn_transcription.py
  │      │ faster-whisper (CPU)
  │      ▼
  │   frn_archive.py
  │      │ SQLite + Opus
  │      ▼
  │   /archive  (Chat-Verlauf Web-UI)
  │
  └── MQTT → Home Automation (optional)
```

## Systemd (ohne Docker)

```
/etc/systemd/system/frn-tx-server.service   # Web TX Server
/etc/systemd/system/frn-stream@.service     # Stream pro Raum (Template)
/etc/systemd/system/icecast2.service        # Icecast (meist Paket-Standard)
```

## Lizenz

MIT
