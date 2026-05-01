#!/usr/bin/env python3
"""Add or update a TX user.  Usage: python3 tx_add_user.py <username> <callsign>"""
import hashlib, getpass, json, sys
from pathlib import Path

USERS_FILE = Path(__file__).parent / "tx_users.json"

username = sys.argv[1] if len(sys.argv) > 1 else input("Benutzername: ")
callsign = sys.argv[2] if len(sys.argv) > 2 else input("Callsign: ")
password = getpass.getpass("Passwort: ")
pw_hash  = hashlib.sha256(password.encode()).hexdigest()

data = json.loads(USERS_FILE.read_text()) if USERS_FILE.exists() else {"users": []}
data["users"] = [u for u in data["users"] if u["username"] != username]
data["users"].append({"username": username, "password_hash": pw_hash, "callsign": callsign})
USERS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
print(f"Benutzer '{username}' ({callsign}) gespeichert.")
