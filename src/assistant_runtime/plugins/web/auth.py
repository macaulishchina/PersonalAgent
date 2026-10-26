"""Single-owner login; no model credentials are exposed to the browser."""

from assistant_runtime.api.system import system
from assistant_runtime.api.passwords import validate_password

import hashlib
import hmac
import os
import secrets
import time
from collections import OrderedDict


class Access:
    def __init__(self, path, db):
        from assistant_runtime.api.configuration_io import configuration_path

        path = configuration_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32) + "\n")
        if path.is_symlink():
            raise ValueError("Web credential cannot be a symlink")
        system.private(path)
        token = path.read_text(encoding="utf-8").strip()
        digest = hashlib.sha256(token.encode()).digest()
        validate_password(token)
        self.digest = digest
        self.db = db
        db.execute(
            "CREATE TABLE IF NOT EXISTS web_sessions (id BLOB PRIMARY KEY, expires REAL, credential BLOB)"
        )
        with db:
            db.execute(
                "DELETE FROM web_sessions WHERE credential != ? OR expires < ?", (self.digest, time.time())
            )
        self.attempts = OrderedDict()

    def login(self, token, peer):
        now = time.time()
        count, start = self.attempts.get(peer, (0, now))
        if now - start > 60:
            count, start = 0, now
        if count >= 10:
            raise ValueError("尝试过于频繁，请一分钟后再试")
        self.attempts[peer] = (count + 1, start)
        if len(self.attempts) > 1024:
            self.attempts.popitem(last=False)
        if not isinstance(token, str) or not hmac.compare_digest(
            hashlib.sha256(token.encode()).digest(), self.digest
        ):
            raise PermissionError("访问口令不正确")
        with self.db:
            self.db.execute("DELETE FROM web_sessions WHERE expires < ?", (now,))
        if self.db.execute("SELECT count(*) FROM web_sessions").fetchone()[0] >= 64:
            raise ValueError("登录设备过多，请先退出旧设备")
        session = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute(
                "INSERT INTO web_sessions VALUES(?,?,?)",
                (hashlib.sha256(session.encode()).digest(), now + 7 * 86400, self.digest),
            )
        return session

    def valid(self, session):
        if not session:
            return False
        key = hashlib.sha256(session.encode()).digest()
        row = self.db.execute(
            "SELECT expires FROM web_sessions WHERE id=? AND credential=?", (key, self.digest)
        ).fetchone()
        return bool(row and row[0] > time.time())

    def logout(self, session):
        with self.db:
            self.db.execute(
                "DELETE FROM web_sessions WHERE id=?", (hashlib.sha256(session.encode()).digest(),)
            )


class AuthRepository(Access):
    """Web-owned repository constructor for the framework's scoped store factory."""

    def __init__(self, db, path):
        super().__init__(path, db)
