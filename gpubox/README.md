# GPU-Box (192.0.0.17)

`whisper_server.py` ist der Dienst, der auf der GPU-Box unter
`/home/administrator/whisper_server.py` laeuft (systemd `whisper-api.service`,
Port 9001). Die Datei hier ist die Arbeitskopie im Repo — die Box ist die
Produktion, das Repo nur die Sicherung.

Stand 2026-09-17: **eingespielt und aktiv**, die Box laeuft seitdem auf
`large-v3`. Sicherung der Vorversion liegt dort als
`whisper_server.py.bak-20260917-vor-hotwords`. Gegenueber der Vorversion:

- `WHISPER_MODEL` wirkt endlich. Bisher stand in der systemd-Unit
  `Environment=WHISPER_MODEL=large-v3`, der Code lud aber fest
  `large-v3-turbo` — und `/health` meldete das auch dann, wenn etwas anderes
  eingestellt war.
- `hotwords` als Formularfeld (Rufnamen der Runde). Der Pi schickt es seit
  2026-09-17 mit; ein `initial_prompt` aus der Anfrage hat jetzt Vorrang vor
  dem der Box, die Prompt-Echo-Pruefung nutzt dann denselben Text.
- Die Antwort liefert `avg_logprob` und `no_speech_prob` mit. Der Pi spart
  sich damit in gut der Haelfte der Faelle den zweiten Whisper-Lauf, den er
  sonst zur Verlaesslichkeitspruefung braucht. Leere Ergebnisse melden `null`,
  nicht `0.0` (das waere der bestmoegliche Wert).

Gemessener Modellvergleich (30 Archiv-Aufnahmen, je mit Hotwords):
`large-v3` mittlerer avg_logprob **-0.36** gegen **-0.53** bei
`large-v3-turbo`, inhaltlich klar besser ("Joerg, Joerg, Oi, Matsu." ->
"Robert, hier Eickelborn, Lippstadt."), dafuer 1.57x langsamer — Median
1.14 s pro Aufnahme, rund 10x Echtzeit.

## Einspielen

```bash
ssh administrator@192.0.0.17 'cp -a ~/whisper_server.py ~/whisper_server.py.bak-$(date +%Y%m%d-%H%M%S)'
scp gpubox/whisper_server.py administrator@192.0.0.17:whisper_server.py
ssh administrator@192.0.0.17 'sudo systemctl restart whisper-api && sleep 40 && curl -s localhost:9001/health'
```

Die Unit setzt `WHISPER_MODEL=large-v3` — nach dem Neustart laeuft also
large-v3. Zurueck geht es ohne Code-Aenderung ueber die Variable. `sudo`
verlangt auf der Box ein Passwort, `sudo -n` schlaegt also fehl.

Beim Testen: Kill- und Start-Befehl NIE in denselben ssh-Aufruf legen —
`pkill -f whisper_server_neu.py` trifft die eigene Remote-Shell mit, weil
ihre Kommandozeile das Muster enthaelt (Exit 255). Muster als
`"[w]hisper_server_neu"` schreiben und in getrennten Aufrufen absetzen.
