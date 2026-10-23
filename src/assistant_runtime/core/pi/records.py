"""Pi-owned foreground execution receipts and logical session identity."""

import time


class ExecutionRecords:
    def __init__(self, store):
        self.db, self.store = store.db, store
        self.outcome = None
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, turn_id TEXT, prompt TEXT, status TEXT, result TEXT DEFAULT '', updated REAL);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, provider TEXT, provider_id TEXT, created REAL, UNIQUE(provider,provider_id));
            CREATE TABLE IF NOT EXISTS task_sessions(task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL);
        """)

        if "execution_id" not in {r[1] for r in self.db.execute("PRAGMA table_info(tasks)")}:
            self.db.execute("ALTER TABLE tasks ADD COLUMN execution_id TEXT")

    def bind(self, ident, execution_id):
        with self.db:
            self.db.execute("UPDATE tasks SET execution_id=? WHERE id=?", (execution_id, ident))

    def create_task(self, ident, prompt):
        with self.db:
            self.db.execute(
                "INSERT INTO tasks(id,prompt,status,updated) VALUES(?,?,?,?)",
                (ident, prompt, "dispatching", time.time()),
            )

        session = self.store.get("session_id")
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
            "SELECT id,turn_id,prompt,status,result,updated,execution_id FROM tasks ORDER BY updated DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = [
            dict(zip(["id", "turn_id", "prompt", "status", "result", "updated", "execution_id"], r))
            for r in rows
        ]
        for item in result:
            if self.outcome and item["execution_id"]:
                execution = self.outcome(item["execution_id"])
                item["status"] = {"cancelled": "interrupted"}.get(execution["state"], execution["state"])
                item["result"] = execution["data"].get("result", item["result"])
        return result

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
