"""Device-owned identities, expiring numeric pairing and playback receipts.

Long-lived credentials are hashed. Short-lived display codes live only in the
private repository and are never configuration exports or ordinary logs.
"""

import hashlib
import hmac
import json
import secrets
import time


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
        db.execute(
            "CREATE TABLE IF NOT EXISTS passport_pairing(device TEXT PRIMARY KEY,credential TEXT NOT NULL,code TEXT UNIQUE NOT NULL,expires REAL NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS passport_configuration(device TEXT NOT NULL,field TEXT NOT NULL,revision INTEGER NOT NULL,fingerprint TEXT NOT NULL,applied INTEGER NOT NULL DEFAULT 0,actual TEXT,PRIMARY KEY(device,field))"
        )
        db.commit()

    def begin(self, ident, credential):
        now = time.time()
        registered = self.db.execute(
            "SELECT credential,revoked,paired FROM passport_devices WHERE id=?", (ident,)
        ).fetchone()
        if registered:
            if (
                registered[1]
                or not registered[2]
                or not hmac.compare_digest(registered[0], digest(credential))
            ):
                raise PermissionError("device_pairing_rejected")
            return {"paired": True, "device_id": ident}
        with self.db:
            self.db.execute("DELETE FROM passport_pairing WHERE expires<=?", (now,))
            current = self.db.execute(
                "SELECT credential,code,expires FROM passport_pairing WHERE device=?", (ident,)
            ).fetchone()
            if current:
                if not hmac.compare_digest(current[0], digest(credential)):
                    raise PermissionError("device_pairing_rejected")
                return {"paired": False, "code": current[1], "expires": current[2]}
            if self.db.execute("SELECT COUNT(*) FROM passport_pairing").fetchone()[0] >= 32:
                raise ValueError("pairing_capacity")
            for _ in range(16):
                code = f"{secrets.randbelow(100000000):08d}"
                if not self.db.execute("SELECT 1 FROM passport_pairing WHERE code=?", (code,)).fetchone():
                    break
            else:
                raise ValueError("pairing_capacity")
            expires = now + 600
            self.db.execute(
                "INSERT INTO passport_pairing VALUES(?,?,?,?)", (ident, digest(credential), code, expires)
            )
        return {"paired": False, "code": code, "expires": expires}

    def challenge(self, code):
        row = self.db.execute(
            "SELECT device FROM passport_pairing WHERE code=? AND expires>?", (code, time.time())
        ).fetchone()
        if not row:
            raise PermissionError("pairing_code_invalid_or_expired")
        return row[0]

    def activate(self, code, name, session):
        # Read again inside the transaction after asynchronous session creation.
        with self.db:
            row = self.db.execute(
                "SELECT device,credential FROM passport_pairing WHERE code=? AND expires>?",
                (code, time.time()),
            ).fetchone()
            if not row:
                raise PermissionError("pairing_code_invalid_or_expired")
            self.db.execute(
                "INSERT INTO passport_devices(id,name,session,credential,pairing,expires,paired) VALUES(?,?,?,?, '',0,1)",
                (row[0], name, session, row[1]),
            )
            self.db.execute("DELETE FROM passport_pairing WHERE device=?", (row[0],))
        return {"device_id": row[0], "session_id": session, "paired": True}

    def list(self):
        return [
            dict(zip(("device_id", "name", "session_id", "paired", "revoked"), row))
            for row in self.db.execute(
                "SELECT id,name,session,paired,revoked FROM passport_devices ORDER BY rowid"
            )
        ]

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

    def configuration(self, device, values):
        """Derived versions, not another settings source. Secrets are not stored here."""
        result = {}
        previous = {
            r[0]: r[1:]
            for r in self.db.execute(
                "SELECT field,revision,fingerprint FROM passport_configuration WHERE device=?", (device,)
            )
        }
        with self.db:
            for field in previous.keys() | values.keys():
                value = values.get(field)
                encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                fingerprint = digest(encoded)
                old = previous.get(field)
                revision = old[0] if old and old[1] == fingerprint else (old[0] + 1 if old else 1)
                self.db.execute(
                    "INSERT INTO passport_configuration(device,field,revision,fingerprint) VALUES(?,?,?,?) "
                    "ON CONFLICT(device,field) DO UPDATE SET revision=excluded.revision,fingerprint=excluded.fingerprint",
                    (device, field, revision, fingerprint),
                )
                result[field] = {"version": revision, "value": value}
        return result

    def configuration_receipt(self, device, versions, actual):
        with self.db:
            for field, revision in versions.items():
                row = self.db.execute(
                    "SELECT revision FROM passport_configuration WHERE device=? AND field=?", (device, field)
                ).fetchone()
                if type(revision) is not int or not row or revision != row[0]:
                    raise ValueError("configuration_version_mismatch")
                self.db.execute(
                    "UPDATE passport_configuration SET applied=? WHERE device=? AND field=?",
                    (revision, device, field),
                )
            # Only harmless local preferences can be reflected. Network values,
            # credentials, transcripts and arbitrary device JSON are never saved.
            for field, value in actual.items():
                self.db.execute(
                    "UPDATE passport_configuration SET actual=? WHERE device=? AND field=?",
                    (json.dumps(value), device, field),
                )

    def configuration_state(self, device):
        return {
            r[0]: {"desired": r[1], "applied": r[2], "actual": json.loads(r[3]) if r[3] is not None else None}
            for r in self.db.execute(
                "SELECT field,revision,applied,actual FROM passport_configuration WHERE device=?", (device,)
            )
        }

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
        self.playback_state(device, message, revision, "played")

    def playback_state(self, device, message, revision, state):
        if state not in {"played", "stopped", "unavailable"}:
            raise ValueError("Invalid playback state")
        with self.db:
            self.db.execute(
                "UPDATE passport_playbacks SET state=? WHERE device=? AND message=? AND revision=?",
                (state, device, message, revision),
            )
