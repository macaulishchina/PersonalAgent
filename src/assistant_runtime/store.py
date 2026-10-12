import json
import sqlite3
import time


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS transcripts(id INTEGER PRIMARY KEY, time REAL, role TEXT, text TEXT);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, turn_id TEXT, prompt TEXT, status TEXT,
                result TEXT DEFAULT '', updated REAL);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, provider TEXT, provider_id TEXT, created REAL, UNIQUE(provider,provider_id));
            CREATE TABLE IF NOT EXISTS task_sessions(task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_jobs(id INTEGER PRIMARY KEY,start_id INTEGER,end_id INTEGER UNIQUE,epoch INTEGER,status TEXT,attempts INTEGER DEFAULT 0,error TEXT);
            CREATE TABLE IF NOT EXISTS memory_claims(source_id INTEGER,evidence_hash TEXT,memory_id TEXT,PRIMARY KEY(source_id,evidence_hash));
            CREATE TABLE IF NOT EXISTS memory_items(id TEXT PRIMARY KEY,revision INTEGER,deleted INTEGER DEFAULT 0,source_end INTEGER,updated REAL);
        """)
        if self.get("memory.cursor") is None:
            self.set("memory.cursor", self.max_transcript())
        with self.db:
            self.db.execute("UPDATE memory_jobs SET status='pending' WHERE status='running'")

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False))
            )

    def transcript(self, role, text):
        with self.db:
            self.db.execute(
                "INSERT INTO transcripts(time,role,text) VALUES(?,?,?)", (time.time(), role, text)
            )

    def create_task(self, ident, prompt):
        with self.db:
            self.db.execute(
                "INSERT INTO tasks(id,prompt,status,updated) VALUES(?,?,?,?)",
                (ident, prompt, "dispatching", time.time()),
            )

        session = self.get("session_id")
        if session:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO task_sessions VALUES(?,?)", (ident, session))

    def update_task(self, ident, status, turn_id=None, result=None):
        with self.db:
            self.db.execute(
                "UPDATE tasks SET status=?,turn_id=coalesce(?,turn_id),result=coalesce(?,result),updated=? WHERE id=?",
                (status, turn_id, result, time.time(), ident),
            )

    def unfinished(self):
        return self.db.execute(
            "SELECT id,turn_id,status FROM tasks WHERE status IN ('dispatching','running','unknown')"
        ).fetchall()

    def recent(self, limit=5):
        rows = self.db.execute(
            "SELECT id,turn_id,prompt,status,result,updated FROM tasks ORDER BY updated DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(zip(["id", "turn_id", "prompt", "status", "result", "updated"], r)) for r in rows]

    def logical_session(self, provider, provider_id):
        import uuid

        row = self.db.execute(
            "SELECT id FROM sessions WHERE provider=? AND provider_id=?", (provider, provider_id)
        ).fetchone()
        if row:
            return row[0]
        ident = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?)", (ident, provider, provider_id, time.time())
            )
        return ident

    def max_transcript(self):
        return self.db.execute("SELECT coalesce(max(id),0) FROM transcripts").fetchone()[0]

    def enqueue_memory(self):
        start = self.get("memory.cursor", self.max_transcript())
        end = self.max_transcript()
        if end <= start:
            return
        epoch = self.get("memory.epoch", 0)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO memory_jobs(start_id,end_id,epoch,status) VALUES(?,?,?,?)",
                (start, end, epoch, "pending"),
            )
            self.set("memory.cursor", end)

    def memory_sources(self, start, end):
        rows = self.db.execute(
            "SELECT id,time,role,text FROM transcripts WHERE id>? AND id<=? ORDER BY id", (start, end)
        ).fetchall()
        return [dict(zip(["id", "time", "role", "text"], row)) for row in rows]
