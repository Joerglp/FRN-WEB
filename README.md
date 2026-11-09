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
- **Transkription** *(optional, KI)* — Spracherkennung via [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- **Sprachausgabe** *(optional, KI)* — Text in einer geklonten Stimme in den Funk senden (Voice-Clone-TTS)
- **KI-Funker** *(optional, KI)* — autonomer Gesprächspartner, der mithört und selbstständig per Stimme antwortet
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
- **Geführte Einrichtung** — `./setup.sh` fragt alles Nötige ab, für Docker oder systemd
- **Zentrale FRN-Konten** — Zugangsdaten an einer Stelle, Zuordnung zu Räumen im Admin-Panel
- **Docker-Support** — kompletter Stack mit `docker compose up`

## Zwei Betriebsarten: Basis oder mit KI

FRN-WEB läuft in **einer** Codebasis, die KI-Funktionen sind ein optionaler Zusatz — niemand muss sie nutzen. Welche Variante du fährst, entscheidet allein die Konfiguration; es gibt keinen zweiten Branch und keinen getrennten Download.

| | **Basis** (ohne KI) | **Voll** (mit KI) |
|---|---|---|
| Mithören, Senden (PTT), Räume, Login, Admin | ✅ | ✅ |
| Funkarchiv (Audio + Chat) | ✅ | ✅ |
| Transkription der Sprüche (Whisper) | — | ✅ |
| Sprachausgabe / KI-Funker | — | ✅ |
| Zusätzliche Server nötig | — | Whisper-, TTS- und Ollama-Dienst (eigene Rechner) |
| Vorlage | `config/config.json.example` | `config/config.ai.json.example` |

Die KI-Server (Whisper, Voice-TTS, Ollama) laufen als **eigenständige Dienste** und werden nur über ihre Adresse in der Config eingebunden — die App selbst bringt **keine** KI-Bibliotheken mit (sie braucht nur `aiohttp`, `numpy`, `scipy`). Fehlt eine Adresse oder steht ein Feature auf `enabled: false`, ist es sauber aus und wird in der Oberfläche ausgeblendet. In der KI-Vorlage stehen alle KI-Features bewusst auf `enabled: false` — man schaltet sie einzeln frei, wenn der jeweilige Server läuft.

**Umschalten** heißt einfach: die passende Vorlage nach `config/config.json` kopieren (siehe Einrichtung, `setup.sh` fragt danach). Auch nachträglich kannst du KI in einer laufenden Basis-Installation dazuschalten, indem du die entsprechenden Blöcke ergänzt.

## Voraussetzungen

- Laufender **FRN_Server** (Java JAR, nicht in diesem Repo enthalten)
- **Icecast2** — im Docker-Stack enthalten, oder extern
- Python 3.13 (Debian 13, so im Betrieb getestet; mindestens 3.10) mit den Paketen aus `requirements.txt`
- `libgsm1` und `ffmpeg` installiert

## Einrichtung mit `setup.sh` (empfohlen)

```bash
git clone https://github.com/Joerglp/FRN-WEB.git
cd FRN-WEB
./setup.sh
```

Das Skript führt in sieben Schritten durch die Einrichtung und legt alles unter `config/` an:

1. **Betriebsart** – Docker oder nativ (Python + systemd, z. B. Raspberry Pi). Prüft Docker bzw. Python ≥ 3.10, `ffmpeg`, `libgsm1` und die Python-Pakete; Fehlendes kann es per `apt` nachinstallieren.
2. **Basis oder mit KI** – kopiert die passende Vorlage nach `config/config.json`.
3. **FRN-Server** – Adresse und Port, mit Verbindungstest.
4. **Icecast und Oberfläche** – bei Docker zufällige Icecast-Passwörter in `config/icecast.xml`, nativ wird das Passwort aus `/etc/icecast2/icecast.xml` übernommen.
5. **Admin-Konto** für die Weboberfläche (Passwort als PBKDF2-Hash in `config/tx_users.json`).
6. **Räume und FRN-Konten** – pro Raum ein TX- und ein Stream-Konto (siehe [FRN-Konten](#frn-konten)).
7. **Starten** – Docker: `.env` schreiben und `docker compose up -d --build`; nativ: systemd-Dienste `frn-tx-server` und `frn-stream@<raum>` anlegen und starten.

Vorhandene Dateien werden nur nach Rückfrage überschrieben und vorher gesichert (`*.bak-DATUM`), das Skript kann also gefahrlos erneut laufen – z. B. um Räume neu zu erfassen. Danach erreichbar unter:

- **Web-UI / PTT:** `http://<rechner>:8765`
- **Funkarchiv:** `http://<rechner>:8765/archive`
- **Icecast:** `http://<rechner>:8000`

> **Tester gesucht:** Im Produktivbetrieb läuft alles per systemd. Für Docker
> geprüft sind der Bau des TX-Images (Raspberry Pi 5, ARM64), die Kontenauflösung
> im Container und `docker compose config`; `setup.sh` ist in beiden Zweigen
> mit Testdaten durchgelaufen. Der komplette Stack mit `docker compose up` –
> FRN_Server, Icecast, Streams, Aufnahme und Transkription zusammen – ist
> **noch nicht im Echtbetrieb getestet**. Wer es ausprobiert, gibt bitte in
> [Issue #1](https://github.com/Joerglp/FRN-WEB/issues/1) Bescheid. Interessant sind vor allem:
>
> - Läuft `./setup.sh` bei dir ohne Stolpern durch?
> - Verbinden sich die Streams mit dem FRN_Server (`host.docker.internal`)?
> - Kommt der MP3-Stream bei Icecast an (`http://localhost:8000`)?
> - Landen Durchsagen als Aufnahme, Transkript und Archiv-Zeile im Funkarchiv?
> - Bleiben Nachrichten, Rückblick und Anmeldung nach
>   `docker compose up -d --build` erhalten (Volume `frn-daten`)?
> - Läuft es auch auf x86-64 (bisher nur ARM64 gebaut)?

### Docker-Details

Laufzeit-Daten des TX-Servers (Nachrichten, Tagesrückblick, Tagesprogramm,
Stimmproben, Sprachvorlagen in `stimmen_ref/` und `voices/`, Anmelde-Tokens)
liegen im Volume `frn-daten` (`/app/daten`, Umgebungsvariable `FRN_DATEN`),
Archiv und Aufnahmen in `frn-archive` und `frn-recordings`. `config/` wird
nach `/app/config` eingebunden; dort liegen auch `konten.json` und die
Kontenliste `frn_konten.xml` (`FRN_KONTEN=/app/config/konten.json`).

```bash
docker compose up -d --build      # bauen und starten
docker compose logs -f frn-web    # Logs
WHISPER_MODEL=small docker compose up -d   # anderes Whisper-Modell
```

Ohne `FRN_DATEN` (Betrieb per systemd) liegen die Daten neben dem Programm.

### Von Hand (ohne Skript)

```bash
apt install libgsm1 ffmpeg python3-aiohttp python3-numpy python3-scipy python3-requests python3-paho-mqtt
cp config/config.json.example config/config.json          # oder config.ai.json.example
cp config/tx_rooms.json.example config/tx_rooms.json
cp config/konten.json.example config/konten.json          # passwd_xml anpassen!
python3 tx_add_user.py admin "WebTX-Admin" --admin        # erster Admin

export FRN_KONTEN=$PWD/config/konten.json
python3 frn_tx_server.py --config config/config.json \
    --users config/tx_users.json --rooms config/tx_rooms.json
./run_stream.sh "Quasel-Ecke" quasel      # Stream je Raum, Konto aus konten.json
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
| `frn_stream_account.email` | Nur für Auto-Discovery der Räume (optional, sonst leer) |
| `frn_stream_account.password` | Passwort dazu |
| `ui.title` | Titel der Web-Oberfläche |
| `whisper.remote_url` | URL des externen Whisper-Servers (leer = lokal) |
| `whisper.model` | Modellgröße (`tiny`/`base`/`small`/`medium`) |
| `whisper.language` | Sprache (z.B. `de`, `en`) |

### `config/tx_users.json`

Web-Benutzer mit PBKDF2-Passwort-Hashes. Benutzer anlegen oder Passwort ändern:

```bash
python3 tx_add_user.py <benutzername> <callsign> [--admin]
```

Oder über das Admin-Panel im Browser (⚙ ADMIN nach Login).

### `config/tx_rooms.json`

Raumliste (Mount, FRN-Raumname, Anzeigename). Die Zugangsdaten stehen nicht
hier, sondern zentral (siehe unten). Ohne Räume und mit gesetztem
`frn_stream_account` entdeckt der Server die Räume beim Start selbst.

### FRN-Konten

Jeder Raum braucht **zwei** beim SystemManager registrierte FRN-Konten:

| Rolle | Wofür | Beispielname |
|-------|-------|--------------|
| **TX** | Senden aus dem Browser, KI-Funker | `Web-Funk, Quasel-Ecke` |
| **Stream** | Mithören, Icecast, Aufnahme fürs Archiv | `Webradio, Quasel-Ecke` |

Ein FRN-Konto darf nur **einmal gleichzeitig** angemeldet sein – ein zweiter
Login mit denselben Daten wird mit `AL=BLOCK` abgewiesen. Deshalb kann auch
nicht ein Konto für mehrere Räume dienen.

Die Zugangsdaten stehen an **einer** Stelle:

- **Kontenliste** im Format der `FRNPasswd.xml` (Name, E-Mail, Passwort) –
  `config/frn_konten.xml`, oder bei eigenem FRN-Server dessen
  `acl/FRNPasswd.xml`.
- **`config/konten.json`** (ohne Passwörter) ordnet die Konten zu: pro Raum
  `stream` und `tx`, dazu optional das Konto, mit dem sich der eigene
  FRN-Server beim SystemManager anmeldet (`server`), und Repeater-Konten.

```json
{
  "passwd_xml": "/app/config/frn_konten.xml",
  "server": "",
  "raeume": { "quasel": { "stream": "stream@example.org", "tx": "tx@example.org" } },
  "repeater": []
}
```

TX-Server, Streams (`run_stream.sh`, Docker: `frn_stream_runner.py`) und Archiv
lesen daraus. Eigene Konten erkennen sie an dieser Liste – Robert antwortet
also nicht auf die eigenen Streams, egal wie die Konten heißen. Ändern lässt
sich alles im Admin-Panel unter **Zugang → FRN-Konten**: Konten zuordnen,
umbenennen, Passwort setzen, sperren, anlegen. Übernommen wird bei Funkstille
(4 Minuten ohne Durchsage): Stream-Dienste und FRN-Server starten dann neu,
TX-Konten verbinden sich neu. Docker hat kein systemd – dort nach einer
Änderung `docker compose restart frn-streams`.

Fehlt `konten.json`, gilt das alte Verhalten (Zugangsdaten in `tx_rooms.json`
bzw. als Aufrufparameter von `run_stream.sh`).

## Admin-Panel

Nach Login mit einem Admin-Account: **⚙ ADMIN**-Button oben rechts. Die Reiter sind in Gruppen sortiert:

| Gruppe | Reiter |
|--------|--------|
| Übersicht | Zustand aller Räume, Dienste und KI-Funker auf einen Blick |
| Zugang | **Benutzer** (Web-Logins) · **Räume** (TX-Räume, live) · **FRN-Konten** (zentrale Konten, siehe oben) · **FRN-Server** (Server wechseln, Konto beim SystemManager beantragen) · **Crosslink** |
| Robert | KI-Funker, Gespräche analysieren, kompletter Prompt, Namens-Antwort |
| Funk & Audio | Transkription & Archiv, Sprecher-Erkennung |
| System | KI-Dienste, Dienste & Geräte, Räume & Verbindungen, Ablaufverfolgung |

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

## KI-Dienste einrichten (optional)

Die KI-Funktionen laufen als **drei eigenständige HTTP-Dienste** — die App bindet sie nur über ihre Adresse ein (`config/config.ai.json.example` als Vorlage). Jeder Dienst ist einzeln optional; du kannst z.B. nur Transkription nutzen und Sprachausgabe weglassen. Sie können alle auf demselben Rechner laufen oder verteilt (empfohlen: rechenintensive Dienste auf einen Rechner mit NVIDIA-GPU).

| Dienst | Skript | Port | Wozu | GPU? |
|--------|--------|------|------|------|
| **Whisper** | `whisper_server.py` | 9001 | Sprüche transkribieren (fürs Archiv + KI-Funker) | empfohlen, geht auch CPU |
| **Voice-TTS** | `voice_server.py` | 9002 | Text in einer Stimme aussprechen (Sprachausgabe, KI-Funker) | empfohlen, geht auch CPU |
| **Ollama** | (externes Projekt) | 11434 | Antworten formulieren (KI-Funker, Auto-Antwort) | empfohlen, geht auch CPU |

Faustregel: **Whisper + Voice-TTS + Ollama** zusammen wollen für flüssigen Betrieb eine GPU. Auf reiner CPU funktioniert alles, ist aber deutlich langsamer (Sprachsynthese z.B. ~20 s statt ~2 s pro Satz) — für einen gemütlichen Funk-Bot oft trotzdem okay.

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

## Sprachausgabe (Voice-Clone-TTS)

Wandelt Text in gesprochenes Audio um und sendet es in den Funk — entweder mit einer der eingebauten Studio-Stimmen oder mit einer aus einem kurzen Referenz-Sample geklonten Stimme. Engine: [Coqui XTTS-v2](https://github.com/idiap/coqui-ai-TTS). `voice_server.py` läuft als eigenständiger Dienst (Port 9002) und wird über `voice.remote_url` angesprochen.

> **Schnelle CPU-Alternative:** Wer keine GPU hat oder Tempo braucht, nutzt statt XTTS den **Piper**-Dienst (`piper/`, Port 9003) — dieselbe Schnittstelle, ~1 s pro Satz auf einem Raspberry Pi, dafür eine feste (nicht geklonte) Stimme. Siehe [`piper/README.md`](piper/README.md).

### Installation (nativ)

Die Abhängigkeiten sind heikel — diese **Versionen sind aufeinander abgestimmt**, neuere brechen XTTS:

```bash
python3 -m venv voice-env && source voice-env/bin/activate
pip install "coqui-tts==0.27.5" "transformers==4.57.6" aiohttp numpy
# CPU:
pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cpu
# ODER GPU (CUDA 12.x):
pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128

COQUI_TOS_AGREED=1 VOICE_DEVICE=cpu python3 voice_server.py   # Port 9002
```

Beim ersten Start lädt XTTS sein Modell (~1,8 GB, CPML-Lizenz — nur nicht-kommerzielle Nutzung).

### Stimmen

- **Eingebaut:** ~58 Studio-Sprecher, sofort nutzbar. Liste via `GET /speakers` (Feld `builtin`). In der Config als `speaker`-ID eintragen, z.B. `"speaker": "aaron_dreschner"`.
- **Eigene Stimme klonen:** ein ≥5 s sauberes WAV als `speaker`-Sample hochladen (`POST /speaker/<name>` mit rohem WAV-Body) oder als Datei ablegen. Danach `"speaker": "<name>"`.

### Umgebungsvariablen

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| `VOICE_DEVICE` | `cpu` | `cpu` \| `cuda` (dauerhaft GPU) \| `cuda-ondemand` (nur während Synthese auf GPU, sonst VRAM frei) |
| `VOICE_PORT` | `9002` | HTTP-Port |
| `VOICE_REF_DIR` | `/ref` | Ordner mit Sprecher-Samples (`<name>.wav`) |
| `COQUI_TOS_AGREED` | — | muss `1` sein (XTTS-Lizenz akzeptieren) |

Dann in `config/config.json`:
```json
"voice": { "enabled": true, "remote_url": "http://DEIN-TTS-SERVER:9002/tts" }
```

## KI-Funker & Auto-Antwort (Ollama)

Formuliert die Antworten. Läuft über [Ollama](https://ollama.com) (Port 11434) mit einem lokalen Sprachmodell — kein Cloud-Dienst, keine API-Kosten.

```bash
# Ollama installieren (siehe ollama.com), dann Modell(e) laden:
ollama pull qwen3:14b      # gut für den KI-Funker (deutsch, folgt Anweisungen)
ollama pull llama3.2:3b    # klein/schnell, reicht für kurze Auto-Antwort-Vorschläge
```

Zwei getrennte, unabhängig schaltbare Funktionen greifen darauf zu:

- **KI-Funker** (`voice.bot`) — hört alle Räume mit und antwortet **selbstständig** per Stimme, wenn sein Name fällt, jemand allgemein ruft (»jemand QRV?«, CQ) oder er in einem Gespräch steckt. Braucht Whisper (hören), Voice-TTS (sprechen) und Ollama (denken). Ob er wirklich gemeint ist, entscheidet das Modell selbst (es darf mit `SKIP` schweigen).
- **Auto-Antwort** (`voice.auto_reply`) — erzeugt bei Namensnennung einen Antwort-**Vorschlag**, der in der App erscheint; gesendet wird erst nach Freigabe (oder mit `auto_send: true` automatisch).

```json
"voice": {
  "enabled": true,
  "remote_url": "http://DEIN-TTS-SERVER:9002/tts",
  "bot": {
    "enabled": true,
    "name": "Robby",
    "speaker": "aaron_dreschner",
    "ollama_url": "http://DEIN-OLLAMA-SERVER:11434",
    "ollama_model": "qwen3:14b",
    "ollama_keep_alive": "2h"
  }
}
```

`ollama_keep_alive`: `0` entlädt das Modell nach jeder Antwort (gut bei geteilter GPU), `"2h"` hält es im RAM (schnellere Antworten auf einem eigenen Server). Alle Bot-Einstellungen sind auch live im **Admin-Panel → KI-FUNKER** editierbar, inkl. Trockentest ohne Senden.

> **Hinweis zur Fairness:** Ein Bot, der ungefragt in Gespräche funkt, kommt nicht überall gut an. Kläre die Nutzung auf deinem Netz/Kanal vorher ab und halte `cooldown_s` großzügig.

## Architektur

```
Browser
  │  WebSocket (PCM audio)              HTTP (Audio-Stream)
  │  HTTP (Archiv, Admin-API, /status)         ▲
  ▼                                            │
frn_tx_server.py ──── TX0/TX1 ────► FRN_Server.jar :10024
  │  (HTTP + WS, :8765)                        │
  │        ▲                          (Audio-Routing)
  │        │ POST /api/intern/sprechbeginn (nur localhost)
  │        │ → LLM vorwärmen, sobald jemand zu sprechen beginnt
  │  ┌─────┴───────────────────────────────────┘
  │  │
  │  └── frn_stream.py (pro Raum)
  │            │ PCM pipe
  │            ▼
  │       ffmpeg (GSM → MP3)
  │            │ Icecast source protocol
  │            ▼
  │       Icecast2 :8000
  │
  ├── frn_transcription.py
  │      │  Whisper large-v3 (remote GPU :9001, sonst faster-whisper CPU)
  │      │  Sprecher-Erkennung: ECAPA (:9005) gegen angelernte Proben
  │      ▼
  │   frn_archive.py (SQLite + Opus, inkl. Sprecher + Treffer-Wert)
  │      ▼
  │   /archive  (Chat-Verlauf Web-UI)
  │
  ├── KI-Funker (optional, Bot im frn_tx_server.py)
  │      │  Verlauf + Uhrzeit je Spruch + Raum-Hinweis → Prompt
  │      │  LLM: llama.cpp / Ollama-API (z. B. :11439)
  │      │  TTS: XTTS (:9002, gestreamt) oder Piper (:9003)
  │      ▼
  │   Antwort als Funkspruch (TX) zurück in den Raum
  │
  └── (Docker) frn_stream_runner.py — startet alle Streams, Watchdog
```

Die GPU-Dienste (Whisper, XTTS, Piper, ECAPA, LLM) laufen optional auf einer separaten Box, siehe [`gpubox/README.md`](gpubox/README.md). Die Seite `/status` (Admin-Login) zeigt, welche Dienste laufen und wie die GPUs ausgelastet sind.

## Systemd (ohne Docker)

`setup.sh` legt beide Dienste an:

```
/etc/systemd/system/frn-tx-server.service   # Web TX Server
/etc/systemd/system/frn-stream@.service     # Stream pro Raum (Template)
```

Stream-Dienst (Auszug); Raum und Icecast-Daten kommen aus
`config/stream-<mount>.env`, die FRN-Zugangsdaten aus `konten.json`:

```ini
[Service]
WorkingDirectory=/opt/FRN-WEB
Environment=FRN_KONTEN=/opt/FRN-WEB/config/konten.json
EnvironmentFile=/opt/FRN-WEB/config/stream-%i.env
ExecStart=/opt/FRN-WEB/run_stream.sh ${FRN_ROOM} ${FRN_MOUNT}
Restart=on-failure
```

`${VAR}` als eigenes Wort bleibt in systemd **ein** Argument, auch mit
Leerzeichen („Quasel-Ecke“, „stream, Joerg“); `$VAR` ohne Klammern würde an
Leerzeichen getrennt.

## Sprecher-Zuordnung

Jedes Audio-Paket im FRN-Protokoll trägt einen `client_idx`, und der Server schickt bei jedem Senden-Start und -Stopp mit `MARKER_CLIENTS` die Liste der verbundenen Stationen. `client_idx` ist die Position des sendenden Clients in dieser Liste, **gezählt ab 1** (verifiziert mit einem Gateway und einem zweiten, direkt sendenden FRN-Client: alle Durchgänge passten). Wer den Wert als Position ab 0 liest, landet immer einen Eintrag zu weit hinten.

Damit ist bekannt, **welcher FRN-Client** sendet – aber nicht, wer am Mikrofon spricht. In dieser Installation läuft der Funkverkehr über ein Gateway: Ein Funkgerät hängt per Soundkarte und PTT an einem Raspberry Pi, der als Client Audio ins FRN-Netz schickt. Alle, die auf dem CB-Kanal sprechen, erscheinen im FRN unter dem Namen des Gateways. [`frn_stream.py`](frn_stream.py) speichert den Client-Namen deshalb nicht als Sprecher.

Wer gesprochen hat, wird darum an der **Stimme** erkannt: Die GPU-Box berechnet für jede Aufnahme ein Stimm-Embedding (ECAPA, Port 9005), der Pi vergleicht es mit den angelernten Proben jeder Person. Ein Name wird nur vergeben, wenn die Ähnlichkeit über der Schwelle liegt und der Zweitbeste deutlich dahinter – lieber kein Name als ein falscher. Schwellen und Proben stehen im Admin-Panel unter *Funk & Audio → Sprecher-Erkennung*.

**Grenzen:** Über den bandbegrenzten, verrauschten Funkkanal trifft die Erkennung nur bei einem Teil der Durchsagen sicher (grob ein Drittel); der Rest erscheint als „Funker“. Kurze Durchsagen unter zwei Sekunden brauchen eine höhere Schwelle.

## Lizenz

MIT
