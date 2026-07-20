#!/bin/bash
# Lädt die deutsche Piper-Stimme "Thorsten emotional" (8 Emotionen) in diesen Ordner.
set -e
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten_emotional/medium"
cd "$(dirname "$0")"
curl -fL "$BASE/de_DE-thorsten_emotional-medium.onnx"      -o de_DE-thorsten_emotional-medium.onnx
curl -fL "$BASE/de_DE-thorsten_emotional-medium.onnx.json" -o de_DE-thorsten_emotional-medium.onnx.json
echo "Fertig. Modell liegt in $(pwd)"
