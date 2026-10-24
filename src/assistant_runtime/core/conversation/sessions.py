"""Stable conversation identities and explicit entrance bindings, independent of Pi."""

import sqlite3
import json
import time
import uuid
from contextlib import closing
from pathlib import Path
from assistant_runtime.api.system import system


class Sessions:
    def __init__(self, store):
        self.store, self.db = store, store.db
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "conversation_sessions" in tables:
            return
        old = set()
        if "chat_messages" in tables:
            old.update(r[0] for r in self.db.execute("SELECT DISTINCT session_id FROM chat_messages"))
        for table in ("chat_requests", "chat_result_receipts"):
            if table in tables:
                old.update(r[0] for r in self.db.execute(f"SELECT DISTINCT session_id FROM {table}"))
        if "job_result_claims" in tables:
            for (raw,) in self.db.execute("SELECT context FROM job_result_claims"):
                context = json.loads(raw)
                if context.get("session"):
                    old.add(context["session"])
        current = store.get("session_id")
        provider = store.get("pi.execution_session_id")
        if not current and provider and "sessions" in tables:
            row = self.db.execute(
                "SELECT id FROM sessions WHERE provider='pi' AND provider_id=?", (provider,)
            ).fetchone()
            current = row[0] if row else None
        if not current and len(old) > 1:
            raise ValueError(
                "Ambiguous legacy main conversation; restore an explicit session_id before migration"
            )
        main = current or next(iter(old), None) or uuid.uuid4().hex
        old.add(main)
        # A stopped business startup owns this connection. Preserve the original DB before conversion.
        path = self.db.execute("PRAGMA database_list").fetchone()[2]
        if path and (current or provider or "chat_messages" in tables):
            self.db.commit()
            backup = Path(path).with_name("conversation-before-migration-" + uuid.uuid4().hex + ".sqlite3")
            backup.touch(mode=0o600)
            system.private(backup)
            with closing(sqlite3.connect(backup)) as target:
                self.db.backup(target)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "CREATE TABLE conversation_sessions(id TEXT PRIMARY KEY,title TEXT NOT NULL,created REAL NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE conversation_bindings(entrance TEXT PRIMARY KEY,session_id TEXT NOT NULL)"
            )
            self.db.executemany(
                "INSERT INTO conversation_sessions VALUES(?,?,?)",
                [(ident, "Main" if ident == main else "Imported", time.time()) for ident in old],
            )
            self.db.execute("INSERT INTO conversation_bindings VALUES('web',?)", (main,))

    def get(self, ident):
        row = self.db.execute(
            "SELECT id,title,created FROM conversation_sessions WHERE id=?", (ident,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown conversation")
        return dict(zip(("id", "title", "created"), row))

    def create(self, title="New conversation"):
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            raise ValueError("Conversation title must be 1..120 characters")
        ident = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO conversation_sessions VALUES(?,?,?)", (ident, title, time.time()))
        return self.get(ident)

    def list(self, before=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Conversation page limit must be 1..100")
        if before is not None:
            self.get(before)
        rows = self.db.execute(
            "SELECT id FROM conversation_sessions WHERE ? IS NULL OR rowid < "
            "(SELECT rowid FROM conversation_sessions WHERE id=?) ORDER BY rowid DESC LIMIT ?",
            (before, before, limit),
        ).fetchall()
        return {
            "sessions": [self.get(row[0]) for row in rows],
            "before": rows[-1][0] if len(rows) == limit else None,
        }

    def bound(self, entrance):
        row = self.db.execute(
            "SELECT session_id FROM conversation_bindings WHERE entrance=?", (entrance,)
        ).fetchone()
        return row[0] if row else None

    def bind(self, entrance, ident):
        self.get(ident)
        with self.db:
            self.db.execute(
                "INSERT INTO conversation_bindings VALUES(?,?) ON CONFLICT(entrance) DO UPDATE SET session_id=excluded.session_id",
                (entrance, ident),
            )
