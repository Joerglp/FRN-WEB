#!/usr/bin/env python3
"""Admin-Passwort für den FRN-TX-Server setzen.

Fragt das Passwort verdeckt ab (geht nicht durch den Shell-Verlauf), schreibt
den PBKDF2-Hash in tx_users.json und startet den Dienst neu.

    python3 /opt/FRN/stream/set_admin_pw.py
"""
import json
import getpass
import hashlib
import secrets
import os

P = "/opt/FRN/stream/tx_users.json"


def main():
    # Ohne TTY (z.B. über Claude Codes ! ) kann getpass nicht interaktiv lesen.
    # Dann Passwort aus der Umgebungsvariable FRN_ADMIN_PW nehmen.
    pw = os.environ.get("FRN_ADMIN_PW")
    if pw is None:
        pw = getpass.getpass("Neues Admin-Passwort: ")
        if pw != getpass.getpass("Wiederholen: "):
            raise SystemExit("Passwörter stimmen nicht überein — abgebrochen.")
    if len(pw) < 8:
        raise SystemExit("Bitte mindestens 8 Zeichen.")

    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 240000)
    pw_hash = f"pbkdf2_sha256$240000${salt.hex()}${dk.hex()}"

    d = json.load(open(P))
    for u in d["users"]:
        if u["username"] == "admin":
            u["password_hash"] = pw_hash
            u["is_admin"] = True
            u["frn_only"] = False
            break
    else:
        raise SystemExit("admin-User nicht gefunden!")

    with open(P, "w") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(P, 0o600)
    os.system("systemctl restart frn-tx-server")
    print("OK — Admin-Passwort gesetzt, Dienst neu gestartet.")


if __name__ == "__main__":
    main()
