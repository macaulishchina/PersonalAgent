"""Memory-owned durable jobs, claims and revision/tombstone metadata."""


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
            self.db.execute(
                "INSERT OR IGNORE INTO memory_jobs(start_id,end_id,epoch,status) VALUES(?,?,?,?)",
                (start, end, epoch, "pending"),
            )
            self.store.set("memory.cursor", end)
