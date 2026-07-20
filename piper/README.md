# FRN Piper-TTS

Schnelle, **lokale** Sprachausgabe für den KI-Funker — läuft auf CPU (auch Raspberry Pi),
ohne GPU, ohne Cloud, ohne API-Key. Engine: [Piper](https://github.com/OHF-Voice/piper1-gpl).

Spricht dieselbe Schnittstelle wie `voice_server.py` (`POST /tts` → WAV), ist also ein
direkter, schnellerer Ersatz: einfach `voice.remote_url` in `config.json` auf diesen
Dienst zeigen lassen.

## Start

```bash
# 1. Stimme laden (deutsch, "Thorsten emotional", 8 Emotionen)
./models/download-thorsten.sh

# 2. Container bauen und starten
docker compose up -d --build
```

Danach lauscht der Dienst auf `127.0.0.1:9003`. In `config.json`:
```json
"voice": { "enabled": true, "remote_url": "http://127.0.0.1:9003/tts" }
```

## Tempo

Modell wird **einmal** beim Start geladen; danach ~0,7–1,4 s pro Satz auf einem
Raspberry Pi 5 (CPU). Zum Vergleich: XTTS-Voice-Clone braucht auf GPU 3–6 s.

## Endpunkte

| Route | Zweck |
|-------|-------|
| `POST /tts` | JSON `{text, speaker?}` → WAV (22,05 kHz mono). `speaker` = Emotion. |
| `GET /speakers` | Liste der Emotionen |
| `GET /health` | Status |

**Emotionen** (thorsten_emotional): `amused`, `angry`, `disgusted`, `drunk`,
`neutral`, `sleepy`, `surprised`, `whisper`. Standard über `PIPER_EMOTION`
(Compose-Env, default `amused`). Ein unbekannter `speaker` fällt auf die
Standard-Emotion zurück — dadurch bleibt die Schnittstelle zu `voice_server.py`
(das echte Sprecher-Namen schickt) kompatibel.

## Umgebungsvariablen

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| `PIPER_MODEL` | `/models/de_DE-thorsten_emotional-medium.onnx` | Pfad zur Stimme |
| `PIPER_EMOTION` | `amused` | Standard-Emotion |
| `PIPER_PORT` | `9003` | HTTP-Port |

Andere Stimmen: siehe [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices)
(z. B. `de_DE-thorsten-high` ohne Emotionen, oder `de_DE-kerstin`, `de_DE-eva_k`).
