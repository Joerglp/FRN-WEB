#!/usr/bin/env bash
# FRN-WEB – geführte Einrichtung
#
#   ./setup.sh
#
# Fragt Schritt für Schritt alles ab, was für den Betrieb nötig ist, und legt
# die Konfiguration unter config/ an – für Docker oder für den Betrieb per
# systemd (z. B. Raspberry Pi). Vorhandene Dateien werden nie ohne Rückfrage
# überschrieben; vor dem Überschreiben entsteht eine Sicherung (*.bak-DATUM).
# Das Skript kann gefahrlos mehrfach laufen.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
HIER="$(pwd)"
CFG="$HIER/config"
STEMPEL="$(date +%Y%m%d-%H%M%S)"

# ── Ausgabe-Helfer ──────────────────────────────────────────────────────────
if [ -t 1 ]; then
    B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'
else
    B=""; G=""; Y=""; R=""; C=""; N=""
fi
titel() { printf '\n%s━━ %s ━━%s\n' "$B$C" "$1" "$N"; }
info()  { printf '%s\n' "$*"; }
ok()    { printf '%s✔ %s%s\n' "$G" "$*" "$N"; }
warn()  { printf '%s! %s%s\n' "$Y" "$*" "$N"; }
fehler(){ printf '%s✘ %s%s\n' "$R" "$*" "$N" >&2; }

# frage VAR "Text" [Vorgabe]
frage() {
    local __var=$1 __text=$2 __vor=${3:-} __ein
    if [ -n "$__vor" ]; then
        read -r -p "$__text [$__vor]: " __ein || true
        __ein=${__ein:-$__vor}
    else
        while :; do
            read -r -p "$__text: " __ein || true
            [ -n "$__ein" ] && break
            warn "Bitte etwas eingeben."
        done
    fi
    printf -v "$__var" '%s' "$__ein"
}

# ja_nein "Frage" [j|n]  → Rückgabe 0 = ja
ja_nein() {
    local vor=${2:-j} ein hinweis="[J/n]"
    [ "$vor" = n ] && hinweis="[j/N]"
    read -r -p "$1 $hinweis: " ein || true
    ein=${ein:-$vor}
    [[ "$ein" =~ ^[jJyY] ]]
}

# geheim VAR "Text"  (verdeckt, zweimal)
geheim() {
    local __var=$1 __text=$2 a b
    while :; do
        read -r -s -p "$__text: " a || true; echo
        [ -z "$a" ] && { warn "Darf nicht leer sein."; continue; }
        read -r -s -p "Wiederholen: " b || true; echo
        [ "$a" = "$b" ] && break
        warn "Stimmt nicht überein, nochmal."
    done
    printf -v "$__var" '%s' "$a"
}

zufall() { python3 -c 'import secrets; print(secrets.token_urlsafe(18))'; }

# Datei schreiben dürfen? (fragt bei vorhandener Datei, sichert sie)
darf_schreiben() {
    local f=$1
    if [ -e "$f" ]; then
        if ja_nein "  $f gibt es schon – überschreiben (Sicherung wird angelegt)?" n; then
            cp -a "$f" "$f.bak-$STEMPEL"
            return 0
        fi
        return 1
    fi
    return 0
}

port_offen() {  # host port
    timeout 3 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null
}

# ── Start ───────────────────────────────────────────────────────────────────
cat <<EOF
${B}FRN-WEB Einrichtung${N}

Dieses Skript richtet ein:
  • Web-Oberfläche zum Mithören und Senden (PTT) mit Admin-Konto
  • Räume mit je einem FRN-Konto zum Senden (TX) und einem zum Mithören (Stream)
  • Icecast-Streams und Funkarchiv
  • optional KI-Funktionen (Transkription, Sprachausgabe, KI-Funker)

Voraussetzung ist ein erreichbarer FRN-Server (FRN_Server.jar, nicht Teil
dieses Projekts) und pro Raum zwei beim SystemManager registrierte FRN-Konten.
Abbrechen jederzeit mit Strg+C – bis zum jeweiligen Schritt Geschriebenes bleibt.
EOF

