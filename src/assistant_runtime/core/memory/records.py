"""Memory-owned durable jobs, claims and revision/tombstone metadata."""

import json
from itertools import groupby


class MemoryRecords:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS memory_jobs(id INTEGER PRIMARY KEY,start_id INTEGER,end_id INTEGER UNIQUE,epoch INTEGER,status TEXT,attempts INTEGER DEFAULT 0,error TEXT);
            CREATE TABLE IF NOT EXISTS memory_claims(source_id INTEGER,evidence_hash TEXT,memory_id TEXT,PRIMARY KEY(source_id,evidence_hash));
            CREATE TABLE IF NOT EXISTS memory_items(id TEXT PRIMARY KEY,revision INTEGER,deleted INTEGER DEFAULT 0,source_end INTEGER,updated REAL);
        """)
        if store.get("memory.cursor") is None:
            store.set("memory.cursor", store.transcripts.last_id())
        if store.get("memory.chat_start") is None:
            # Explicitly start with new input, never silently backfill private history.
            store.set("memory.chat_start", store.transcripts.chat_position())
        with self.db:
            self.db.execute("UPDATE memory_jobs SET status='pending' WHERE status='running'")

    def pending_count(self):
        return self.db.execute(
            "SELECT count(*) FROM memory_jobs WHERE status IN ('pending','running')"
        ).fetchone()[0]

    def enqueue(self):
        start = self.store.get("memory.cursor", self.store.transcripts.last_id())
        end = self.store.transcripts.last_id()
        if end <= start:
            return
        epoch = self.store.get("memory.epoch", 0)
        with self.db:
            if any(s["role"] == "user" for s in self.store.transcripts.between(start, end)):
                self.db.execute(
                    "INSERT OR IGNORE INTO memory_jobs(start_id,end_id,epoch,status) VALUES(?,?,?,?)",
                    (start, end, epoch, "pending"),
                )
            self.store.set("memory.cursor", end)

    def collect_conversations(self, enabled, workspace):
        transcripts = self.store.transcripts
        if not enabled:
            self.store.set("memory.chat_start", transcripts.chat_position())
            return
        sources = transcripts.completed_inputs(self.store.get("memory.chat_start", 0))
        if not sources:
            return
        # Flush legacy voice evidence before appending attributed chat evidence. Both
        # paths share one transcript cursor, so neither can enqueue the other's input.
        self.enqueue()
        epoch = self.store.get("memory.epoch", 0)
        for _, group in groupby(sources, key=lambda s: (s["session_id"], s["request_id"])):
            batch, size = [], 0
            for source in group:
                length = len(source["text"])
                if length > 32000:
                    raise ValueError(f"Memory input exceeds 32000 characters: {source['message_id']}")
                if batch and size + length > 32000:
                    self._conversation_batch(batch, epoch, workspace)
                    batch, size = [], 0
                batch.append(source)
                size += length
            if batch:
                self._conversation_batch(batch, epoch, workspace)

    def _conversation_batch(self, sources, epoch, workspace):
        # Evidence, job and cursor commit together. A crash before this transaction
        # leaves conversation history discoverable; after it, message IDs deduplicate.
        with self.db:
            start = self.store.transcripts.last_id()
            for source in sources:
                provenance = {k: v for k, v in source.items() if k not in {"text", "time"}}
                provenance["workspace"] = str(workspace)
                self.db.execute(
                    "INSERT OR IGNORE INTO transcripts(time,role,text,message_id,provenance) VALUES(?,'user',?,?,?)",
                    (source["time"], source["text"], source["message_id"], json.dumps(provenance)),
                )
            end = self.store.transcripts.last_id()
            if end == start:
                return
            self.db.execute(
                "INSERT INTO memory_jobs(start_id,end_id,epoch,status) VALUES(?,?,?,'pending')",
                (start, end, epoch),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES('memory.cursor',?)", (json.dumps(end),)
            )
