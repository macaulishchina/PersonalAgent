"""Device-owned identities and playback receipts. Tokens are never stored in plaintext."""

import hashlib
import hmac
import secrets
import time
import uuid


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Devices:
    def __init__(self, db):
        self.db = db
        db.execute(
            "CREATE TABLE IF NOT EXISTS passport_devices(id TEXT PRIMARY KEY,name TEXT NOT NULL,session TEXT NOT NULL,credential TEXT NOT NULL,pairing TEXT NOT NULL,expires REAL NOT NULL,paired INTEGER NOT NULL DEFAULT 0,revoked INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS passport_playbacks(device TEXT NOT NULL,message TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,PRIMARY KEY(device,message,revision))"
        )
        db.commit()

    def issue(self, name, session, ident=None):
        ident = ident or uuid.uuid4().hex
        credential, pairing = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires = time.time() + 600
        with self.db:
            self.db.execute(
                "INSERT INTO passport_devices(id,name,session,credential,pairing,expires) VALUES(?,?,?,?,?,?)",
                (ident, name, session, digest(credential), digest(pairing), expires),
            )
        return {"device_id": ident, "credential": credential, "pairing_token": pairing, "expires": expires}

    def list(self):
        return [
            dict(zip(("device_id", "name", "session_id", "paired", "revoked"), row))
            for row in self.db.execute(
                "SELECT id,name,session,paired,revoked FROM passport_devices ORDER BY rowid"
            )
        ]

    def pair(self, ident, pairing, credential):
        row = self.db.execute(
            "SELECT pairing,credential,expires,revoked FROM passport_devices WHERE id=?", (ident,)
        ).fetchone()
        if (
            not row
            or row[3]
            or row[2] < time.time()
            or not hmac.compare_digest(row[0], digest(pairing))
            or not hmac.compare_digest(row[1], digest(credential))
        ):
            raise PermissionError("device_pairing_rejected")
        # An identical exchange may recover a lost acknowledgement, never bind another identity.
        with self.db:
            self.db.execute("UPDATE passport_devices SET paired=1 WHERE id=?", (ident,))

    def authenticate(self, ident, credential):
        row = self.db.execute(
            "SELECT session,credential,paired,revoked FROM passport_devices WHERE id=?", (ident,)
        ).fetchone()
        if not row or not row[2] or row[3] or not hmac.compare_digest(row[1], digest(credential)):
            raise PermissionError("device_authentication_failed")
        return row[0]

    def revoke(self, ident):
        with self.db:
            self.db.execute("UPDATE passport_devices SET revoked=1 WHERE id=?", (ident,))

    def claim_playback(self, device, message, revision):
        with self.db:
            return (
                self.db.execute(
                    "INSERT OR IGNORE INTO passport_playbacks VALUES(?,?,?,'unknown')",
                    (device, message, revision),
                ).rowcount
                == 1
            )

    def played(self, device, message, revision):
        with self.db:
            self.db.execute(
                "UPDATE passport_playbacks SET state='played' WHERE device=? AND message=? AND revision=?",
                (device, message, revision),
            )