command -v python3 >/dev/null || { fehler "python3 fehlt – bitte zuerst installieren."; exit 1; }
mkdir -p "$CFG"

# ── 1. Betriebsart ──────────────────────────────────────────────────────────
titel "1/7 Betriebsart"
info "  1) Docker   – alles in Containern (TX-Server, Streams, Icecast, Whisper)"
info "  2) Nativ    – Python + systemd direkt auf dem Rechner (z. B. Raspberry Pi)"
frage ART "Auswahl" "1"
if [ "$ART" = 1 ]; then
    MODUS=docker
    if ! command -v docker >/dev/null; then
        fehler "docker ist nicht installiert (https://docs.docker.com/engine/install/)."; exit 1
    fi
    docker compose version >/dev/null 2>&1 || { fehler "'docker compose' (Plugin) fehlt."; exit 1; }
    ok "Docker gefunden: $(docker --version | cut -d, -f1)"
else
    MODUS=nativ
    PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
        || { fehler "Python $PYV ist zu alt (mindestens 3.10, getestet 3.13)."; exit 1; }
    ok "Python $PYV"
    FEHLT_APT=()
    command -v ffmpeg >/dev/null || FEHLT_APT+=(ffmpeg)
    ldconfig -p 2>/dev/null | grep -q libgsm.so || FEHLT_APT+=(libgsm1)
    for m in aiohttp:python3-aiohttp numpy:python3-numpy scipy:python3-scipy \
             requests:python3-requests paho.mqtt:python3-paho-mqtt; do
        python3 -c "import ${m%%:*}" 2>/dev/null || FEHLT_APT+=("${m##*:}")
    done
    if [ ${#FEHLT_APT[@]} -gt 0 ]; then
        warn "Es fehlen: ${FEHLT_APT[*]}"
        if command -v apt-get >/dev/null && ja_nein "Jetzt per apt installieren?"; then
            SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO=sudo
            $SUDO apt-get update && $SUDO apt-get install -y "${FEHLT_APT[@]}"
        else
            warn "Bitte selbst nachinstallieren (siehe README), dann weiter."
        fi
    else
        ok "ffmpeg, libgsm und Python-Pakete vorhanden"
    fi
fi

# ── 2. Variante ─────────────────────────────────────────────────────────────
titel "2/7 Basis oder mit KI"
info "  1) Basis    – Mithören, Senden, Archiv (keine KI-Server nötig)"
info "  2) Mit KI   – zusätzlich Transkription, Sprachausgabe, KI-Funker"
info "               (Whisper-/TTS-/LLM-Dienste laufen separat, siehe README)"
frage VAR "Auswahl" "1"
VORLAGE="$CFG/config.json.example"; [ "$VAR" = 2 ] && VORLAGE="$CFG/config.ai.json.example"
if darf_schreiben "$CFG/config.json"; then
    cp "$VORLAGE" "$CFG/config.json"; chmod 600 "$CFG/config.json"
    ok "config/config.json aus $(basename "$VORLAGE") angelegt"
else
    info "  config/config.json bleibt, wie sie ist."
fi

# ── 3. FRN-Server ───────────────────────────────────────────────────────────
titel "3/7 FRN-Server"
if [ "$MODUS" = docker ]; then
    info "  Läuft der FRN-Server auf diesem Rechner, ist das 'host.docker.internal'."
    VOR_HOST=host.docker.internal
else
    VOR_HOST=localhost
fi
frage FRN_HOST "Adresse des FRN-Servers" "$VOR_HOST"
frage FRN_PORT "Port" "10024"
TEST_HOST=$FRN_HOST; [ "$TEST_HOST" = host.docker.internal ] && TEST_HOST=localhost
if port_offen "$TEST_HOST" "$FRN_PORT"; then
    ok "FRN-Server $TEST_HOST:$FRN_PORT antwortet"
else
    warn "Keine Verbindung zu $TEST_HOST:$FRN_PORT – Einrichtung geht trotzdem weiter."
fi

# ── 4. Icecast & Oberfläche ─────────────────────────────────────────────────
titel "4/7 Icecast und Weboberfläche"
if [ "$MODUS" = docker ]; then
    ICE_HOST=icecast; ICE_PORT=8000
    ICE_SRC=$(zufall); ICE_ADM=$(zufall)
    # Ein frueheres "docker compose up" ohne setup.sh hat hier evtl. einen
    # leeren Ordner angelegt -- der muss weg, sonst scheitert das Schreiben.
    if [ -d "$CFG/icecast.xml" ]; then
        rmdir "$CFG/icecast.xml" 2>/dev/null || sudo rmdir "$CFG/icecast.xml" \
            || { echo "Bitte den Ordner $CFG/icecast.xml von Hand löschen." >&2; exit 1; }
    fi
    if darf_schreiben "$CFG/icecast.xml"; then
        sed -e "s#<source-password>[^<]*#<source-password>$ICE_SRC#" \
            -e "s#<relay-password>[^<]*#<relay-password>$(zufall)#" \
            -e "s#<admin-password>[^<]*#<admin-password>$ICE_ADM#" \
            "$CFG/icecast.xml.example" > "$CFG/icecast.xml"
        chmod 644 "$CFG/icecast.xml"
        ok "config/icecast.xml mit zufälligen Passwörtern angelegt"
    else
        ICE_SRC=$(sed -n 's#.*<source-password>\([^<]*\)<.*#\1#p' "$CFG/icecast.xml" | head -1)
        ICE_ADM=$(sed -n 's#.*<admin-password>\([^<]*\)<.*#\1#p' "$CFG/icecast.xml" | head -1)
    fi
else
    frage ICE_HOST "Icecast-Adresse" "localhost"
    frage ICE_PORT "Icecast-Port" "8000"
    ICE_SRC=""
    if [ -r /etc/icecast2/icecast.xml ]; then
        ICE_SRC=$(sed -n 's#.*<source-password>\([^<]*\)<.*#\1#p' /etc/icecast2/icecast.xml | head -1)
        [ -n "$ICE_SRC" ] && ok "Quell-Passwort aus /etc/icecast2/icecast.xml übernommen"
    fi
    [ -z "$ICE_SRC" ] && geheim ICE_SRC "Icecast source-password"
    ICE_ADM=""
fi
frage UI_TITEL "Titel der Weboberfläche" "FRN Webstreams"
frage TX_PORT "Port der Weboberfläche" "8765"
# Im Container lauscht der Server immer auf 8765 (Compose-Portabbildung,
# Healthcheck, frn_stream) -- der gewaehlte Port gilt dort nur nach aussen (.env).
if [ "$MODUS" = docker ]; then CFG_TX_PORT=8765; else CFG_TX_PORT="$TX_PORT"; fi

python3 - "$CFG/config.json" "$FRN_HOST" "$FRN_PORT" "$ICE_HOST" "$ICE_PORT" "$ICE_SRC" "$ICE_ADM" "$UI_TITEL" "$CFG_TX_PORT" <<'PY'
import json, sys
p, host, port, ih, ip, isrc, iadm, titel, txp = sys.argv[1:]
c = json.load(open(p, encoding="utf-8"))
c.setdefault("frn", {}).update(server=host, port=int(port))
ice = c.setdefault("icecast", {})
ice.update(host=ih, port=int(ip), source_password=isrc)
if iadm:
    ice["admin_password"] = iadm
c.setdefault("ui", {})["title"] = titel
c.setdefault("tx_server", {}).update(host="0.0.0.0", port=int(txp))
# Raeume kommen aus tx_rooms.json + konten.json, nicht aus dem Sammelkonto
fsa = c.get("frn_stream_account")
if isinstance(fsa, dict) and fsa.get("password") in ("DEIN_FRN_PASSWORT", None):
    fsa["email"] = ""; fsa["password"] = ""
json.dump(c, open(p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
PY
ok "config/config.json angepasst"

# ── 5. Admin-Konto ──────────────────────────────────────────────────────────
titel "5/7 Admin-Konto für die Weboberfläche"
ADMIN_ANLEGEN=1
if [ -e "$CFG/tx_users.json" ] && ! ja_nein "  config/tx_users.json gibt es schon – trotzdem einen Admin anlegen/ändern?" n; then
    ADMIN_ANLEGEN=0
fi
if [ "$ADMIN_ANLEGEN" = 1 ]; then
    frage ADM_USER "Benutzername" "admin"
    frage ADM_CALL "Rufzeichen/Anzeigename" "WebTX-Admin"
    geheim ADM_PW "Passwort (mind. 8 Zeichen)"
    FRN_USER_PW="$ADM_PW" python3 "$HIER/tx_add_user.py" "$ADM_USER" "$ADM_CALL" --admin \
        --datei "$CFG/tx_users.json"
fi

# ── 6. Räume und FRN-Konten ─────────────────────────────────────────────────
titel "6/7 Räume und FRN-Konten"
cat <<EOF
  Jeder Raum braucht ${B}zwei${N} FRN-Konten: eines zum Senden (TX – Web-PTT,
  KI-Funker) und eines zum Mithören (Stream – Icecast und Archiv). Ein
  FRN-Konto darf nur einmal gleichzeitig angemeldet sein, sonst weist der
  Server es mit AL=BLOCK ab. Konten beantragen: Admin-Panel → FRN-Server →
  "FRN-Konto registrieren" oder https://www.freeradionetwork.de

  Die Passwörter landen an genau einer Stelle (Kontenliste im Format der
  FRNPasswd.xml), die Zuordnung in config/konten.json. Später änderst du das
  im Admin-Panel unter Zugang → FRN-Konten.
EOF

KONTEN_XML="$CFG/frn_konten.xml"
KONTEN_XML_DIENST="$KONTEN_XML"
if [ "$MODUS" = docker ]; then
    KONTEN_XML_DIENST=/app/config/frn_konten.xml
elif [ -f /opt/FRN/acl/FRNPasswd.xml ] \
     && ja_nein "  Eigener FRN-Server gefunden – dessen /opt/FRN/acl/FRNPasswd.xml als Kontenliste nutzen?" j; then
    KONTEN_XML=/opt/FRN/acl/FRNPasswd.xml; KONTEN_XML_DIENST=$KONTEN_XML
fi

RAEUME_JSON="[]"
if [ -e "$CFG/konten.json" ] && ! ja_nein "  config/konten.json gibt es schon – Räume neu erfassen?" n; then
    info "  Räume bleiben unverändert."
else
    frage ANZ "Wie viele Räume?" "1"
    RAEUME=()
    for ((i = 1; i <= ANZ; i++)); do
        printf '\n%sRaum %d%s\n' "$B" "$i" "$N"
        frage R_NAME "  FRN-Raumname (genau wie auf dem Server)"
        VOR_MOUNT=$(printf '%s' "$R_NAME" | tr '[:upper:]' '[:lower:]' | sed -e 's/[^a-z0-9]\+/_/g' -e 's/^_//;s/_$//' | cut -c1-24)
        frage R_MOUNT "  Kurzname für URL/Stream (a-z, 0-9, _)" "${VOR_MOUNT:-raum$i}"
        info "  TX-Konto (Senden):"
        frage TX_NAME "    FRN-Name (Rufname, Name)" "Web-Funk, $R_NAME"
        frage TX_MAIL "    E-Mail"
        geheim TX_PW "    Passwort"
        info "  Stream-Konto (Mithören):"
        frage ST_NAME "    FRN-Name (Rufname, Name)" "Webradio, $R_NAME"
        while :; do
            frage ST_MAIL "    E-Mail"
            [ "${ST_MAIL,,}" != "${TX_MAIL,,}" ] && break
            warn "Muss ein anderes Konto als das TX-Konto sein."
        done
        geheim ST_PW "    Passwort"
        RAEUME+=("$(python3 -c 'import json,sys; print(json.dumps(dict(zip(
            ["name","mount","tx_name","tx_mail","tx_pw","st_name","st_mail","st_pw"], sys.argv[1:]))))' \
            "$R_NAME" "$R_MOUNT" "$TX_NAME" "$TX_MAIL" "$TX_PW" "$ST_NAME" "$ST_MAIL" "$ST_PW")")
    done
    RAEUME_JSON="[$(IFS=,; echo "${RAEUME[*]}")]"

    RAEUME_JSON="$RAEUME_JSON" python3 - "$HIER" "$CFG" "$KONTEN_XML" "$KONTEN_XML_DIENST" "$STEMPEL" <<'PY'
import json, shutil, sys
import os
from pathlib import Path
hier, cfg, xml_pfad, xml_dienst, stempel = sys.argv[1:]
os.environ["FRN_KONTEN"] = str(Path(cfg) / "konten.json")   # Sicherungen nach config/sicherung
sys.path.insert(0, hier)
import frn_konten as fk
cfg = Path(cfg)
raeume = json.loads(os.environ["RAEUME_JSON"])
z_lokal = {"passwd_xml": xml_pfad}
konten = [dict(k) for k in fk.lade_konten(z_lokal)] if Path(xml_pfad).exists() else []
def setze(name, mail, pw):
    k = fk.konto(mail, konten)
    if k:
        k.update(name=name, password=pw, disabled=False)
    else:
        konten.append({"name": name, "email": mail, "password": pw, "disabled": False})
for r in raeume:
    setze(r["tx_name"], r["tx_mail"], r["tx_pw"])
    setze(r["st_name"], r["st_mail"], r["st_pw"])
if not Path(xml_pfad).exists():
    Path(xml_pfad).write_text('<?xml version="1.0" encoding="UTF-8" ?>\n<PasswdList>\n <disabled>true</disabled>\n</PasswdList>\n', encoding="utf-8")
    Path(xml_pfad).chmod(0o600)
fk.speichere_konten(konten, z_lokal)
zuordnung = {
    "passwd_xml": xml_dienst,
    "server": "",
    "raeume": {r["mount"]: {"stream": r["st_mail"], "tx": r["tx_mail"]} for r in raeume},
    "repeater": [],
}
kj = cfg / "konten.json"
if kj.exists():
    shutil.copy2(kj, kj.with_name(f"konten.json.bak-{stempel}"))
kj.write_text(json.dumps(zuordnung, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
kj.chmod(0o600)
tr = cfg / "tx_rooms.json"
if tr.exists():
    shutil.copy2(tr, tr.with_name(f"tx_rooms.json.bak-{stempel}"))
tr.write_text(json.dumps({"rooms": [
    {"mount": r["mount"], "name": r["name"], "callsign": r["tx_name"],
     "email": r["tx_mail"], "konto": "zentral (konten.json)"} for r in raeume
]}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"  {len(raeume)} Raum/Räume, {2 * len(raeume)} Konten gespeichert")
PY
    ok "Kontenliste: $KONTEN_XML · Zuordnung: config/konten.json · Räume: config/tx_rooms.json"
fi

# ── 7. Starten ──────────────────────────────────────────────────────────────
titel "7/7 Starten"
if [ "$MODUS" = docker ]; then
    WH_URL=""
    if [ "$VAR" = 2 ]; then
        info "  Whisper: leer lassen = lokaler CPU-Container, sonst URL eines GPU-Rechners"
        read -r -p "  WHISPER_REMOTE_URL (z. B. http://192.168.1.50:9001/transcribe) []: " WH_URL || true
    fi
    if darf_schreiben "$HIER/.env"; then
        {
            echo "TX_PORT=$TX_PORT"
            echo "ICECAST_PORT=8000"
            [ -n "$WH_URL" ] && echo "WHISPER_REMOTE_URL=$WH_URL"
            echo "WHISPER_MODEL=small"
        } > "$HIER/.env"
        ok ".env angelegt"
    fi
    if ja_nein "Jetzt bauen und starten (docker compose up -d --build)?"; then
        docker compose up -d --build
        ok "Stack läuft – Logs: docker compose logs -f"
    else
        info "  Später starten mit: docker compose up -d --build"
    fi
else
    if [ "$(id -u)" -ne 0 ]; then
        warn "systemd-Dienste einrichten braucht root – Skript dafür mit sudo erneut starten."
        warn "Ohne Dienst testen: python3 frn_tx_server.py --config config/config.json --users config/tx_users.json --rooms config/tx_rooms.json"
    elif ja_nein "systemd-Dienste anlegen (frn-tx-server, frn-stream@<raum>)?"; then
        mkdir -p /opt/FRN/recordings /opt/FRN/archive/audio
        U_TX=/etc/systemd/system/frn-tx-server.service
        U_ST=/etc/systemd/system/frn-stream@.service
        if darf_schreiben "$U_TX"; then
            cat > "$U_TX" <<EOF
[Unit]
Description=FRN Web TX Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$HIER
Environment=FRN_KONTEN=$CFG/konten.json
Environment=FRN_CONFIG=$CFG/config.json
ExecStart=/usr/bin/python3 $HIER/frn_tx_server.py --config $CFG/config.json --users $CFG/tx_users.json --rooms $CFG/tx_rooms.json
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
            ok "$U_TX"
        fi
        if darf_schreiben "$U_ST"; then
            cat > "$U_ST" <<EOF
[Unit]
Description=FRN Audio Stream - %i
After=network.target icecast2.service

[Service]
Type=simple
WorkingDirectory=$HIER
Environment=FRN_KONTEN=$CFG/konten.json
Environment=FRN_TX_PORT=$TX_PORT
EnvironmentFile=$CFG/stream-%i.env
# Zugangsdaten holt run_stream.sh aus konten.json (zentrale Konten)
ExecStart=$HIER/run_stream.sh \${FRN_ROOM} \${FRN_MOUNT}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
            ok "$U_ST"
        fi
        MOUNTS=$(python3 -c 'import json,sys; [print(r["mount"]+"\t"+r["name"]) for r in json.load(open(sys.argv[1]))["rooms"]]' "$CFG/tx_rooms.json" 2>/dev/null || true)
        while IFS=$'\t' read -r M NAME; do
            [ -z "$M" ] && continue
            E="$CFG/stream-$M.env"
            if darf_schreiben "$E"; then
                printf 'FRN_ROOM=%s\nFRN_MOUNT=%s\nFRN_SERVER=%s\nFRN_PORT=%s\nICECAST_HOST=%s\nICECAST_PORT=%s\nICECAST_PASS=%s\n' \
                    "$NAME" "$M" "$FRN_HOST" "$FRN_PORT" "$ICE_HOST" "$ICE_PORT" "$ICE_SRC" > "$E"
                chmod 600 "$E"
            fi
        done <<< "$MOUNTS"
        systemctl daemon-reload
        if ja_nein "Dienste jetzt aktivieren und starten?"; then
            systemctl enable --now frn-tx-server
            while IFS=$'\t' read -r M NAME; do
                [ -n "$M" ] && systemctl enable --now "frn-stream@$M"
            done <<< "$MOUNTS"
            ok "Dienste laufen – Logs: journalctl -u frn-tx-server -f"
        fi
    fi
fi

# ── Fertig ──────────────────────────────────────────────────────────────────
IP=$(hostname -I 2>/dev/null | awk '{print $1}'); IP=${IP:-localhost}
cat <<EOF

${B}${G}Fertig.${N}
  Weboberfläche:  http://$IP:$TX_PORT   (Login mit dem Admin-Konto)
  Funkarchiv:     http://$IP:$TX_PORT/archive
  Icecast:        http://$IP:$( [ "$MODUS" = docker ] && echo 8000 || echo "$ICE_PORT" )

Nächste Schritte:
  • Admin-Panel → Zugang → FRN-Konten: Konten und Räume prüfen/ändern
  • Mit KI: Admin-Panel → KI-Dienste, Adressen von Whisper/TTS/LLM eintragen
  • Ausführliche Anleitung: README.md
EOF
