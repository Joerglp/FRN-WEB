#!/usr/bin/env python3
"""Zentrale FRN-Konten (2026-11-09).

Passwoerter stehen nur noch an EINER Stelle: in der FRNPasswd.xml des
FRN-Servers (acl/). Welches Konto wofuer benutzt wird, steht passwortfrei in
konten.json:

    {
      "passwd_xml": "/opt/FRN/acl/FRNPasswd.xml",   # oder config/frn_konten.xml
      "server_properties": "/opt/FRN/server.properties",
      "server": "server@example.org",
      "raeume": {"quasel": {"stream": "stream@example.org", "tx": "tx@example.org"}},
      "repeater": ["repeater1@example.org"]
    }

Streams (run_stream.sh), TX-Server und Archiv holen sich Name/E-Mail/Passwort
von hier. Eigene Konten werden am Namen aus dieser Liste erkannt statt an
Namensanfaengen wie "stream-"/"web-". Ohne konten.json bleibt alles beim Alten
(z. B. im Docker-Image ohne eigenen FRN-Server).

CLI fuer run_stream.sh:   frn_konten.py env stream <mount>
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET

DATEN = Path(os.environ.get("FRN_DATEN") or Path(__file__).parent)
KONTEN_PFAD = Path(os.environ.get("FRN_KONTEN") or DATEN / "konten.json")
ROLLEN = ("stream", "tx")

_cache: dict = {}


def _sicherung(pfad: Path):
    """Kopie vor dem Schreiben -- in FRN_SICHERUNG bzw. /opt/FRN/backups/konten,
    nicht neben die Datei (acl/ liest der FRN-Server)."""
    if pfad.exists():
        ordner = Path(os.environ.get("FRN_SICHERUNG")
                      or ("/opt/FRN/backups/konten" if Path("/opt/FRN/backups").is_dir()
                          else KONTEN_PFAD.parent / "sicherung"))
        try:
            ordner.mkdir(parents=True, exist_ok=True)
        except OSError:
            ordner = pfad.parent
        ziel = ordner / f"{pfad.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(pfad, ziel)


def _schreibe_atomar(pfad: Path, text: str, encoding="utf-8"):
    tmp = pfad.with_name(pfad.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    try:
        st = pfad.stat()
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, st.st_mode & 0o7777)
    except (OSError, FileNotFoundError):
        pass
    os.replace(tmp, pfad)


def aktiv() -> bool:
    return KONTEN_PFAD.exists()


def lade_zuordnung() -> dict:
    """konten.json (gecacht nach mtime). Leeres dict, wenn nicht vorhanden."""
    try:
        mt = KONTEN_PFAD.stat().st_mtime
    except FileNotFoundError:
        return {}
    c = _cache.get("z")
    if c and c[0] == mt:
        return c[1]
    z = json.loads(KONTEN_PFAD.read_text(encoding="utf-8"))
    z.setdefault("raeume", {})
    z.setdefault("repeater", [])
    _cache["z"] = (mt, z)
    return z


def speichere_zuordnung(z: dict):
    _sicherung(KONTEN_PFAD)
    _schreibe_atomar(KONTEN_PFAD, json.dumps(z, indent=2, ensure_ascii=False) + "\n")
    _cache.pop("z", None)
    _cache.pop("eigene", None)


def passwd_pfad(z: dict | None = None) -> Path:
    z = lade_zuordnung() if z is None else z
    return Path(z.get("passwd_xml") or "/opt/FRN/acl/FRNPasswd.xml")


def lade_konten(z: dict | None = None) -> list[dict]:
    """Alle Konten der FRNPasswd.xml (gecacht nach mtime)."""
    pfad = passwd_pfad(z)
    try:
        mt = pfad.stat().st_mtime
    except FileNotFoundError:
        return []
    c = _cache.get("k")
    if c and c[0] == (pfad, mt):
        return c[1]
    wurzel = ET.parse(pfad).getroot()
    konten = []
    for u in wurzel.findall("User"):
        konten.append({
            "name":     (u.findtext("OperatorName") or "").strip(),
            "email":    (u.findtext("EMailAddress") or "").strip(),
            "password": (u.findtext("Password") or ""),
            "disabled": (u.findtext("disabled") or "").strip().lower() == "true",
        })
    _cache["k"] = ((pfad, mt), konten, (wurzel.findtext("disabled") or "").strip())
    return konten


def speichere_konten(konten: list[dict], z: dict | None = None):
    """FRNPasswd.xml neu schreiben (Format wie vom FRN-Server erwartet)."""
    pfad = passwd_pfad(z)
    lade_konten(z)
    liste_aus = (_cache.get("k") or (None, None, "true"))[2] or "true"
    zeilen = ['<?xml version="1.0" encoding="UTF-8" ?>', "<PasswdList>", "",
              f" <disabled>{escape(liste_aus)}</disabled>", ""]
    for k in konten:
        zeilen += [" <User>",
                   f"  <OperatorName>{escape(k['name'])}</OperatorName>",
                   f"  <EMailAddress>{escape(k['email'])}</EMailAddress>",
                   f"  <Password>{escape(k['password'])}</Password>",
                   f"  <disabled>{'true' if k.get('disabled') else 'false'}</disabled>",
                   " </User>", ""]
    zeilen.append("</PasswdList>")
    _sicherung(pfad)
    _schreibe_atomar(pfad, "\n".join(zeilen) + "\n")
    _cache.pop("k", None)
    _cache.pop("eigene", None)


def konto(email: str, konten: list[dict] | None = None) -> dict | None:
    if not email:
        return None
    e = email.strip().lower()
    for k in (lade_konten() if konten is None else konten):
        if k["email"].lower() == e:
            return k
    return None


def fuer(mount: str, rolle: str) -> dict | None:
    """Konto (name, email, password) fuer Raum+Rolle, sonst None."""
    z = lade_zuordnung()
    email = (z.get("raeume", {}).get(mount) or {}).get(rolle)
    return konto(email) if email else None


def eigene() -> dict:
    """{operatorname_klein: rolle} aller zugeordneten Konten."""
    z = lade_zuordnung()
    if not z:
        return {}
    konten = lade_konten(z)
    schluessel = (_cache.get("z", (None,))[0], (_cache.get("k") or (None,))[0])
    c = _cache.get("eigene")
    if c and c[0] == schluessel:
        return c[1]
    ergebnis = {}

    def merke(email, rolle):
        k = konto(email, konten)
        if k and k["name"]:
            ergebnis[k["name"].lower()] = rolle

    for raum in z.get("raeume", {}).values():
        for rolle in ROLLEN:
            merke(raum.get(rolle), rolle)
    merke(z.get("server"), "server")
    for e in z.get("repeater", []):
        merke(e, "repeater")
    _cache["eigene"] = (schluessel, ergebnis)
    return ergebnis


def rolle_von(name: str) -> str:
    """'stream' | 'tx' | 'server' | 'repeater' | '' fuer einen FRN-Namen."""
    try:
        return eigene().get((name or "").strip().lower(), "")
    except Exception:
        return ""


def setze_server_properties(email: str, password: str, z: dict | None = None):
    """sysman_auth_emailaddr/-password in server.properties setzen."""
    z = lade_zuordnung() if z is None else z
    pfad = Path(z.get("server_properties") or "/opt/FRN/server.properties")
    text = pfad.read_text(encoding="latin-1")
    for schl, wert in (("sysman_auth_emailaddr", email), ("sysman_auth_password", password)):
        neu, n = re.subn(rf"(?m)^{schl}=.*$", lambda _m: f"{schl}={wert}", text, count=1)
        if not n:
            neu = text.rstrip("\n") + f"\n{schl}={wert}\n"
        text = neu
    _sicherung(pfad)
    _schreibe_atomar(pfad, text, encoding="latin-1")


def _cli(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "env" and argv[1] in ROLLEN:
        k = fuer(argv[2], argv[1])
        if not k or not k["password"]:
            return 1
        print(f"FRN_CALLSIGN={shlex.quote(k['name'])}")
        print(f"FRN_EMAIL={shlex.quote(k['email'])}")
        print(f"FRN_PASSWORD={shlex.quote(k['password'])}")
        return 0
    if argv == ["liste"]:
        z = lade_zuordnung()
        for mount, raum in z.get("raeume", {}).items():
            for rolle in ROLLEN:
                k = konto(raum.get(rolle, ""))
                print(f"{mount:18} {rolle:7} {k['name'] if k else '-':32} {raum.get(rolle, '')}")
        k = konto(z.get("server", ""))
        print(f"{'(server)':18} {'server':7} {k['name'] if k else '-':32} {z.get('server', '')}")
        return 0
    print("Aufruf: frn_konten.py env <stream|tx> <mount> | liste", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
