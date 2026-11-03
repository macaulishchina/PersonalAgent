"""Conversation-owned source transcript records for memory evidence."""

import json
import time


class Transcripts:
    def __init__(self, db):
        self.db = db
        db.execute(
            "CREATE TABLE IF NOT EXISTS transcripts(id INTEGER PRIMARY KEY, time REAL, role TEXT, text TEXT)"
        )
        columns = {r[1] for r in db.execute("PRAGMA table_info(transcripts)")}
        with db:
            if "message_id" not in columns:
                db.execute("ALTER TABLE transcripts ADD COLUMN message_id TEXT")
            if "provenance" not in columns:
                db.execute("ALTER TABLE transcripts ADD COLUMN provenance TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS transcript_message ON transcripts(message_id)")

    def chat_position(self):
        if not self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_messages'"
        ).fetchone():
            return 0
        return self.db.execute("SELECT coalesce(max(ordinal),0) FROM chat_messages").fetchone()[0]

    def completed_inputs(self, after):
        """Bounded, unconsumed user evidence; unfinished/uncertain turns are never inferred."""
        if not self.chat_position():
            return []
        private = ""
        if self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_policies'"
        ).fetchone():
            private = " AND NOT EXISTS(SELECT 1 FROM conversation_policies p WHERE p.session_id=m.session_id AND json_extract(p.data,'$.capture_memory')=0) "
        rows = self.db.execute(
            "SELECT m.id,m.session_id,m.turn_id,m.source,m.text,m.updated,q.id,q.source "
            "FROM chat_messages m JOIN conversation_queue q "
            "ON q.session_id=m.session_id AND q.turn_id=m.turn_id "
            "WHERE q.state='completed' AND m.role='user' AND m.status='complete' "
            "AND m.ordinal>? AND trim(m.text)<>'' "
            "AND NOT EXISTS(SELECT 1 FROM transcripts t WHERE t.message_id=m.id) "
            + private
            + "ORDER BY q.ordinal,m.ordinal LIMIT 64",
            (after,),
        ).fetchall()
        keys = (
            "message_id",
            "session_id",
            "turn_id",
            "entrance",
            "text",
            "time",
            "request_id",
            "request_entrance",
        )
        return [dict(zip(keys, row)) for row in rows]

    def append(self, role, text):
        with self.db:
            self.db.execute(
                "INSERT INTO transcripts(time,role,text) VALUES(?,?,?)", (time.time(), role, text)
            )

    def last_id(self):
        return self.db.execute("SELECT coalesce(max(id),0) FROM transcripts").fetchone()[0]

    def between(self, start, end):
        rows = self.db.execute(
            "SELECT id,time,role,text,provenance FROM transcripts WHERE id>? AND id<=? ORDER BY id",
            (start, end),
        ).fetchall()
        return [
            {**dict(zip(["id", "time", "role", "text"], row[:4])), **json.loads(row[4] or "{}")}
            for row in rows
        ]
