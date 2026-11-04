"""Opt-in immutable task backgrounds; task ownership, not a second executor."""

from contextvars import ContextVar
import json
import uuid

from .repository import ACTIVE
from .tool_contracts import CONTEXT
from assistant_runtime.mcp.contracts import validate
from assistant_runtime.core.workfiles.inputs import caller

pending_context = ContextVar("pending_task_context", default=None)


class TaskContexts:
    def __init__(self, plugin):
        self.plugin, self.host, self.db = plugin, plugin.host, plugin.repo.db
        self.results = self.host.workfiles.results
        self.preparing = set()
        self.results.protection_checks["task_context"] = self.protected
        self.results.protection_checks["context_prepare"] = lambda ident: ident in self.preparing

    def protected(self, ident):
        row = self.db.execute("SELECT state FROM job_definitions WHERE id=?", (ident,)).fetchone()
        if row and row[0] not in {"archived", "terminated"}:
            return True
        return (
            self.db.execute(
                "SELECT 1 FROM job_runs WHERE task_id=? AND state IN (" + ",".join("?" for _ in ACTIVE) + ")",
                (ident, *ACTIVE),
            ).fetchone()
            is not None
        )

    def capture(self, spec):
        if "context" not in spec:
            return None
        validate(spec["context"], CONTEXT, "spec.context")
        if spec.get("execution", {}).get("kind", "agent") != "agent":
            raise ValueError("Task context requires agent execution")
        conversations = getattr(self.host, "conversations", None)
        if conversations is None:
            raise ValueError("snapshot_capability_unavailable")
        owner = caller()  # Capture before the create operation establishes a new usage scope.
        context = spec["context"]
        row, _ = conversations.snapshots.resolve(context["snapshot_ref"], owner=owner)
        dependencies = json.loads(row["dependencies"])
        for ident in dependencies:
            self.results.content(self.results.row(ident, owner="operator"))
        additional = None
        if context.get("additional_input"):
            additional = self.host.workfiles.inputs.get(context["additional_input"], owner=owner)
            self.host.workfiles.inputs.text(additional)
        checkpoint = json.loads(row["checkpoint"])
        return {
            "snapshot_ref": row["id"],
            "source_session": row["session_id"],
            "checkpoint": checkpoint["id"],
            "result_id": row["result_id"],
            "dependencies": dependencies,
            "additional_input": additional,
            "owner": owner,
        }

    def hold_preparing(self, captured):
        ident = uuid.uuid4().hex
        self.preparing.add(ident)
        try:
            for reference in [captured["result_id"], *captured["dependencies"]]:
                self.results.hold(reference, "context_prepare:" + ident)
        except BaseException:
            self.release_preparing(ident)
            raise
        return ident

    def release_preparing(self, ident):
        self.results.release("context_prepare:" + ident)
        self.preparing.discard(ident)

    def bind(self, task, captured):
        if captured is None:
            return
        self.db.execute("INSERT INTO task_contexts VALUES(?,?)", (task, json.dumps(captured)))
        for reference in [captured["result_id"], *captured["dependencies"]]:
            self.results.hold(reference, "task_context:" + task, commit=False)

    async def seed(self, run, thread):
        row = self.db.execute("SELECT data FROM task_contexts WHERE task_id=?", (run["task_id"],)).fetchone()
        if not row:
            raise ValueError("snapshot_task_binding_unavailable")
        captured = json.loads(row[0])
        bundle = json.loads(self.results.content(self.results.row(captured["result_id"], owner="operator")))
        additional = (
            self.host.workfiles.inputs.text(captured["additional_input"])
            if captured["additional_input"]
            else ""
        )
        for reference in captured["dependencies"]:
            self.results.content(self.results.row(reference, owner="operator"))
            self.results.grant(reference, caller())
        seeded = await self.host.backend.client.request(
            "session.seed",
            {
                "session_id": thread,
                "snapshot_ref": captured["snapshot_ref"],
                "content": bundle,
                "additional": additional,
            },
        )
        self.plugin.provider_update(
            run["id"],
            None,
            context={
                "snapshot_ref": seeded["snapshot_ref"],
                "checkpoint": captured["checkpoint"],
                "additional_input": captured["additional_input"]["id"]
                if captured["additional_input"]
                else None,
                "additional_sha256": captured["additional_input"]["sha256"]
                if captured["additional_input"]
                else None,
                "seeded": True,
            },
        )
