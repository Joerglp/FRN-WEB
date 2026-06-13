# FRN-WEB

Browser-basierter **PTT-Sender, Stream-Empfänger und Gesprächsarchiv** für das [Free Radio Network (FRN)](http://www.freeradionetwork.de/).

## Features

- **Mithören** — Icecast-Streams direkt im Browser (alle konfigurierten Räume)
- **Senden (PTT)** — Push-to-Talk über Mikrofon, GSM 06.10 kodiert, in den FRN-Raum
- **Schnell-Satz-Buttons** — vordefinierte Phrasen per Knopfdruck senden; eigene Sprachaufnahme pro Button, grüner Punkt zeigt eigene Aufnahme an
- **Login** — mit lokalem Account (`tx_users.json`) oder direkt mit FRN-Zugangsdaten
- **Admin-Panel** — Benutzer, Räume und FRN-Server live verwalten ohne Server-Neustart
- **FRN-Server wechseln** — direkt aus dem Browser auf einen anderen FRN-Server umschalten
- **FRN-Konto registrieren** — neues FRN-Konto direkt aus dem Browser beantragen
- **Auto-Discovery** — Räume automatisch vom FRN-Server lesen, keine feste Raumliste nötig
- **Transkription** *(optional)* — Spracherkennung via [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- **Funkarchiv** *(optional)* — Alle Übertragungen mit Audioplayer, durchsuchbar; separater **Chat-Verlauf**-Tab
- **Komprimiertes Audio** — WAV-Aufnahmen werden als Opus gespeichert (~10× kleiner)
- **Hintergrund-Audio** — 🔊-Button hält den Stream aktiv wenn der Bildschirm gesperrt wird
- **WakeLock** — ☀️-Button verhindert das automatische Abdunkeln des Displays
- **Leertaste PTT** — Leerzeichen als Tastaturkürzel für Push-to-Talk (Desktop)
- **TOT-Countdown** — visueller Countdown beim Senden, auto-stop wenn Zeit abläuft
- **Online-Nutzer live** — Stationsliste wird per WebSocket in Echtzeit aktualisiert
- **RX-Lautstärke** — Schieberegler, Wert bleibt dauerhaft gespeichert
- **Browser-Benachrichtigungen** — Notification bei Empfang/Chat wenn Tab im Hintergrund
- **Tab-Titel-Indikator** — `● RX` / `● CHAT` im Browser-Tab bei Aktivität
- **Raum per URL** — `?room=<mount>` wählt Raum automatisch nach Login
- **Archiv-Statistiken** — Balkendiagramm, Top-Rufzeichen und -Räume, Gesamtzahlen
- **Kommentare** — Notizen zu einzelnen Archiv-Einträgen hinzufügen
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

# 2. Benutzer und Räume konfigurieren
cp config/tx_users.json.example config/tx_users.json
python3 tx_add_user.py admin "DL0XYZ"   # Ersten Admin anlegen

# 3. config/config.json editieren
#    → frn.server: Adresse des FRN_Server (host.docker.internal = lokaler Host)
#    → frn_stream_account: FRN-Zugangsdaten für Auto-Discovery und Streams

# 4. Starten (ohne Whisper)
docker compose up -d

# 4b. Starten MIT Whisper-Transkription
WITH_WHISPER=true docker compose up -d --build
```

Danach erreichbar unter:
- **Web-UI / PTT:** `http://localhost:8765`
- **Funkarchiv:** `http://localhost:8765/archive`
- **Icecast:** `http://localhost:8000`

## Schnellstart (ohne Docker / Raspberry Pi)

```bash
pip install aiohttp numpy scipy
apt install libgsm1 ffmpeg

# TX-Server starten
python3 frn_tx_server.py \
    --config config/config.json \
    --users  config/tx_users.json \
    --rooms  config/tx_rooms.json

# Streams starten (je Raum, oder via systemd — siehe unten)
bash run_stream.sh "Quasel-Ecke" quasel TX-Quasel tx@example.com passwort
```

## Konfiguration

### `config/config.json`

| Schlüssel | Beschreibung |
|-----------|-------------|
| `frn.server` | Adresse des FRN_Server.jar |
| `frn.port` | Port des FRN_Server (Standard: 10024) |
| `frn.tx_timeout` | Max. Sendezeit in Sekunden, TOT-Countdown (Standard: 180) |
| `icecast.host` | Icecast-Adresse |
| `icecast.source_password` | Passwort für Stream-Einspeisung |
| `auth.mode` | `local` / `frn` / `both` — Login-Methode |
| `frn_stream_account.email` | FRN-Zugangsdaten für TX und Room-Discovery |
| `frn_stream_account.password` | Passwort des Stream-Accounts |
| `ui.title` | Titel der Web-Oberfläche |
| `whisper.remote_url` | URL des externen Whisper-Servers (leer = lokal) |
| `whisper.model` | Modellgröße (`tiny`/`base`/`small`/`medium`) |
| `whisper.language` | Sprache (z.B. `de`, `en`) |

### `config/tx_users.json`

Web-Benutzer mit SHA-256-Passwort-Hashes. Neuen Admin-Benutzer anlegen:

```bash
python3 tx_add_user.py <benutzername> <callsign>
```

Oder über das Admin-Panel im Browser (⚙ ADMIN nach Login).

### `config/tx_rooms.json` (optional)

Manuelle Raumliste. Leer lassen (`{"rooms":[]}`) damit der Server die Räume beim Start
automatisch vom FRN-Server entdeckt (`frn_stream_account` muss gesetzt sein).

## Admin-Panel

Nach Login mit einem Admin-Account: **⚙ ADMIN**-Button oben rechts.

| Tab | Funktion |
|-----|---------|
| BENUTZER | Web-Benutzer anlegen / löschen / Passwort ändern |
| RÄUME | TX-Räume hinzufügen / entfernen (live, kein Neustart) |
| STATUS | FRN-Verbindungsstatus aller Räume + aktive Tokens |
| SERVER | FRN-Server wechseln (inkl. Zugangsdaten) + neues FRN-Konto registrieren |

## Funkarchiv

Erreichbar unter `/archive`. Drei Tabs:

| Tab | Inhalt |
|-----|--------|
| AUDIO-ARCHIV | Alle Sprachübertragungen mit Transkription und Audioplayer; Kommentare pro Eintrag |
| CHAT-VERLAUF | Alle FRN-Textnachrichten (SQLite, durchsuchbar nach Text/Rufzeichen/Raum) |
| STATISTIK | Übertragungen pro Tag (Balkendiagramm), Top-Rufzeichen, Top-Räume, Gesamtzahlen |

Chat-Nachrichten werden automatisch gespeichert sobald sie im FRN-Raum eingehen — unabhängig von der Transkription.

### FRN-Konto registrieren

Im SERVER-Tab können neue FRN-Konten direkt beim System-Manager `sysman.freeradionetwork.de` beantragt werden. Rufzeichen, Name, E-Mail und Stadt eingeben → **KONTO BEANTRAGEN** → Passwort kommt per E-Mail.

## Whisper-Transkription

Alle Übertragungen werden automatisch transkribiert und im **Funkarchiv** gespeichert.
`whisper_server.py` läuft als eigenständiger HTTP-Server (Docker-Service oder systemd)
und wird von `frn_tx_server.py` über `whisper.remote_url` angesprochen.

### Docker (Standard)

`docker compose up` startet automatisch einen lokalen `whisper`-Service (CPU, `medium`-Modell).
Kein weiterer Aufwand — das Modell wird beim ersten Start heruntergeladen (~1,5 GB).

```bash
# Anderes Modell wählen (in .env oder inline):
WHISPER_MODEL=small docker compose up -d
```

### Externer GPU-Rechner (empfohlen für bessere Qualität)

Auf einem Rechner mit NVIDIA-GPU:

```bash
pip install faster-whisper aiohttp
WHISPER_MODEL=large-v3 WHISPER_DEVICE=cuda python3 whisper_server.py
```

Dann in `.env` (Docker) oder `config/config.json` (nativ):

```bash
# .env
WHISPER_REMOTE_URL=http://192.168.x.x:9001/transcribe
```

```json
"whisper": { "remote_url": "http://192.168.x.x:9001/transcribe" }
```

Ist `WHISPER_REMOTE_URL` gesetzt, wird der lokale `whisper`-Docker-Service ignoriert.

### Nativ (systemd)

```bash
pip install faster-whisper aiohttp numpy
python3 whisper_server.py   # läuft auf Port 9001
```

In `config/config.json`:
```json
"whisper": { "remote_url": "http://localhost:9001/transcribe" }
```

| Modell | Größe | Qualität | RAM (CPU) |
|--------|-------|----------|-----------|
| `tiny` | 75 MB | niedrig | ~400 MB |
| `base` | 145 MB | mittel | ~600 MB |
| `small` | 480 MB | gut | ~1 GB |
| `medium` | 1,5 GB | sehr gut | ~2,5 GB |
| `large-v3` | 3 GB | exzellent | ~5 GB / 3 GB VRAM |

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
  │            │ Icecast source protocol
  │            ▼
  │       Icecast2 :8000
  │
  ├── frn_transcription.py (faster-whisper, CPU oder remote GPU)
  │      ▼
  │   frn_archive.py (SQLite + Opus)
  │      ▼
  │   /archive  (Chat-Verlauf Web-UI)
  │
  └── (Docker) frn_stream_runner.py — startet alle Streams, Watchdog
```

## Systemd (ohne Docker)

```
/etc/systemd/system/frn-tx-server.service   # Web TX Server
/etc/systemd/system/frn-stream@.service     # Stream pro Raum (Template)
```

Beispiel Stream-Service (`/etc/systemd/system/frn-stream@quasel.service`):

```ini
[Unit]
Description=FRN Stream - %i
After=network.target icecast2.service

[Service]
EnvironmentFile=/opt/FRN/stream/stream.env
ExecStart=/opt/FRN/stream/run_stream.sh "Quasel-Ecke" %i TX-%i tx@example.com passwort
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

## Bekannte Einschränkungen / Hilfe gesucht

### Sprecher-Zuordnung im Archiv (client_idx-Semantik unbekannt)

Jedes Audio-Paket im FRN-Protokoll enthält einen 2-Byte-`client_idx`, der laut Protokollstruktur den sendenden Client identifizieren soll. Der FRN-Server sendet außerdem regelmäßig eine `MARKER_CLIENTS`-Liste mit allen verbundenen Stationen.

**Problem:** Der empfangene `client_idx` zeigt in unseren Tests konstant auf Position 0 der Clientliste — unabhängig davon, welche Station tatsächlich sendet. Es ist unklar ob:

- `client_idx` ein direkter Array-Index in die empfangene Clientliste ist (was nicht funktioniert),
- oder ob der Server eine interne Slot-Nummer vergibt, die nicht mit der Sortierung der `MARKER_CLIENTS`-Liste übereinstimmt,
- oder ob die Semantik noch anders ist (z. B. Connection-ID, Ring-Buffer-Position o. ä.).

Da das FRN-Protokoll proprietär und nicht öffentlich dokumentiert ist, ist die genaue Bedeutung von `client_idx` unbekannt.

**Relevant in [`frn_stream.py`](frn_stream.py), Funktion `_parse_client_list()` und `MARKER_SOUND`-Handler.**

Wer Einblick in die FRN-Protokollspezifikation hat oder den Java-Client analysiert hat — ein Hinweis als Issue wäre sehr willkommen!

## Lizenz

MIT
