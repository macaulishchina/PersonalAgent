"""Public locators, browser-bound requests and fixed-duration access leases."""

import re
import secrets
import time
from collections import OrderedDict
from .links import IDENT, digest

REQUEST_COOKIE = "personal_agent_access_request"
VISIT_COOKIE = "personal_agent_visit"
REQUEST_SECONDS = 300
CLAIM_SECONDS = 60
MAX_SECONDS = 365 * 86400


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(IDENT, value):
        raise ValueError("Invalid access identity")
    return value


def bearer(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", value)


class AccessRequests:
    def __init__(self, db, credential):
        self.db, self.credential = db, credential
        db.executescript("""
            CREATE TABLE IF NOT EXISTS web_entrances (
                id TEXT PRIMARY KEY, session TEXT NOT NULL, title TEXT NOT NULL,
                endpoint TEXT NOT NULL, permission TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS web_access_requests (
                id TEXT PRIMARY KEY, entry TEXT NOT NULL, claim BLOB NOT NULL, code TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, deadline REAL NOT NULL,
                decided REAL, seconds INTEGER, permission TEXT NOT NULL, credential BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS web_requests_entry ON web_access_requests(entry,state);
            CREATE TABLE IF NOT EXISTS web_entry_access (
                id TEXT PRIMARY KEY, entry TEXT NOT NULL, kind TEXT NOT NULL, binding BLOB NOT NULL,
                request TEXT UNIQUE, created REAL NOT NULL, expires REAL NOT NULL,
                credential BLOB NOT NULL, closed INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS web_entry_binding ON web_entry_access(entry,kind,binding);
        """)
        self.attempts = OrderedDict()

    def create(self, session, title, endpoint, permission="chat"):
        identity(session)
        if (
            not isinstance(endpoint, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]*:[A-Za-z0-9_-]{1,96}", endpoint)
            or len(endpoint) > 128
            or permission not in {"read", "chat"}
            or not isinstance(title, str)
            or not 1 <= len(title.strip()) <= 120
        ):
            raise ValueError("Invalid access entrance")
        if self.db.execute("SELECT count(*) FROM web_entrances WHERE revoked=0").fetchone()[0] >= 128:
            raise ValueError("访问入口过多，请先关闭旧入口")
        ident = secrets.token_hex(16)
        with self.db:
            self.db.execute(
                "INSERT INTO web_entrances VALUES(?,?,?,?,?,0)",
                (ident, session, title.strip(), endpoint, permission),
            )
        return {"id": ident, "path": "/chat/e/" + ident + "/"}

    def entry(self, ident):
        identity(ident)
        row = self.db.execute(
            "SELECT session,title,endpoint,permission FROM web_entrances WHERE id=? AND revoked=0", (ident,)
        ).fetchone()
        if not row:
            raise PermissionError("访问入口已关闭或不存在")
        return dict(zip(("session_id", "title", "endpoint_id", "permission"), row))

    def entries(self):
        return [
            dict(zip(("id", "session_id", "title", "endpoint_id", "permission", "revoked"), row))
            for row in self.db.execute(
                "SELECT id,session,title,endpoint,permission,revoked FROM web_entrances ORDER BY rowid DESC LIMIT 512"
            )
        ]

    def revoke(self, ident):
        identity(ident)
        with self.db:
            self.db.execute("UPDATE web_entrances SET revoked=1 WHERE id=?", (ident,))
            self.db.execute(
                "UPDATE web_access_requests SET state='cancelled' WHERE entry=? AND state IN ('pending','approved')",
                (ident,),
            )
            self.db.execute("UPDATE web_entry_access SET closed=1 WHERE entry=?", (ident,))

    def prune(self):
        now = time.time()
        self.db.execute(
            "UPDATE web_access_requests SET state='expired' WHERE state='pending' AND deadline<=?", (now,)
        )
        self.db.execute(
            "UPDATE web_access_requests SET state='expired' WHERE state='approved' AND decided+?<=?",
            (CLAIM_SECONDS, now),
        )
        self.db.execute(
            "UPDATE web_access_requests SET state='expired' WHERE state IN ('pending','approved') AND credential!=?",
            (self.credential,),
        )
        self.db.execute(
            "UPDATE web_entry_access SET closed=1 WHERE kind='account' AND binding NOT IN (SELECT id FROM web_sessions WHERE expires>? AND credential=?)",
            (now, self.credential),
        )
        self.db.execute("DELETE FROM web_entry_access WHERE expires<?", (now - 86400,))
        self.db.execute(
            "DELETE FROM web_access_requests WHERE deadline<? AND id NOT IN (SELECT request FROM web_entry_access WHERE request IS NOT NULL)",
            (now - 86400,),
        )

    def lookup(self, entry, claim):
        self.entry(entry)
        if not bearer(claim):
            return None
        row = self.db.execute(
            "SELECT id,code,state,created,deadline,decided,seconds,permission,credential FROM web_access_requests WHERE entry=? AND claim=?",
            (entry, digest(claim)),
        ).fetchone()
        if not row:
            return None
        result = dict(
            zip(
                (
                    "id",
                    "code",
                    "state",
                    "created",
                    "deadline",
                    "decided",
                    "seconds",
                    "permission",
                    "credential",
                ),
                row,
            )
        )
        if result.pop("credential") != self.credential:
            result["state"] = "expired"
        elif result["state"] == "pending" and result["deadline"] <= time.time():
            result["state"] = "expired"
        elif result["state"] == "approved" and result["decided"] + CLAIM_SECONDS <= time.time():
            result["state"] = "expired"
        return result

    def start(self, entry, claim, peer):
        target = self.entry(entry)
        previous = self.lookup(entry, claim)
        if previous and previous["state"] in {"pending", "approved"}:
            return claim, previous
        now = time.time()
        count, since = self.attempts.get(peer, (0, now))
        if now - since >= 60:
            count, since = 0, now
        if count >= 6:
            raise ValueError("访问申请过于频繁，请一分钟后重试")
        self.attempts[peer] = count + 1, since
        if len(self.attempts) > 1024:
            self.attempts.popitem(last=False)
        with self.db:
            self.prune()
            total = self.db.execute(
                "SELECT count(*) FROM web_access_requests WHERE state IN ('pending','approved')"
            ).fetchone()[0]
            pending = self.db.execute(
                "SELECT count(*) FROM web_access_requests WHERE entry=? AND state IN ('pending','approved')",
                (entry,),
            ).fetchone()[0]
            if total >= 128 or pending >= 3:
                raise ValueError("设备已有访问申请等待处理，请稍后重试")
            ident, claim = secrets.token_hex(16), secrets.token_urlsafe(32)
            self.db.execute(
                "INSERT INTO web_access_requests VALUES(?,?,?,?, 'pending',?,?,NULL,NULL,?,?)",
                (
                    ident,
                    entry,
                    digest(claim),
                    f"{secrets.randbelow(1000000):06d}",
                    now,
                    now + REQUEST_SECONDS,
                    target["permission"],
                    self.credential,
                ),
            )
        return claim, self.lookup(entry, claim)

    def cancel(self, entry, claim):
        item = self.lookup(entry, claim)
        if item and item["state"] in {"pending", "approved"}:
            with self.db:
                self.db.execute(
                    "UPDATE web_access_requests SET state='cancelled' WHERE id=? AND state IN ('pending','approved')",
                    (item["id"],),
                )

    def pending(self, endpoint):
        with self.db:
            self.prune()
        return [
            dict(
                zip(("request_id", "entrance_id", "title", "permission", "code", "created", "deadline"), row)
            )
            for row in self.db.execute(
                "SELECT r.id,r.entry,e.title,r.permission,r.code,r.created,r.deadline FROM web_access_requests r JOIN web_entrances e ON e.id=r.entry WHERE e.endpoint=? AND e.revoked=0 AND r.state='pending' AND r.credential=? ORDER BY r.created LIMIT 32",
                (endpoint, self.credential),
            )
        ]

    def decide(self, endpoint, ident, decision, seconds=None):
        identity(ident)
        if (
            decision not in {"approve", "deny"}
            or (decision == "approve" and (type(seconds) is not int or not 1 <= seconds <= MAX_SECONDS))
            or (decision == "deny" and seconds is not None)
        ):
            raise ValueError("Invalid access decision or duration")
        with self.db:
            self.prune()
            row = self.db.execute(
                "SELECT r.entry,r.state,r.seconds,r.decided FROM web_access_requests r JOIN web_entrances e ON e.id=r.entry WHERE r.id=? AND e.endpoint=? AND e.revoked=0 AND r.credential=?",
                (ident, endpoint, self.credential),
            ).fetchone()
            if not row:
                raise PermissionError("此设备无权处理该访问申请")
            state = "approved" if decision == "approve" else "denied"
            if (
                row[1] in {state, "consumed"}
                and (row[1] != "consumed" or state == "approved")
                and row[2] == seconds
            ):
                return {"state": row[1], "expires": row[3] + seconds if seconds else None}
            if row[1] != "pending":
                raise ValueError("访问申请已处理或已过期")
            now = time.time()
            self.db.execute(
                "UPDATE web_access_requests SET state=?,decided=?,seconds=? WHERE id=? AND state='pending'",
                (state, now, seconds, ident),
            )
        return {"state": state, "expires": now + seconds if seconds else None}

    def claim(self, entry, claim, visit):
        item = self.lookup(entry, claim)
        if not item:
            raise PermissionError("访问申请无效")
        if item["state"] == "consumed":
            access = self.resolve(entry, "temporary", visit)
            if access["request_id"] != item["id"]:
                raise PermissionError("访问申请已使用，请重新申请")
            return None, access["expires"]
        if item["state"] != "approved":
            raise PermissionError("尚未获得有效批准")
        now = time.time()
        expires = item["decided"] + item["seconds"]
        if expires <= now:
            raise PermissionError("访问授权已到期")
        token = secrets.token_urlsafe(32)
        with self.db:
            self.capacity()
            updated = self.db.execute(
                "UPDATE web_access_requests SET state='consumed' WHERE id=? AND state='approved'",
                (item["id"],),
            )
            if updated.rowcount != 1:
                raise PermissionError("访问申请已使用")
            self.db.execute(
                "INSERT INTO web_entry_access VALUES(?,?,'temporary',?,?,?,?,?,0)",
                (secrets.token_hex(16), entry, digest(token), item["id"], now, expires, self.credential),
            )
        return token, expires

    def capacity(self):
        self.prune()
        if (
            self.db.execute(
                "SELECT count(*) FROM web_entry_access WHERE closed=0 AND expires>?", (time.time(),)
            ).fetchone()[0]
            >= 256
        ):
            raise ValueError("访问连接过多，请先关闭旧访问")

    def account(self, entry, account):
        self.entry(entry)
        binding = bytes.fromhex(account["id"])
        try:
            return self.resolve(entry, "account", account["id"], hashed=True)
        except PermissionError:
            pass
        with self.db:
            self.capacity()
            self.db.execute(
                "INSERT INTO web_entry_access VALUES(?,?,'account',?,NULL,?,?,?,0)",
                (secrets.token_hex(16), entry, binding, time.time(), account["expires"], self.credential),
            )
        return self.resolve(entry, "account", account["id"], hashed=True)

    def resolve(self, entry, kind, token, *, hashed=False):
        target = self.entry(entry)
        if hashed:
            if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
                raise PermissionError("请重新登录")
            binding = bytes.fromhex(token)
        else:
            if not bearer(token):
                raise PermissionError("请申请访问")
            binding = digest(token)
        row = self.db.execute(
            "SELECT id,request,expires FROM web_entry_access WHERE entry=? AND kind=? AND binding=? AND credential=? AND closed=0 AND expires>? ORDER BY created DESC LIMIT 1",
            (entry, kind, binding, self.credential, time.time()),
        ).fetchone()
        if not row:
            raise PermissionError("访问已结束，请重新进入")
        return {
            **target,
            "id": row[0],
            "request_id": row[1],
            "expires": row[2],
            "kind": kind,
            "permission": "chat" if kind == "account" else target["permission"],
        }

    def accesses(self, accounts):
        active_accounts = {a["id"] for a in accounts}
        rows = self.db.execute(
            "SELECT a.id,a.kind,a.binding,a.created,a.expires,e.session,e.title,e.permission FROM web_entry_access a JOIN web_entrances e ON e.id=a.entry WHERE a.closed=0 AND a.expires>? AND a.credential=? AND e.revoked=0 ORDER BY a.created DESC LIMIT 512",
            (time.time(), self.credential),
        )
        return [
            {
                "id": r[0],
                "kind": r[1],
                "created": r[3],
                "expires": r[4],
                "session_id": r[5],
                "title": r[6],
                "permission": "chat" if r[1] == "account" else r[7],
            }
            for r in rows
            if r[1] != "account" or r[2].hex() in active_accounts
        ]

    def close(self, ident):
        identity(ident)
        with self.db:
            self.db.execute(
                "UPDATE web_entry_access SET closed=1 WHERE id=? AND credential=?", (ident, self.credential)
            )
