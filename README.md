# FRN-WEB

Browser-basierter **PTT-Sender und Stream-Empfänger** für das [Free Radio Network (FRN)](http://www.freeradionetwork.de/).

## Features

- **Mithören** — Icecast-Streams direkt im Browser (alle konfigurierten Räume)
- **Senden (PTT)** — Push-to-Talk über Mikrofon, GSM 06.10 kodiert, in den FRN-Raum
- **Login** — mit lokalem Account (tx_users.json) oder direkt mit FRN-Zugangsdaten
- **Admin-Panel** — Benutzer und Räume live verwalten ohne Server-Neustart
- **Auto-Discovery** — Räume automatisch vom FRN-Server lesen, keine feste Raumliste nötig
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
# Passwort-Hash setzen:
python3 server/tx_add_user.py admin "DL0XYZ"

# 3. config/config.json editieren
#    → frn.server auf die Adresse des FRN_Server setzen
#    → frn_stream_account mit FRN-Zugangsdaten füllen (für Auto-Discovery)

# 4. Starten
docker compose up -d
```

Danach erreichbar unter:
- **Web-UI:** http://localhost:8765
- **Icecast:** http://localhost:8000

## Schnellstart (ohne Docker)

```bash
pip install aiohttp numpy scipy
# libgsm installieren: apt install libgsm1

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

### `config/tx_rooms.json` (optional)

Manuelle Raumliste mit FRN-Zugangsdaten pro Raum. Wird diese Datei leer gelassen
oder weggelassen, entdeckt der Server die verfügbaren Räume beim Start automatisch
über den FRN-Server (`frn_stream_account` muss gesetzt sein).

### `config/tx_users.json`

Web-Benutzer mit SHA-256-Passwort-Hashes. Neuen Admin-Benutzer anlegen:

```bash
python3 server/tx_add_user.py <benutzername> <callsign>
```

Oder über das Admin-Panel im Browser (⚙ ADMIN nach Login).

> **Hinweis:** Damit der ⚙ ADMIN-Button sichtbar ist, muss der Benutzer
> `"is_admin": true` in der tx_users.json haben.
> Alternativ kann man sich mit FRN-Zugangsdaten anmelden und erhält
> normalen Zugang (kein Admin).

## Admin-Panel

Nach Login mit einem Admin-Account erscheint der **⚙ ADMIN**-Button oben rechts:

| Tab | Funktion |
|-----|---------|
| BENUTZER | Web-Benutzer anlegen / löschen |
| RÄUME | TX-Räume hinzufügen / entfernen (live, kein Neustart) |
| STATUS | FRN-Verbindungsstatus aller Räume + aktive Tokens |

## Räume automatisch entdecken

Statt einer festen `tx_rooms.json` kann der Server beim Start die verfügbaren
FRN-Räume direkt vom FRN-Server lesen (MARKER_NETWORKS, 0x05):

```json
"frn_stream_account": {
  "email":           "stream@example.de",
  "password":        "geheim",
  "callsign_prefix": "WEB"
}
```

`tx_rooms.json` dann leer lassen (`{"rooms":[]}`). Der Server baut die
Raumliste beim Start automatisch auf. Über `GET /api/frn-networks?token=...`
lässt sich die Live-Liste auch jederzeit abrufen.

## Architektur

```
Browser
  │  WebSocket (PCM audio)          HTTP (Audio-Stream)
  ▼                                        ▲
frn_tx_server.py  ── TX0/TX1 ──►  FRN_Server.jar :10024
  │  (HTTP + WS, :8765)                    │
  │                               (Audio-Routing)
  │                                        │
  │                              frn_stream.py (pro Raum)
  │                                    │ PCM pipe
  │                                    ▼
  │                               ffmpeg (GSM → MP3)
  │                                    │ Icecast source
  │                                    ▼
  └──────────── Web-UI ──────────  Icecast2 :8000
```

## Systemd (ohne Docker)

```
/etc/systemd/system/frn-tx-server.service   # Web TX Server
/etc/systemd/system/frn-stream@.service     # Stream pro Raum (Template)
/etc/systemd/system/icecast2.service        # Icecast (meist Paket-Standard)
```

## Lizenz

MIT
