"""Shared SQLite connection and settings; schema/record methods belong to their owners."""

import json
import sqlite3
from functools import cached_property


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    @cached_property
    def transcripts(self):
        from assistant_runtime.core.conversation.transcripts import Transcripts

        return Transcripts(self.db)

    @cached_property
    def executions(self):
        from assistant_runtime.core.pi.records import ExecutionRecords

        return ExecutionRecords(self)

    @cached_property
    def memory(self):
        from assistant_runtime.core.memory.records import MemoryRecords

        return MemoryRecords(self)

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False))
            )
