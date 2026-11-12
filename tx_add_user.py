#!/usr/bin/env python3
"""Web-Benutzer anlegen oder Passwort aendern (PBKDF2, wie frn_tx_server.py).

    python3 tx_add_user.py <benutzername> <callsign> [--admin] [--datei PFAD]

Ohne --datei: config/tx_users.json, falls es den Ordner config/ gibt,
sonst tx_users.json neben diesem Skript. Passwort wird verdeckt abgefragt
oder aus der Umgebungsvariable FRN_USER_PW genommen (z. B. fuer setup.sh).
"""
import argparse
import getpass
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

ITERATIONEN = 240000   # = _PBKDF2_ITERATIONS in frn_tx_server.py


def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, ITERATIONEN)
    return f"pbkdf2_sha256${ITERATIONEN}${salt.hex()}${dk.hex()}"


def main():
    hier = Path(__file__).resolve().parent
    std = hier / "config" / "tx_users.json" if (hier / "config").is_dir() else hier / "tx_users.json"
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("username")
    ap.add_argument("callsign")
    ap.add_argument("--admin", action="store_true", help="Admin-Rechte vergeben")
    ap.add_argument("--datei", default=str(std))
    a = ap.parse_args()

    pw = os.environ.get("FRN_USER_PW")
    if pw is None:
        pw = getpass.getpass("Passwort: ")
        if pw != getpass.getpass("Wiederholen: "):
            sys.exit("Passwörter stimmen nicht überein – abgebrochen.")
    if len(pw) < 8:
        sys.exit("Bitte mindestens 8 Zeichen.")

    pfad = Path(a.datei)
    daten = json.loads(pfad.read_text(encoding="utf-8")) if pfad.exists() else {"users": []}
    name = a.username.strip().lower()
    alt = next((u for u in daten["users"] if u.get("username", "").lower() == name), {})
    daten["users"] = [u for u in daten["users"] if u.get("username", "").lower() != name]
    daten["users"].append({
        **alt,      # uebrige Felder (z. B. voice) bleiben beim Passwortwechsel
        "username": name,
        "callsign": a.callsign,
        "is_admin": a.admin or alt.get("is_admin", False),
        "default_room": alt.get("default_room", ""),
        "frn_only": alt.get("frn_only", False),
        "password_hash": hash_pw(pw),
    })
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(json.dumps(daten, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(pfad, 0o600)
    print(f"Benutzer '{name}' ({a.callsign}{', Admin' if a.admin else ''}) in {pfad} gespeichert.")


if __name__ == "__main__":
    main()
