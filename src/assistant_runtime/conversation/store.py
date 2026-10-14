"""Durable, ordered conversation projection and resumable change notifications."""

import json
import time


class ConversationStore:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                role TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                turn_id TEXT, attachments TEXT NOT NULL DEFAULT '[]', updated REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS chat_session_order ON chat_messages(session_id, ordinal);
            CREATE TABLE IF NOT EXISTS chat_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                message_id TEXT, kind TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_requests (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, message_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_context (message_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS chat_uploads (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, media_type TEXT NOT NULL,
                created REAL NOT NULL, attached INTEGER NOT NULL DEFAULT 0
            );
        """)

    def get(self, ident):
        row = self.db.execute("SELECT * FROM chat_messages WHERE id=?", (ident,)).fetchone()
        if not row:
            return None
        keys = (
            "id",
            "session_id",
            "ordinal",
            "role",
            "source",
            "kind",
            "text",
            "status",
            "turn_id",
            "attachments",
            "updated",
        )
        result = dict(zip(keys, row))
        result["attachments"] = json.loads(result["attachments"])
        return result

    def emit(self, session, kind, data, message_id=None):
        with self.db:
            cur = self.db.execute(
                "INSERT INTO chat_events(session_id,message_id,kind,data) VALUES(?,?,?,?)",
                (session, message_id, kind, json.dumps(data, ensure_ascii=False)),
            )
            seq = cur.lastrowid
            if seq % 500 == 0:
                self.db.execute("DELETE FROM chat_events WHERE seq < ?", (seq - 20000,))
        return seq

    def put(
        self,
        ident,
        session,
        *,
        role="assistant",
        source="task",
        kind="message",
        text="",
        status="streaming",
        turn_id=None,
        attachments=None,
        updated_at=None,
    ):
        old = self.get(ident)
        if old and old["session_id"] != session:
            raise ValueError("Message belongs to another session")
        text = text[:200000]
        attachments = attachments if attachments is not None else (old["attachments"] if old else [])
        values = (role, source, kind, text, status, turn_id, attachments)
        if old and values == tuple(
            old[k] for k in ("role", "source", "kind", "text", "status", "turn_id", "attachments")
        ):
            return old
        with self.db:
            seq = self.db.execute(
                "INSERT INTO chat_events(session_id,message_id,kind,data) VALUES(?,?,?,?)",
                (session, ident, "message", "{}"),
            ).lastrowid
            if seq % 500 == 0:
                self.db.execute("DELETE FROM chat_events WHERE seq < ?", (seq - 20000,))
            self.db.execute(
                """INSERT INTO chat_messages VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET role=excluded.role,source=excluded.source,
                kind=excluded.kind,text=excluded.text,status=excluded.status,
                turn_id=excluded.turn_id,attachments=excluded.attachments,updated=excluded.updated""",
                (
                    ident,
                    session,
                    old["ordinal"] if old else seq,
                    *values[:6],
                    json.dumps(attachments),
                    time.time() if updated_at is None else updated_at,
                ),
            )
        return self.get(ident)

    def history(self, session, before=None, limit=60):
        rows = self.db.execute(
            "SELECT id FROM chat_messages WHERE session_id=? AND ordinal<? ORDER BY ordinal DESC LIMIT ?",
            (session, before or 2**63 - 1, min(limit, 100)),
        ).fetchall()
        return [self.get(row[0]) for row in reversed(rows)]

    def cursor(self):
        return self.db.execute("SELECT coalesce(max(seq),0) FROM chat_events").fetchone()[0]

    def changes(self, session, after, limit=100):
        first = self.db.execute("SELECT min(seq) FROM chat_events").fetchone()[0]
        if after > self.cursor() or (first and after and after < first - 1):
            return None
        rows = self.db.execute(
            "SELECT seq,message_id,kind,data FROM chat_events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
            (session, after, limit),
        ).fetchall()
        return [
            {"seq": seq, "kind": kind, "data": self.get(mid) if mid else json.loads(data)}
            for seq, mid, kind, data in rows
        ]

    def recover(self, session):
        for (ident,) in self.db.execute(
            "SELECT id FROM chat_messages WHERE session_id=? AND status IN ('sending','streaming')",
            (session,),
        ).fetchall():
            m = self.get(ident)
            self.put(
                ident,
                session,
                **{k: m[k] for k in ("role", "source", "kind", "text", "turn_id", "attachments")},
                status="unknown",
            )
        with self.db:
            self.db.execute("UPDATE chat_requests SET state='unknown' WHERE state='sending'")
