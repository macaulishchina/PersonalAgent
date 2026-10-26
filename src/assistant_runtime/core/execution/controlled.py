"""One round at a time over retained Pi contexts, with resource fences."""

import asyncio
import hashlib
import json

from assistant_runtime.api.ai_usage import UsageScope, current_usage
from assistant_runtime.api.plugins import CallIdentity, current_call
from .authority import uid
from .records import TERMINAL

TOOLS = ["pa_read", "pa_write", "pa_check", "request_user_input"]


class ControlledExecution:
    def __init__(self, service):
        self.service = service
        self.interrupting = set()

    def work_id(self, row):
        key = [row["controller"], row["owner"], row["data"]["binding"]["usage_scope_id"]]
        return "controlled-" + hashlib.sha256(json.dumps(key).encode()).hexdigest()

    async def submit(self, principal, args, *, continuing=False):
        required = {"context_id", "request_id", "revision", "text"} | (
            {"previous_execution_id"} if continuing else set()
        )
        if set(args) - required - {"inputs", "limits"} or not required <= set(args):
            raise ValueError(
                "Controlled round requires context_id, request_id, revision and text; continuation also requires previous_execution_id"
            )
        ident = uid(args["request_id"])
        context = self.service.contexts.owned(principal, "context", args["context_id"])
        intent = json.dumps(
            {"continue": continuing, **args, "request_id": ident, "context_id": context["id"]}, sort_keys=True
        )
        prior = self.service.records.find(ident)
        if prior:
            if (
                prior["data"].get("context_id") != context["id"]
                or prior["data"].get("control_intent") != intent
            ):
                raise ValueError("Execution request ID reused with another controlled intent")
            return self.service.view(prior)
        self.service.broker.files.available()
        workspace = context["data"]["binding"]["workspace_id"]
        async with self.service.broker.lock(workspace):
            context = self.service.contexts.owned(principal, "context", context["id"])
            prior = self.service.records.find(ident)
            if prior:
                if (
                    prior["data"].get("context_id") != context["id"]
                    or prior["data"].get("control_intent") != intent
                ):
                    raise ValueError("Execution request ID reused with another controlled intent")
                return self.service.view(prior)
            previous = context["data"]["last_execution"]
            if continuing:
                if not previous or args["previous_execution_id"] != previous:
                    raise ValueError("Continuation must name the last settled execution")
                if self.service.records.get(previous)["state"] not in TERMINAL - {"unknown"}:
                    raise PermissionError("Uncertain executions cannot continue")
            elif previous is not None:
                raise ValueError("An existing context requires explicit continuation")
            workspace_row = self.service.contexts.verify(context)
            from assistant_runtime.core.ai_usage.controlled import validate

            limits = args.get("limits", {})
            validate(limits)
            # Immutable scope registration precedes admission; retrying after a
            # crash can only re-register the same ceilings, never reset counters.
            await self.service.usage.controlled_budget(
                self.work_id(context), "", workspace_row["data"].get("usage_limits", {})
            )
            await self.service.usage.controlled_budget(
                self.work_id(context),
                ident,
                {"max_requests": self.service.usage.settings.max_requests, **limits},
            )
            self.service.contexts.reserve(context, ident, args["revision"])
            call_token = current_call.set(CallIdentity(ident, context["owner"]))
            usage_token = current_usage.set(UsageScope(self.work_id(context), ident))
            try:
                submission = {
                    "request_id": ident,
                    "text": args["text"],
                    "inputs": args.get("inputs", []),
                    "model": context["data"]["model"],
                    "effort": context["data"]["effort"],
                }
                result = await self.service.submit(
                    submission, controlled={**context, "control_intent": intent}
                )
                self.service.broker.open(ident)
                return result
            except BaseException:
                if self.service.records.find(ident) is None:
                    # No execution was accepted, and no model/controlled operation
                    # can have dispatched. Restore the previous context reference.
                    row = self.service.contexts.get("context", context["id"])
                    ws = self.service.contexts.get("workspace", workspace)
                    with self.service.records.db:
                        self.service.records.db.execute(
                            "UPDATE execution_resources SET revision=revision+1,data=? WHERE id=?",
                            (
                                json.dumps({**row["data"], "state": "ready", "last_execution": previous}),
                                row["id"],
                            ),
                        )
                        self.service.records.db.execute(
                            "UPDATE execution_resources SET revision=revision+1,data=? WHERE id=?",
                            (json.dumps({**ws["data"], "active_execution": None}), ws["id"]),
                        )
                raise
            finally:
                current_usage.reset(usage_token)
                current_call.reset(call_token)

    async def request(self, principal, action, args):
        from assistant_runtime.api.runs import ExecutionRequest

        run = self.service.records.get(args.get("id"))
        context = self.service.contexts.owned(principal, "context", run["data"].get("context_id"))
        if action == "operation_get":
            if set(args) != {"id", "operation_id", "grant_id"}:
                raise ValueError("Operation lookup requires id, operation_id and grant_id")
            grant = self.service.authority.owned(principal, args["grant_id"])
            binding = {**run["data"]["control_binding"], "actor": grant["binding"]["actor"]}
            self.service.authority.verify(principal, grant["id"], binding, "read", grant["generation"])
            item = self.service.records.db.execute(
                "SELECT state,result FROM controlled_operations WHERE execution_id=? AND id=?",
                (run["id"], args["operation_id"]),
            ).fetchone()
            if item is None:
                raise ValueError("Unknown controlled operation")
            return {
                "id": args["operation_id"],
                "execution_id": run["id"],
                "state": item[0],
                "result": json.loads(item[1]),
            }
        if action in {"answer", "supplement"}:
            self.service.contexts.verify(context)
        token = current_call.set(CallIdentity(run["id"], context["owner"]))
        try:
            if action == "reconcile":
                return await self.reconcile(run)
            return await self.service.request(ExecutionRequest(action, args), _controlled=True)
        finally:
            current_call.reset(token)

    async def reconcile(self, run):
        stopped = await self.service.broker.fence(run["id"]) if run["state"] in TERMINAL else False
        summary = self.service.records.operations.reconcile(run["id"])
        # Resource fencing blocks every model-driven write even if the remote
        # provider is still computing. Never claim remote stop from local evidence.
        summary.update(
            local_writes="fenced" if stopped else "unknown",
            local_processes="controlled_children_stopped" if stopped else "unknown",
            external_effects="not_exposed",
            observation_coverage="controlled_resources",
            safe_to_retry=stopped and run["state"] in TERMINAL,
            reason="new_context_only" if stopped else "resource_stop_unconfirmed",
        )
        if stopped and run["state"] == "unknown":
            self.service.contexts.finish(run["data"]["context_id"], run["id"], stopped=True)
        with self.service.records.db:
            summary["observation_id"] = self.service.records.operations.observe(
                run["id"], None, {"kind": "controlled_reconciliation", **summary}
            )
        return summary

    def prepare_inputs(self, run):
        context = self.service.contexts.get("context", run["data"]["context_id"])
        self.service.contexts.verify(context)
        area = self.service.broker.files.execution_area(context["data"]["binding"]["workspace_id"], run["id"])
        inputs, descriptions = self.service.files.inputs.materialize(
            run["data"].get("inputs", []), area, directory="inputs"
        )
        for item in descriptions:
            from pathlib import Path

            item["path"] = Path(item["path"]).name
            item["area"] = "inputs"
        return area, inputs, descriptions

    async def session(self, run, area):
        context = self.service.contexts.get("context", run["data"]["context_id"])
        self.service.contexts.verify(context)
        self.service.broker.admitted(run["id"], "execute")
        # The agent's local cwd is its own engine scratch. Model resource tools
        # resolve opaque areas through the host; no arbitrary host path is exposed.
        parameters = {
            "model": run["data"]["model"],
            "budget_id": run["id"],
            "cwd": str(self.service.files.area("executions/" + run["id"]) / "scratch"),
            "instructions": "Use only the provided controlled tools. Areas: source is read-only baseline, work is the retained editable workspace, inputs are this round's read-only attachments, artifacts are this round's deliverables. Paths are relative to those areas. Check names select trusted fixed checks; do not invent shell commands.",
            "config": {
                "model_reasoning_effort": run["data"]["effort"],
                "allowed_tools": TOOLS,
                "skills": [],
                "mcp_servers": {},
                "controlled_context": context["id"],
                "control_token": run["data"]["resource_token"],
            },
        }
        native = context["data"]["native_session"]
        if native:
            parameters["session_id"] = native
            created = await self.service.client.request("session.resume", parameters)
        else:
            created = await self.service.client.request("session.create", parameters)
            self.service.contexts.save(context, native_session=created["session"]["id"])
        return created

    async def finish(self, run, *, native_closed):
        stopped = await self.service.broker.fence(run["id"])
        self.service.contexts.finish(run["data"]["context_id"], run["id"], stopped=stopped and native_closed)

    def authorize_session(self, session, *, required=False):
        execution = self.service.sessions.get(session)
        if execution is None:
            if required:
                raise PermissionError("Controlled session has no active host-bound execution")
            return
        run = self.service.records.get(execution)
        if run["data"].get("context_id"):
            self.service.broker.admitted(execution, "execute")

    async def interrupt(self, execution, code):
        if execution in self.interrupting:
            return
        self.interrupting.add(execution)
        try:
            run = self.service.records.get(execution)
            self.service.broker.cancel(execution)
            if run["state"] not in TERMINAL:
                self.service.records.update(execution, "unknown", error={"code": code})
            signal = self.service.finished.get(execution)
            if signal:
                signal.set()
            elif run["state"] == "queued":
                await self.finish(run, native_closed=True)  # No session dispatch occurred.
        finally:
            self.interrupting.discard(execution)

    async def watch(self):
        while not self.service.stopping:
            for run in self.service.records.active():
                if not run["data"].get("context_id"):
                    continue
                try:
                    self.service.contexts.verify(
                        self.service.contexts.get("context", run["data"]["context_id"])
                    )
                except (ValueError, PermissionError):
                    await self.interrupt(run["id"], "resource_authorization_unavailable")
            await asyncio.sleep(0.1)
