"""Revocable conversation links, distinct from owner login and device credentials."""

import hashlib
import hmac
import re
import secrets
import time
from collections import OrderedDict

COOKIE = "personal_agent_view"
IDENT = r"[a-f0-9]{32}"


def digest(value):
    return hashlib.sha256(value.encode()).digest()


class Links:
    def __init__(self, db, credential):
        self.db, self.credential = db, credential
        db.executescript("""
            CREATE TABLE IF NOT EXISTS web_links (
                id TEXT PRIMARY KEY, token BLOB UNIQUE NOT NULL, session TEXT NOT NULL,
                title TEXT NOT NULL, permission TEXT NOT NULL, created REAL NOT NULL,
                expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, credential BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS web_link_browsers (
                id BLOB PRIMARY KEY, expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS web_link_authorizations (
                browser BLOB NOT NULL, link TEXT NOT NULL, PRIMARY KEY(browser,link)
            );
        """)
        self.attempts = OrderedDict()

    def list(self):
        return [
            dict(zip(("id", "session_id", "title", "permission", "created", "expires", "revoked"), row))
            for row in self.db.execute(
                "SELECT id,session,title,permission,created,expires,revoked FROM web_links "
                "WHERE credential=? ORDER BY created DESC LIMIT 512",
                (self.credential,),
            )
        ]

    def create(self, session, title, permission="chat", expires_in=0):
        if (
            not isinstance(session, str)
            or not re.fullmatch(IDENT, session)
            or not isinstance(title, str)
            or not 1 <= len(title.strip()) <= 120
            or permission not in {"read", "chat"}
            or type(expires_in) is not int
            or not 0 <= expires_in <= 365 * 86400
        ):
            raise ValueError("Invalid conversation link")
        now = time.time()
        active = self.db.execute(
            "SELECT count(*) FROM web_links WHERE credential=? AND revoked=0 AND (expires=0 OR expires>?)",
            (self.credential, now),
        ).fetchone()[0]
        if active >= 128:
            raise ValueError("快捷链接过多，请先撤销旧链接")
        ident, token = secrets.token_hex(16), secrets.token_urlsafe(32)
        with self.db:
            self.db.execute(
                "INSERT INTO web_links VALUES(?,?,?,?,?,?,?,0,?)",
                (
                    ident,
                    digest(token),
                    session,
                    title.strip(),
                    permission,
                    now,
                    now + expires_in if expires_in else 0,
                    self.credential,
                ),
            )
        return {"id": ident, "path": "/chat/c/" + ident + "/#" + token}

    def revoke(self, ident):
        with self.db:
            self.db.execute(
                "UPDATE web_links SET revoked=1 WHERE id=? AND credential=?", (ident, self.credential)
            )
            self.prune()

    def prune(self):
        """Expired/revoked/logged-out sessions cannot consume the browser limit."""
        now = time.time()
        self.db.execute(
            "DELETE FROM web_link_authorizations WHERE browser IN "
            "(SELECT id FROM web_link_browsers WHERE expires<=?) OR link IN "
            "(SELECT id FROM web_links WHERE revoked=1 OR (expires>0 AND expires<=?) OR credential!=?)",
            (now, now, self.credential),
        )
        self.db.execute(
            "DELETE FROM web_link_browsers WHERE expires<=? OR NOT EXISTS "
            "(SELECT 1 FROM web_link_authorizations a WHERE a.browser=web_link_browsers.id)",
            (now,),
        )

    def exchange(self, token, browser, peer, ident):
        now = time.time()
        count, start = self.attempts.get(peer, (0, now))
        if now - start > 60:
            count, start = 0, now
        if count >= 10:
            raise ValueError("尝试过于频繁，请一分钟后重试")
        self.attempts[peer] = count + 1, start
        if len(self.attempts) > 1024:
            self.attempts.popitem(last=False)
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise PermissionError("快捷链接无效或已撤销")
        row = self.db.execute(
            "SELECT id FROM web_links WHERE id=? AND token=? AND credential=? AND revoked=0 "
            "AND (expires=0 OR expires>?)",
            (ident, digest(token), self.credential, now),
        ).fetchone()
        if not row:
            raise PermissionError("快捷链接无效或已撤销")
        browser = browser if self.browser_valid(browser) else secrets.token_urlsafe(32)
        with self.db:
            self.prune()
            count = self.db.execute("SELECT count(*) FROM web_link_browsers").fetchone()[0]
            if count >= 128 and not self.browser_valid(browser):
                raise ValueError("登录设备过多，请先退出旧设备")
            self.db.execute(
                "INSERT INTO web_link_browsers VALUES(?,?) "
                "ON CONFLICT(id) DO UPDATE SET expires=excluded.expires",
                (digest(browser), now + 7 * 86400),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO web_link_authorizations VALUES(?,?)", (digest(browser), row[0])
            )
        return browser, row[0]

    def browser_valid(self, browser):
        if not isinstance(browser, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", browser):
            return False
        row = self.db.execute(
            "SELECT expires FROM web_link_browsers WHERE id=?", (digest(browser),)
        ).fetchone()
        return bool(row and row[0] > time.time())

    def resolve(self, browser, ident):
        if not self.browser_valid(browser) or not re.fullmatch(IDENT, ident):
            raise PermissionError("请重新打开此会话的快捷链接")
        row = self.db.execute(
            "SELECT l.session,l.title,l.permission,l.credential FROM web_links l "
            "JOIN web_link_authorizations a ON a.link=l.id "
            "WHERE a.browser=? AND l.id=? AND l.revoked=0 AND (l.expires=0 OR l.expires>?)",
            (digest(browser), ident, time.time()),
        ).fetchone()
        if not row or not hmac.compare_digest(row[3], self.credential):
            raise PermissionError("快捷链接无效或已撤销")
        return {"session_id": row[0], "title": row[1], "permission": row[2]}

    def logout(self, browser, ident):
        if isinstance(browser, str):
            with self.db:
                self.db.execute(
                    "DELETE FROM web_link_authorizations WHERE browser=? AND link=?", (digest(browser), ident)
                )
                self.prune()

    def accesses(self):
        rows = self.db.execute(
            "SELECT lower(hex(b.id)),l.id,l.session,l.title,l.permission,CASE WHEN l.expires>0 THEN min(b.expires,l.expires) ELSE b.expires END "
            "FROM web_link_browsers b JOIN web_link_authorizations a ON a.browser=b.id "
            "JOIN web_links l ON l.id=a.link WHERE b.expires>? AND l.credential=? AND l.revoked=0 "
            "AND (l.expires=0 OR l.expires>?)",
            (time.time(), self.credential, time.time()),
        )
        return [
            {
                "id": r[0] + ":" + r[1],
                "kind": "link",
                "session_id": r[2],
                "title": r[3],
                "permission": r[4],
                "created": None,
                "expires": r[5],
            }
            for r in rows
        ]

    def close_access(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{64}:[a-f0-9]{32}", ident):
            raise ValueError("Invalid link access")
        browser, link = ident.split(":")
        with self.db:
            self.db.execute(
                "DELETE FROM web_link_authorizations WHERE browser=? AND link=?",
                (bytes.fromhex(browser), link),
            )
            self.prune()
