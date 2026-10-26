"""Append-only observations; outcome uncertainty is never rewritten as failure."""

import json
import time
import uuid


class OperationRecords:
    def __init__(self, records):
        self.records, self.db = records, records.db
        self.host_start_id = str(uuid.uuid4())
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS execution_operations(
                id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, kind TEXT NOT NULL,
                native_id TEXT NOT NULL, parent_id TEXT, state TEXT NOT NULL,
                data TEXT NOT NULL, created REAL NOT NULL,
                UNIQUE(execution_id,kind,native_id)
            );
            CREATE TABLE IF NOT EXISTS execution_observations(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id TEXT NOT NULL, operation_id TEXT,
                host_start_id TEXT NOT NULL, observed REAL NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS execution_observation_cursor
                ON execution_observations(execution_id,sequence);
        """)

    def observe(self, execution, operation, data):
        encoded = json.dumps(data, ensure_ascii=False)
        if len(encoded.encode()) > 8192:
            raise ValueError("Execution observation exceeds 8 KiB")
        cursor = self.db.execute(
            "INSERT INTO execution_observations(execution_id,operation_id,host_start_id,observed,data) "
            "VALUES(?,?,?,?,?)",
            (execution, operation, self.host_start_id, time.time(), encoded),
        )
        return cursor.lastrowid

    def begin(self, execution, kind, native_id, *, parent=None, **details):
        if kind not in {"model", "tool"} or not isinstance(native_id, str) or not 1 <= len(native_id) <= 256:
            raise ValueError("Invalid execution operation")
        ident = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([execution, kind, native_id])))
        with self.db:
            existing = self.db.execute("SELECT id FROM execution_operations WHERE id=?", (ident,)).fetchone()
            if existing:
                return ident
            row = self.records.get(execution)
            terminal = row["state"] in {"completed", "failed", "cancelled", "interrupted", "unknown"}
            if terminal and kind == "model":
                raise RuntimeError("Execution no longer admits operations")
            if kind == "tool" and details.get("model_request_id"):
                candidate = self.db.execute(
                    "SELECT id FROM execution_operations WHERE execution_id=? AND kind='model' AND native_id=?",
                    (execution, details["model_request_id"]),
                ).fetchone()
                parent = candidate[0] if candidate else None
            self.db.execute(
                "INSERT INTO execution_operations VALUES(?,?,?,?,?,'observed',?,?)",
                (ident, execution, kind, native_id, parent, json.dumps(details), time.time()),
            )
            if terminal:
                self.db.execute("UPDATE execution_operations SET state='unknown' WHERE id=?", (ident,))
            self.observe(
                execution,
                ident,
                {"state": "unknown" if terminal else "observed", "late": terminal, "kind": kind, **details},
            )
            self.records.event(execution, "operation.observed", {"operation_id": ident, "kind": kind})
        return ident

    def settle(self, execution, kind, native_id, state):
        if state not in {"completed", "failed", "not_sent", "unknown"}:
            raise ValueError("Invalid operation outcome")
        row = self.db.execute(
            "SELECT id,state FROM execution_operations WHERE execution_id=? AND kind=? AND native_id=?",
            (execution, kind, native_id),
        ).fetchone()
        if row is None:
            return
        with self.db:
            self.observe(execution, row[0], {"state": state, "late": row[1] != "observed"})
            if row[1] == "observed":
                self.db.execute("UPDATE execution_operations SET state=? WHERE id=?", (state, row[0]))

    def finish(self, execution):
        for (ident,) in self.db.execute(
            "SELECT id FROM execution_operations WHERE execution_id=? AND state='observed'", (execution,)
        ).fetchall():
            self.observe(
                execution, ident, {"state": "unknown", "reason": "execution_ended_without_settlement"}
            )
            self.db.execute("UPDATE execution_operations SET state='unknown' WHERE id=?", (ident,))

    def history(self, execution, cursor=0, limit=100):
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid observation cursor or limit")
        rows = self.db.execute(
            "SELECT sequence,operation_id,host_start_id,observed,data FROM execution_observations "
            "WHERE execution_id=? AND sequence>? ORDER BY sequence LIMIT ?",
            (execution, cursor, limit + 1),
        ).fetchall()
        return {
            "items": [
                dict(sequence=r[0], operation_id=r[1], host_start_id=r[2], observed=r[3], **json.loads(r[4]))
                for r in rows[:limit]
            ],
            "cursor": rows[min(len(rows), limit) - 1][0] if rows else cursor,
            "has_more": len(rows) > limit,
        }

    def reconcile(self, execution):
        row = self.records.get(execution)
        # Legacy full-permission tools can escape observation through external MCP or
        # plugin I/O. A terminal turn or an empty pending-tool list is not a write fence.
        latest = self.db.execute(
            "SELECT data FROM execution_observations WHERE execution_id=? AND operation_id IS NULL ORDER BY sequence DESC",
            (execution,),
        )
        process = next(
            (data for (raw,) in latest if (data := json.loads(raw)).get("kind") == "local_process"), None
        )
        models = dict(
            self.db.execute(
                "SELECT state,count(*) FROM execution_operations WHERE execution_id=? AND kind='model' GROUP BY state",
                (execution,),
            )
        )
        summary = {
            "execution_state": row["state"],
            "last_local_observation": process,
            "recorded_model_operations": models,
            "observation_coverage": "pi_native_only",
            "local_processes": "unknown",
            "local_writes": "unfenced",
            "remote_model": "unknown",
            "external_effects": "unknown",
            "safe_to_retry": False,
            "reason": "controlled_operation_evidence_unavailable",
        }
        with self.db:
            sequence = self.observe(execution, None, {"kind": "reconciliation", **summary})
        return {"id": execution, "observation_id": sequence, **summary}
