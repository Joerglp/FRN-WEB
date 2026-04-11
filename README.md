# FRN-WEB

Browser-basierter **PTT-Sender und Stream-Empfänger** für das [Free Radio Network (FRN)](http://www.freeradionetwork.de/).

## Features

- **Mithören** — Icecast-Streams direkt im Browser (alle konfigurierten Räume)
- **Senden (PTT)** — Push-to-Talk über Mikrofon, GSM 06.10 kodiert, in den FRN-Raum
- **Admin-Panel** — Benutzer und Räume live verwalten ohne Server-Neustart
- **Docker-Support** — kompletter Stack mit `docker compose up`

## Voraussetzungen

- Laufender **FRN_Server** (Java JAR, nicht in diesem Repo enthalten)
- **Icecast2** — im Docker-Stack enthalten, oder extern
- Python ≥ 3.11 mit `aiohttp`, `numpy`, `scipy`
- `libgsm1` und `ffmpeg` installiert

## Schnellstart (Docker)

```bash
# 1. Repo klonen
git clone https://github.com/<user>/FRN-WEB.git
cd FRN-WEB

# 2. Konfiguration anpassen
cp .env.example .env
cp config/tx_users.json.example config/tx_users.json
# Passwort-Hash setzen:
python3 server/tx_add_user.py admin "DL0XYZ"

# 3. config/config.json editieren
#    → frn.server auf die Adresse des FRN_Server setzen

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

python3 server/frn_tx_server.py --config config/config.json

# Streams (in separaten Terminals oder als systemd-Services):
bash server/run_stream.sh "Quasel-Ecke" quasel TX-Quasel tx-q@local streampass
```

## Konfiguration

### `config/config.json`
Zentrale Einstellungsdatei:

| Schlüssel | Beschreibung |
|-----------|-------------|
| `frn.server` | Adresse des FRN_Server.jar |
| `frn.port` | Port des FRN_Server (Standard: 10024) |
| `icecast.host` | Icecast-Adresse |
| `icecast.source_password` | Passwort für Stream-Einspeisung |
| `ui.streams` | Liste der Räume im Webplayer |

### `config/tx_rooms.json`
FRN-Zugangsdaten pro Raum (Callsign, E-Mail, Passwort).

### `config/tx_users.json`
Web-Benutzer mit SHA-256-Passwort-Hashes. Neuen Benutzer anlegen:

```bash
python3 server/tx_add_user.py <benutzername> <callsign>
```

Oder über das Admin-Panel im Browser (⚙ ADMIN nach Login).

## Admin-Panel

Nach Login mit Admin-Account erscheint der ⚙ ADMIN-Button:

- **Benutzer-Tab** — anlegen, löschen
- **Räume-Tab** — TX-Räume hinzufügen/entfernen (live, kein Neustart nötig)
- **Status-Tab** — FRN-Verbindungsstatus aller Räume

## Architektur

```
Browser (HTTPS)
  │  WebSocket (PCM audio)
  ▼
frn_tx_server.py  ──── TX0/TX1 ────►  FRN_Server.jar :10024
  │                                        │
  │                               (Audio-Routing)
  │                                        │
  ▼                                        ▼
(Web-UI + API)               frn_stream.py (pro Raum)
                                     │ PCM pipe
                                     ▼
                                  ffmpeg (GSM→MP3)
                                     │ Icecast source protocol
                                     ▼
                               Icecast2 :8000
                                     │ HTTP
                                     ▼
                             Browser Audio-Player
```

## Systemd (ohne Docker)

Beispiel-Service-Dateien liegen auf dem Produktionsserver unter
`/etc/systemd/system/frn-*.service`.

## Lizenz

MIT
