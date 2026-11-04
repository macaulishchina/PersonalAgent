"""Durable, ordered conversation projection and resumable change notifications."""

import hashlib
import json
import time
from contextlib import nullcontext


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
            CREATE TABLE IF NOT EXISTS conversation_uploads (id TEXT PRIMARY KEY, session_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conversation_execution (request_id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conversation_queue (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL, source TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL, turn_id TEXT
            );
        """)
        if "tool_summary" not in {row[1] for row in self.db.execute("PRAGMA table_info(chat_messages)")}:
            self.db.execute("ALTER TABLE chat_messages ADD COLUMN tool_summary TEXT")
        if "task_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(chat_messages)")}:
            self.db.execute("ALTER TABLE chat_messages ADD COLUMN task_id TEXT")

        if "failure" not in {row[1] for row in self.db.execute("PRAGMA table_info(chat_messages)")}:
            self.db.execute("ALTER TABLE chat_messages ADD COLUMN failure TEXT")
        if "result_ref" not in {row[1] for row in self.db.execute("PRAGMA table_info(chat_messages)")}:
            self.db.execute("ALTER TABLE chat_messages ADD COLUMN result_ref TEXT")
        if "presentation" not in {row[1] for row in self.db.execute("PRAGMA table_info(chat_messages)")}:
            self.db.execute("ALTER TABLE chat_messages ADD COLUMN presentation TEXT")
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS conversation_retry_target "
            "ON conversation_queue(session_id,json_extract(payload,'$.retry_of')) "
            "WHERE json_extract(payload,'$.retry_of') IS NOT NULL"
        )

    @staticmethod
    def queue_revision(payload):
        return hashlib.sha256(payload.encode()).hexdigest()

    def execution(self, ident):
        row = self.db.execute(
            "SELECT data FROM conversation_execution WHERE request_id=?", (ident,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def execution_update(self, ident, **changes):
        data = {**(self.execution(ident) or {}), **changes}
        self.db.execute(
            "INSERT INTO conversation_execution VALUES(?,?) ON CONFLICT(request_id) DO UPDATE SET data=excluded.data",
            (ident, json.dumps(data)),
        )

    def queue(self, session):
        requests = []
        for ident, state, turn, payload in self.db.execute(
            "SELECT id,state,turn_id,payload FROM conversation_queue "
            "WHERE session_id=? AND state IN ('queued','sending','running') ORDER BY ordinal",
            (session,),
        ):
            data = json.loads(payload)
            requests.append(
                {
                    "id": ident,
                    "state": state,
                    "turn_id": turn,
                    "message_id": data["mid"],
                    "text": data["text"],
                    "attachments": data["media"],
                    "revision": self.queue_revision(payload),
                }
            )
        return requests

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
            "tool_summary",
            "task_id",
            "failure",
            "result_ref",
            "presentation",
        )
        result = dict(zip(keys, row))
        result["attachments"] = json.loads(result["attachments"])
        result["tool_summary"] = json.loads(result["tool_summary"]) if result["tool_summary"] else None
        result["failure"] = json.loads(result["failure"]) if result["failure"] else None
        result["result_ref"] = json.loads(result["result_ref"]) if result["result_ref"] else None
        result["presentation"] = json.loads(result["presentation"]) if result["presentation"] else {}
        return result

    def attachment_sessions(self, ident):
        return {
            row[0]
            for row in self.db.execute(
                "SELECT session_id FROM conversation_uploads WHERE id=? UNION "
                "SELECT DISTINCT session_id FROM chat_messages,json_each(chat_messages.attachments) "
                "WHERE json_extract(json_each.value,'$.id')=?",
                (ident, ident),
            )
        }

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
        tool_summary=None,
        task_id=None,
        failure=None,
        result_ref=None,
        presentation=None,
        commit=True,
        new_ordinal=False,
    ):
        old = self.get(ident)
        if old and old["session_id"] != session:
            raise ValueError("Message belongs to another session")
        attachments = attachments if attachments is not None else (old["attachments"] if old else [])
        tool_summary = tool_summary if tool_summary is not None else (old["tool_summary"] if old else None)
        task_id = task_id if task_id is not None else (old["task_id"] if old else None)
        failure = failure if failure is not None else (old["failure"] if old else None)
        result_ref = result_ref if result_ref is not None else (old["result_ref"] if old else None)
        presentation = presentation if presentation is not None else (old["presentation"] if old else {})
        values = (
            role,
            source,
            kind,
            text,
            status,
            turn_id,
            attachments,
            tool_summary,
            task_id,
            failure,
            result_ref,
            presentation,
        )
        if (
            old
            and not new_ordinal
            and values
            == tuple(
                old[k]
                for k in (
                    "role",
                    "source",
                    "kind",
                    "text",
                    "status",
                    "turn_id",
                    "attachments",
                    "tool_summary",
                    "task_id",
                    "failure",
                    "result_ref",
                    "presentation",
                )
            )
        ):
            return old
        with self.db if commit else nullcontext():
            seq = self.db.execute(
                "INSERT INTO chat_events(session_id,message_id,kind,data) VALUES(?,?,?,?)",
                (session, ident, "message", "{}"),
            ).lastrowid
            if seq % 500 == 0:
                self.db.execute("DELETE FROM chat_events WHERE seq < ?", (seq - 20000,))
            self.db.execute(
                """INSERT INTO chat_messages
                (id,session_id,ordinal,role,source,kind,text,status,turn_id,attachments,updated,tool_summary,task_id,failure,result_ref,presentation)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET ordinal=excluded.ordinal,role=excluded.role,source=excluded.source,
                kind=excluded.kind,text=excluded.text,status=excluded.status,
                turn_id=excluded.turn_id,attachments=excluded.attachments,updated=excluded.updated,
                tool_summary=excluded.tool_summary,task_id=excluded.task_id,failure=excluded.failure,result_ref=excluded.result_ref,
                presentation=excluded.presentation""",
                (
                    ident,
                    session,
                    old["ordinal"] if old and not new_ordinal else seq,
                    *values[:6],
                    json.dumps(attachments),
                    time.time() if updated_at is None else updated_at,
                    json.dumps(tool_summary) if tool_summary is not None else None,
                    task_id,
                    json.dumps(failure) if failure is not None else None,
                    json.dumps(result_ref) if result_ref is not None else None,
                    json.dumps(presentation),
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
        for (message,) in self.db.execute(
            "SELECT r.message_id FROM chat_requests r JOIN conversation_queue q ON q.id=r.id "
            "WHERE q.session_id=? AND q.state IN ('sending','running')",
            (session,),
        ).fetchall():
            m = self.get(message)
            self.put(
                message,
                session,
                **{k: m[k] for k in ("role", "source", "kind", "text", "turn_id", "attachments")},
                status="unknown",
            )
        for (ident,) in self.db.execute(
            "SELECT id FROM chat_messages WHERE session_id=? AND status IN ('queued','sending','streaming')",
            (session,),
        ).fetchall():
            m = self.get(ident)
            self.put(
                ident,
                session,
                **{k: m[k] for k in ("role", "source", "kind", "text", "turn_id", "attachments")},
                status="interrupted" if m["status"] == "queued" else "unknown",
            )
        with self.db:
            self.db.execute(
                "UPDATE chat_requests SET state='unknown' WHERE session_id=? AND "
                "(state='sending' OR id IN (SELECT id FROM conversation_queue WHERE state='running'))",
                (session,),
            )
            self.db.execute(
                "UPDATE chat_requests SET state='interrupted' WHERE state='queued' AND session_id=?",
                (session,),
            )
            self.db.execute(
                "UPDATE conversation_queue SET state=CASE WHEN state='queued' THEN 'interrupted' ELSE 'unknown' END WHERE session_id=? AND state IN ('queued','sending','running')",
                (session,),
            )
