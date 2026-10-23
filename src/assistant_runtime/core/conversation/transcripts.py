"""Conversation-owned source transcript records for memory evidence."""

import time


class Transcripts:
    def __init__(self, db):
        self.db = db
        db.execute(
            "CREATE TABLE IF NOT EXISTS transcripts(id INTEGER PRIMARY KEY, time REAL, role TEXT, text TEXT)"
        )

    def append(self, role, text):
        with self.db:
            self.db.execute(
                "INSERT INTO transcripts(time,role,text) VALUES(?,?,?)", (time.time(), role, text)
            )

    def last_id(self):
        return self.db.execute("SELECT coalesce(max(id),0) FROM transcripts").fetchone()[0]

    def between(self, start, end):
        rows = self.db.execute(
            "SELECT id,time,role,text FROM transcripts WHERE id>? AND id<=? ORDER BY id", (start, end)
        ).fetchall()
        return [dict(zip(["id", "time", "role", "text"], row)) for row in rows]
