"""Bounded private receipts and durable attempt budgets on one owned IO thread."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import sqlite3
import time


class Receipts:
    def __init__(self, directory, settings):
        self.directory, self.settings = directory, settings
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shadow-receipts")
        self.db = None
        self.pending = 0
        self.closing = False

    async def call(self, action, *args):
        if action != "close" and (self.closing or self.pending >= 16):
            raise RuntimeError("Shadow receipt storage is busy or closing")
        self.pending += 1
        future = asyncio.get_running_loop().run_in_executor(self.pool, self.operation, action, *args)

        def completed(work):
            self.pending -= 1
            if not work.cancelled():
                work.exception()  # A timed-out caller may no longer consume an IO error.

        future.add_done_callback(completed)
        # Caller deadlines remain bounded. IO continues on the owned serial
        # executor; close is queued after it and drains before shutting down.
        return await asyncio.shield(future)

    async def close(self):
        self.closing = True
        await self.call("close")
        await asyncio.to_thread(self.pool.shutdown, wait=True, cancel_futures=True)

    def operation(self, action, *args):
        return getattr(self, "_" + action)(*args)

    def _open(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        path = self.directory / "shadow.sqlite3"
        self.db = sqlite3.connect(path, timeout=1)
        path.chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA wal_autocheckpoint=128")
        self.db.execute("PRAGMA max_page_count=32768")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS receipts (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS attempts (day TEXT PRIMARY KEY, count INTEGER NOT NULL);
        """)
        with self.db:
            self.db.execute("UPDATE receipts SET state='unknown' WHERE state='dispatching'")

    def _close(self):
        if self.db:
            self.db.close()
            self.db = None

    @staticmethod
    def day():
        return datetime.now(timezone.utc).date().isoformat()

    def _claim(self, job, skip=None):
        day = self.day()
        data = {k: v for k, v in job.items() if k not in {"input", "question", "deadline"}}
        data["created_at"] = time.time()
        with self.db:
            if self.db.execute("SELECT 1 FROM receipts WHERE id=?", (job["id"],)).fetchone():
                return False
            count = self.db.execute("SELECT count FROM attempts WHERE day=?", (day,)).fetchone()
            if skip is None and (count[0] if count else 0) >= self.settings.daily_limit:
                skip = "budget_skipped"
            state = skip or "dispatching"
            self.db.execute(
                "DELETE FROM receipts WHERE seq NOT IN (SELECT seq FROM receipts ORDER BY seq DESC LIMIT ?)",
                (self.settings.retention - 1,),
            )
            self.db.execute(
                "INSERT INTO receipts(id,kind,state,data) VALUES(?,?,?,?)",
                (job["id"], job["kind"], state, json.dumps(data, ensure_ascii=False)),
            )
            if skip is None:
                self.db.execute(
                    "INSERT INTO attempts VALUES(?,1) ON CONFLICT(day) DO UPDATE SET count=count+1", (day,)
                )
            self.db.execute("DELETE FROM attempts WHERE day<?", (day,))
        return skip is None

    def _finish(self, ident, state, result):
        row = self._get(ident)
        data = {k: v for k, v in row.items() if k not in {"seq", "state"}}
        with self.db:
            self.db.execute(
                "UPDATE receipts SET state=?,data=? WHERE id=? AND state='dispatching'",
                (state, json.dumps({**data, **result}, ensure_ascii=False), ident),
            )

    def _get(self, ident):
        row = self.db.execute("SELECT seq,state,data FROM receipts WHERE id=?", (ident,)).fetchone()
        if not row:
            raise ValueError("Shadow receipt not found")
        return {"seq": row[0], "state": row[1], **json.loads(row[2])}

    def _find(self, ident):
        try:
            return self._get(ident)
        except ValueError:
            return None

    def _list(self, limit, before):
        rows = self.db.execute(
            "SELECT seq,state,data FROM receipts WHERE seq<? ORDER BY seq DESC LIMIT ?",
            (before if before is not None else 2**63 - 1, limit + 1),
        ).fetchall()
        return {
            "items": [{"seq": r[0], "state": r[1], **json.loads(r[2])} for r in rows[:limit]],
            "before": rows[limit - 1][0] if len(rows) > limit else None,
        }

    def _label(self, ident, expected):
        row = self._get(ident)
        from .questions import question

        if expected not in row.get("choices", {}) and expected not in (
            question(row["kind"])["criteria"] if row["kind"] != "selection" else {}
        ):
            raise ValueError("Label must be one of this question's choices")
        data = {k: v for k, v in row.items() if k not in {"seq", "state"}}
        with self.db:
            self.db.execute(
                "UPDATE receipts SET data=? WHERE id=?",
                (json.dumps({**data, "expected": expected, "labelled_at": time.time()}), ident),
            )
        return self._get(ident)

    def _status(self):
        rows = self.db.execute("SELECT kind,state,data FROM receipts ORDER BY seq DESC").fetchall()
        groups = {}
        for kind, state, raw in rows:
            data = json.loads(raw)
            group = groups.setdefault(
                data["source"] + ":" + kind,
                {
                    "kind": kind,
                    "source": data["source"],
                    "states": {},
                    "latencies": [],
                    "labelled": 0,
                    "correct": 0,
                    "baseline_compared": 0,
                    "baseline_agreed": 0,
                    "abstained": 0,
                },
            )
            group["states"][state] = group["states"].get(state, 0) + 1
            if state != "completed":
                continue
            group["latencies"].append(data["latency_ms"])
            choice = data["answer"]["choice"]
            group["abstained"] += choice == "uncertain"
            if data.get("expected") is not None:
                group["labelled"] += 1
                group["correct"] += choice == data["expected"]
            if data.get("baseline") is not None:
                group["baseline_compared"] += 1
                group["baseline_agreed"] += choice == data["baseline"]
        for group in groups.values():
            latencies = sorted(group.pop("latencies"))
            group["latency_ms"] = {
                "p50": latencies[math.ceil(len(latencies) * 0.5) - 1] if latencies else None,
                "p95": latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else None,
            }
            group["accuracy"] = group["correct"] / group["labelled"] if group["labelled"] else None
        count = self.db.execute("SELECT count FROM attempts WHERE day=?", (self.day(),)).fetchone()
        return {
            "scope": "retained_receipts",
            "records": len(rows),
            "groups": groups,
            "attempts_today": count[0] if count else 0,
            "budget_day": self.day(),
        }
