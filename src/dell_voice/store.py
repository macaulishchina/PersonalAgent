import json
import sqlite3
import time


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS transcripts(id INTEGER PRIMARY KEY, time REAL, role TEXT, text TEXT);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, turn_id TEXT, prompt TEXT, status TEXT,
                result TEXT DEFAULT '', updated REAL);
        ''')

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def transcript(self, role, text):
        with self.db:
            self.db.execute("INSERT INTO transcripts(time,role,text) VALUES(?,?,?)", (time.time(), role, text))

    def create_task(self, ident, prompt):
        with self.db:
            self.db.execute("INSERT INTO tasks(id,prompt,status,updated) VALUES(?,?,?,?)", (ident, prompt, "dispatching", time.time()))

    def update_task(self, ident, status, turn_id=None, result=None):
        with self.db:
            self.db.execute("UPDATE tasks SET status=?,turn_id=coalesce(?,turn_id),result=coalesce(?,result),updated=? WHERE id=?",
                            (status, turn_id, result, time.time(), ident))

    def unfinished(self):
        return self.db.execute("SELECT id,turn_id,status FROM tasks WHERE status IN ('dispatching','running','unknown')").fetchall()

    def recent(self, limit=5):
        rows = self.db.execute("SELECT id,turn_id,prompt,status,result,updated FROM tasks ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(["id","turn_id","prompt","status","result","updated"], r)) for r in rows]

