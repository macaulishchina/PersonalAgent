"""Per-execution result ownership and durable delivery decisions; no time-based handoff."""

import asyncio
import json
import time
import weakref

from assistant_runtime.api.contracts import ConversationFeedbackRequest, NotificationRequest
from assistant_runtime.core.tasks.repository import TERMINAL


class TaskFeedback:
    def __init__(self, plugin):
        self.plugin = plugin
        self.db = plugin.repo.db
        self.locks = weakref.WeakValueDictionary()
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS job_result_claims (
          task_id TEXT PRIMARY KEY, context TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS job_result_feedback (
          run_id TEXT NOT NULL, round INTEGER NOT NULL, owner TEXT NOT NULL,
          context TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL,
          payload TEXT, receipt TEXT, updated REAL NOT NULL,
          PRIMARY KEY(run_id,round));
        """)

    async def capture(self, owner, caller):
        if not isinstance(owner, str) or owner not in {"background", "conversation"}:
            raise ValueError("feedback_owner must be background or conversation")
        if owner == "background":
            return None
        if caller:
            raise ValueError("Background callers cannot acquire foreground feedback ownership")
        context = await self.plugin.host.runtime.invoke(ConversationFeedbackRequest("capture"), timeout=1)
        if not context:
            raise ValueError("No active conversation can accept result ownership")
        return context

    def bind(self, task_id, context):
        if context:
            # Called in the definition creation transaction, before scheduling.
            self.db.execute(
                "INSERT OR IGNORE INTO job_result_claims VALUES(?,?)", (task_id, json.dumps(context))
            )

    def describe(self, run):
        number = run["data"].get("completion_round", 0)
        row = self.db.execute(
            "SELECT owner,context,version,state,receipt FROM job_result_feedback WHERE run_id=? AND round=?",
            (run["id"], number),
        ).fetchone()
        if row:
            return {
                "run_id": run["id"],
                "round": number,
                "owner": row[0],
                "context": json.loads(row[1]),
                "version": row[2],
                "state": row[3],
                "receipt": json.loads(row[4]) if row[4] else None,
            }
        context = None
        if number == 0:
            first = self.db.execute(
                "SELECT id FROM job_runs WHERE task_id=? ORDER BY created,id LIMIT 1", (run["task_id"],)
            ).fetchone()
            claim = self.db.execute(
                "SELECT context FROM job_result_claims WHERE task_id=?", (run["task_id"],)
            ).fetchone()
            if first and first[0] == run["id"] and claim:
                context = json.loads(claim[0])
        return {
            "run_id": run["id"],
            "round": number,
            "owner": "conversation" if context else "background",
            "context": context or {},
            "version": 0,
            "state": "delivered" if run["data"].get("completion_delivered") else "open",
            "receipt": None,
        }

    def save(self, info, *, payload=None):
        self.db.execute(
            "INSERT INTO job_result_feedback VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id,round) DO UPDATE SET owner=excluded.owner,context=excluded.context,"
            "version=excluded.version,state=excluded.state,payload=coalesce(excluded.payload,job_result_feedback.payload),"
            "receipt=excluded.receipt,updated=excluded.updated",
            (
                info["run_id"],
                info["round"],
                info["owner"],
                json.dumps(info["context"]),
                info["version"],
                info["state"],
                json.dumps(payload) if payload else None,
                json.dumps(info["receipt"]) if info["receipt"] else None,
                time.time(),
            ),
        )

    async def transfer(self, args, caller):
        # The server binds the conversation; callers cannot supply session/turn identities.
        if set(args) != {"run_id", "round", "version", "owner"}:
            raise ValueError("Feedback transfer requires run_id, round, version and owner")
        if any(type(args[k]) is not int or args[k] < 0 for k in ("round", "version")):
            raise ValueError("Invalid feedback round/version")
        context = await self.capture(args["owner"], caller)
        async with self.locks.setdefault(args["run_id"], asyncio.Lock()):
            run = self.plugin.repo.run(args["run_id"])
            info = self.describe(run)
            if info["round"] != args["round"] or info["version"] != args["version"]:
                raise ValueError("Feedback ownership changed; read get_task again")
            if info["state"] != "open":
                return {**info, "transferred": False}  # Committed output cannot be recalled/reissued.
            if not run["data"]["spec"].get("notify_completion", True):
                raise ValueError("Completion feedback is disabled for this execution")
            info.update(owner=args["owner"], context=context or {}, version=info["version"] + 1)
            with self.db:
                self.save(info)
        if run["state"] in TERMINAL:
            await self.deliver(run, self.plugin.result_payload(run))
            info = self.describe(self.plugin.repo.run(run["id"]))
        return {**info, "transferred": True}

    async def deliver(self, run, payload):
        async with self.locks.setdefault(run["id"], asyncio.Lock()):
            # Refresh after acquiring the lock: continuation may have changed the round.
            current = self.plugin.repo.run(run["id"])
            if current["revision"] != run["revision"]:
                return None
            info = self.describe(run)
            if info["state"] == "delivered":
                return info["receipt"] or {"route": "notification"}
            if info["state"] == "open":
                info.update(state="committed", version=info["version"] + 1)
                snapshot = {"notification": payload, "attachments": run["data"].get("attachments", [])[:4]}
                with self.db:
                    self.save(info, payload=snapshot)
            else:
                snapshot = json.loads(
                    self.db.execute(
                        "SELECT payload FROM job_result_feedback WHERE run_id=? AND round=?",
                        (run["id"], info["round"]),
                    ).fetchone()[0]
                )
            payload = snapshot["notification"]
            receipt = None
            if info["owner"] == "conversation":
                # A lost reply retries the SAME delivery identity. Never infer release from a timeout.
                receipt = await self.plugin.host.runtime.invoke(
                    ConversationFeedbackRequest(
                        "deliver",
                        info["context"],
                        {
                            "id": f"task-result:{run['id']}:{info['round']}",
                            "task_id": run["task_id"],
                            "run_id": run["id"],
                            "payload": payload,
                            "attachments": snapshot["attachments"],
                        },
                    )
                )
                if receipt["state"] in {"delivered", "submitted"}:
                    receipt = {**receipt, "route": "conversation"}
                else:
                    # Explicit release or uncertain speech hands off, never retries speech.
                    info.update(owner="background", context={}, version=info["version"] + 1)
                    if receipt["state"] == "unknown":
                        payload = {**payload, "voice": False}
                    snapshot["notification"] = payload
                    with self.db:
                        self.save(info, payload=snapshot)
                    receipt = None
            if receipt is None:
                notice = await self.plugin.host.runtime.invoke(
                    NotificationRequest("publish", payload, run["id"])
                )
                receipt = {"route": "notification", "notification_id": notice["id"]}
            info.update(state="delivered", receipt=receipt)
            with self.db:
                self.save(info)
            return receipt
