"""Immutable public conversation checkpoints and creation receipts."""

import asyncio
import json
import sqlite3
import time
import uuid

from assistant_runtime.core.workfiles.inputs import caller
from assistant_runtime.api.results import ResultAccessError


class Snapshots:
    def __init__(self, conversations):
        self.conversations, self.host = conversations, conversations.host
        self.db = conversations.sessions.db
        self.results = self.host.workfiles.results
        self.tasks = {}
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS conversation_snapshots(
                request_id TEXT PRIMARY KEY, id TEXT UNIQUE NOT NULL, session_id TEXT NOT NULL,
                owner TEXT NOT NULL, created REAL NOT NULL, state TEXT NOT NULL,
                checkpoint TEXT NOT NULL, engine TEXT NOT NULL, result_id TEXT,
                dependencies TEXT NOT NULL DEFAULT '[]', compacted INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
        """)
        self.results.protection_checks["snapshot"] = self.protected

    def protected(self, ident):
        row = self.db.execute(
            "SELECT state,result_id FROM conversation_snapshots WHERE id=?", (ident,)
        ).fetchone()
        if not row:
            return False
        if row[0] == "preparing":
            return True
        if not row[1]:
            return False
        result = self.results.row(row[1], owner="operator")
        return result["state"] == "ready" and (
            not self.results.days
            or result["accessed"] + self.results.days * 86400 > time.time()
            or self.results.db.execute("SELECT 1 FROM result_holds WHERE id=?", (row[1],)).fetchone()
            is not None
        )

    def row(self, *, ident=None, request=None):
        query, value = ("id", ident) if ident else ("request_id", request)
        row = self.db.execute(f"SELECT * FROM conversation_snapshots WHERE {query}=?", (value,)).fetchone()
        if not row:
            return None
        return dict(
            zip(
                (
                    "request_id",
                    "id",
                    "session_id",
                    "owner",
                    "created",
                    "state",
                    "checkpoint",
                    "engine",
                    "result_id",
                    "dependencies",
                    "compacted",
                    "error",
                ),
                row,
            )
        )

    @staticmethod
    def authorize(row, owner):
        if owner not in {"operator", row["owner"]}:
            raise ValueError("snapshot_forbidden")

    def describe(self, row):
        checkpoint = json.loads(row["checkpoint"])
        state = row["state"]
        retention = {"retention_days": self.results.days, "expires_at": None}
        if row["result_id"]:
            result = self.results.row(row["result_id"], owner="operator")
            state = result["state"] if state == "ready" else state
            retention = {k: self.results.view(result)[k] for k in ("retention_days", "expires_at")}
        return {
            "found": True,
            "request_id": row["request_id"],
            "snapshot_ref": row["id"],
            "source_session": row["session_id"],
            "checkpoint": checkpoint["id"],
            "created_at": row["created"],
            "state": state,
            "compacted": bool(row["compacted"]),
            "coverage_known": checkpoint["coverage_known"],
            "input_count": len(checkpoint["inputs"]),
            **retention,
            **({"error": row["error"]} if row["error"] else {}),
        }

    def resolve(self, ident, *, owner):
        row = self.row(ident=ident)
        if not row:
            raise ValueError("snapshot_not_found")
        self.authorize(row, owner)
        if row["state"] != "ready":
            raise ValueError("snapshot_" + row["state"])
        payload = json.loads(self.results.content(self.results.row(row["result_id"], owner="operator")))
        # Read validation never silently recaptures changed/missing history.
        return row, payload

    async def request(self, action, args):
        try:
            return await self._request(action, args)
        except sqlite3.Error:
            raise ValueError("snapshot_storage_unavailable") from None

    async def _request(self, action, args):
        session = args["session_id"]
        owner = caller()
        if action == "snapshot_inputs":
            row = self.row(ident=args.get("snapshot_ref"))
            if not row or row["session_id"] != session:
                raise ValueError("snapshot_not_found")
            self.authorize(row, owner)
            checkpoint = json.loads(row["checkpoint"])
            limit = args.get("limit", 100)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError("snapshot_invalid_page")
            cursor = args.get("cursor")
            offset = 0
            if cursor is not None:
                try:
                    prefix, number = cursor.split(":")
                    offset = int(number)
                    if prefix != row["id"] or offset < 0 or offset > len(checkpoint["inputs"]):
                        raise ValueError()
                except (AttributeError, TypeError, ValueError):
                    raise ValueError("snapshot_invalid_cursor") from None
            inputs = checkpoint["inputs"][offset : offset + limit]
            end = offset + len(inputs)
            return {
                "snapshot_ref": row["id"],
                "checkpoint": checkpoint["id"],
                "coverage_known": checkpoint["coverage_known"],
                "request_ids": inputs,
                "next_cursor": f"{row['id']}:{end}" if end < len(checkpoint["inputs"]) else None,
            }
        request = str(uuid.UUID(args["request_id"]))
        row = self.row(request=request)
        if row:
            if row["session_id"] != session:
                raise ValueError("snapshot_request_conflict")
            self.authorize(row, owner)
        if action == "snapshot_receipt":
            return (
                self.describe(row) if row else {"found": False, "state": "not_found", "request_id": request}
            )
        if row is None:
            chat = await self.conversations.get(session)
            engine = chat.backend.execution_session_id
            if engine is None:
                raise ValueError("snapshot_source_unavailable")
            checkpoint = await self.host.backend.client.request("session.checkpoint", {"session_id": engine})
            ident = uuid.uuid4().hex
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO conversation_snapshots(request_id,id,session_id,owner,created,state,checkpoint,engine) VALUES(?,?,?,?,?,'preparing',?,?)",
                    (request, ident, session, owner, time.time(), json.dumps(checkpoint), engine),
                )
            row = self.row(request=request)
            if row["session_id"] != session:
                raise ValueError("snapshot_request_conflict")
            self.authorize(row, owner)
        if row["state"] == "preparing":
            if request not in self.tasks:
                task = self.conversations.context.spawn(self.prepare(row), "snapshot:" + row["id"])
                self.tasks[request] = task
                task.add_done_callback(lambda _: self.tasks.pop(request, None))
            await asyncio.shield(self.tasks[request])
        return self.describe(self.row(request=request))

    async def prepare(self, row):
        reason = "snapshot:" + row["id"]
        try:
            exported = await self.host.backend.client.request(
                "session.snapshot", {"session_id": row["engine"], "checkpoint": json.loads(row["checkpoint"])}
            )
            payload = exported["content"]
            dependencies = self.host.backend.executions.capture_context_results(
                row["engine"], exported["references"], reason
            )
            content = await self.results.put(
                "snapshot:" + row["id"], "context", row["owner"], payload, hold=False
            )
            with self.db:
                self.db.execute(
                    "UPDATE conversation_snapshots SET state='ready',result_id=?,dependencies=?,compacted=? WHERE id=?",
                    (
                        content["id"],
                        json.dumps(sorted(set(dependencies))),
                        int(exported["compacted"]),
                        row["id"],
                    ),
                )
        except asyncio.CancelledError:
            raise  # Restart resumes the same persisted boundary, never a newer one.
        except (OSError, ValueError, RuntimeError, ResultAccessError) as error:
            code = error.code if isinstance(error, ResultAccessError) else "snapshot_preparation_unavailable"
            with self.db:
                self.db.execute(
                    "UPDATE conversation_snapshots SET state='unavailable',error=? WHERE id=?",
                    (code, row["id"]),
                )
            self.results.release(reason)
