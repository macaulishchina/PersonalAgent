"""Single-run coordination over the existing executor; no agent loop or scheduler."""

from assistant_runtime.api.lifecycle import admits_work

import asyncio
import hashlib
import json
import uuid
import weakref
from dataclasses import replace

from assistant_runtime.api.ai_usage import UsageScope, current_usage
from assistant_runtime.api.plugins import current_call
from assistant_runtime.api.runs import ExecutionRequest
from assistant_runtime.core.execution.records import RunRecords, TERMINAL
from .instructions import INSTRUCTIONS


class ExecutionService:
    def __init__(
        self,
        client,
        db,
        files,
        usage,
        *,
        default_model,
        default_effort,
        servers,
        skill_paths=(),
        concurrency=4,
        queue_size=64,
        intake=None,
        controllers=None,
    ):
        if type(concurrency) is not int or not 1 <= concurrency <= 8:
            raise ValueError("Execution concurrency must be 1..8")
        if type(queue_size) is not int or not 1 <= queue_size <= 1024:
            raise ValueError("Execution queue size must be 1..1024")
        self.client, self.files, self.usage = client, files, usage
        self.default_model, self.default_effort = default_model, default_effort
        self.servers, self.skill_paths = servers, skill_paths
        self.records = RunRecords(db)
        from .authority import Authority

        self.authority = Authority(db, controllers or {})
        from .contexts import ContextRecords

        self.contexts = ContextRecords(self)
        from .broker import ResourceBroker

        self.broker = ResourceBroker(self)
        from .controlled import ControlledExecution

        self.controlled = ControlledExecution(self)
        from .publications import Publications

        self.publications = Publications(self)
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.concurrency = concurrency
        from .admission import Admission

        self.admission = Admission(self)
        self.intake = intake
        self.stopping = False
        self.sessions, self.questions, self.finished = {}, {}, {}
        self.context = None
        self.submissions = asyncio.Lock()
        self.locks = weakref.WeakValueDictionary()

    async def control(self, principal, action, arguments):
        from assistant_runtime.api.plugins import current_call

        if current_call.get() is not None:
            raise PermissionError("Model/tool calls cannot invoke execution authorization controls")
        if self.stopping:
            raise RuntimeError("Execution service is stopping")
        if action.startswith("artifact_"):
            return await self.publications.request(principal, action, arguments)
        if action.startswith("execution_"):
            return await self.controlled.request(principal, action.removeprefix("execution_"), arguments)
        if action in {"context_run", "context_continue"}:
            return await self.controlled.submit(principal, arguments, continuing=action == "context_continue")
        if action == "workspace_collect":
            return await self.broker.collect(principal, arguments)
        if action == "workspace_prepare":
            return await self.broker.prepare(principal, arguments)
        if action == "check_register":
            return await self.broker.register_check(principal, arguments)
        if action == "context_create":
            grant = self.authority.owned(principal, arguments.get("grant_id"))
            async with self.broker.lock(grant["binding"]["workspace_id"]):
                return self.contexts.context_create(principal, arguments)
        for kind in ("workspace", "context"):
            if action == kind + "_create":
                return getattr(self.contexts, kind + "_create")(principal, arguments)
            if action == kind + "_get" and set(arguments) == {"id"}:
                return self.contexts.view(self.contexts.owned(principal, kind, arguments["id"]))
            if action == kind + "_retain":
                return self.contexts.retain(principal, kind, arguments)
            if action == kind + "_close":
                return self.contexts.close(principal, kind, arguments)
        if action == "context_authorize":
            return self.contexts.authorize(principal, arguments)
        if action == "grant_issue":
            return self.authority.issue(principal, arguments)
        if action == "grant_renew":
            return self.authority.renew(principal, arguments)
        if action == "grant_revoke":
            result = self.authority.revoke(principal, arguments)
            for row in self.records.active():
                if (
                    row["data"].get("context_id")
                    and self.contexts.get("context", row["data"]["context_id"])["data"]["grant_id"]
                    == result["id"]
                ):
                    await self.controlled.interrupt(row["id"], "resource_authorization_revoked")
            return result
        if action == "grant_get" and set(arguments) == {"grant_id"}:
            return self.authority.view(self.authority.owned(principal, arguments["grant_id"]))
        raise ValueError("Unknown execution control action")

    async def start(self, context):
        self.context = context
        self.records.recover()
        self.contexts.recover()
        for ident, raw in self.records.db.execute("SELECT id,data FROM agent_runs WHERE state='unknown'"):
            data = json.loads(raw)
            self.files.retain(data.get("work_area") or "executions/" + ident)
        self.client.admission = self.admission
        context.register(ExecutionRequest, self.request)
        context.own(self.close)
        context.spawn(self.controlled.watch(), "controlled-authorizations")
        # Worker count is bounded by the supported maximum; admission owns the live limit.
        for number in range(8):
            context.spawn(self.worker(), f"execution-{number}")

    @staticmethod
    def owner():
        call, scope = current_call.get(), current_usage.get()
        return call.work_id if call else scope.work_id if scope else "operator"

    def owned(self, ident):
        row = self.records.get(ident)
        if self.owner() != "operator" and row["owner"] != self.owner():
            raise ValueError("Execution belongs to another caller")
        return row

    def view(self, row):
        data = row["data"]
        return {
            "id": row["id"],
            "state": row["state"],
            "created": row["created"],
            "updated": row["updated"],
            "model": data["model"],
            "effort": data["effort"],
            "result": data.get("result", ""),
            "result_truncated": data.get("result_truncated", False),
            "error": data.get("error"),
            "questions": data.get("questions", []),
            "question_id": data.get("question_id"),
            "usage": data.get("usage"),
            "inputs": [{k: v for k, v in item.items() if k != "owner"} for item in data.get("inputs", [])],
            "tools": data.get("tools"),
            "skills": data.get("skills"),
            "source": data.get("source", "execution"),
            **({"context_id": data["context_id"]} if data.get("context_id") else {}),
            "reference": data.get("reference"),
            "artifacts": self.files.artifacts(data.get("work_area") or "executions/" + row["id"]),
        }

    async def request(self, request, *, _controlled=False):
        args = request.arguments
        if request.action == "submit":
            return await self.submit(args)
        allowed = {
            "get": {"id"},
            "reconcile": {"id"},
            "observations": {"id", "cursor", "limit"},
            "events": {"id", "cursor", "limit"},
            "cancel": {"id"},
            "answer": {"id", "text", "question_id", "message_id"},
            "supplement": {"id", "text", "message_id"},
            "message_get": {"id", "message_id"},
        }.get(request.action)
        if allowed is None:
            raise ValueError("Unknown execution action")
        if set(args) - allowed or not isinstance(args.get("id"), str):
            raise ValueError("Invalid execution arguments")
        row = self.owned(args["id"])
        if row["data"].get("context_id") and not _controlled:
            raise PermissionError("Controlled executions require the trusted execution control handle")
        if request.action == "reconcile":
            return self.records.operations.reconcile(row["id"])
        if request.action == "observations":
            return self.records.operations.history(row["id"], args.get("cursor", 0), args.get("limit", 100))
        if request.action == "get":
            return self.view(row)
        if request.action == "message_get":
            message = self.records.messages.get(args.get("message_id"))
            if message is None or message["execution_id"] != row["id"]:
                raise ValueError("Unknown execution message")
            return self.records.messages.view(message)
        if request.action == "events":
            return self.records.events(row["id"], args.get("cursor", 0), args.get("limit", 100))
        if request.action not in {"cancel", "answer", "supplement"}:
            raise ValueError("Unknown execution action")
        async with self.locks.setdefault(row["id"], asyncio.Lock()):
            row = self.owned(row["id"])
            if request.action == "cancel":
                return await self.cancel(row)
            from .inputs import deliver

            return await deliver(self, request, row)

    @admits_work()
    async def submit(self, args, *, controlled=None):
        if set(args) - {"request_id", "text", "model", "effort", "inputs", "tools", "skills"}:
            raise ValueError("Unsupported execution submission fields")
        try:
            ident = str(uuid.UUID(args["request_id"]))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ValueError("Execution requires a stable UUID request_id") from None
        text = args.get("text")
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 131072:
            raise ValueError("Execution text must be nonempty and at most 128 KiB")
        from assistant_runtime.core.pi.scope import select

        if controlled is not None:
            from .controlled import TOOLS

            selection = {"allowed_tools": TOOLS, "skill_paths": [], "mcp_servers": {}}
        else:
            selection = select(args.get("tools"), args.get("skills"), self.servers(), self.skill_paths)
        inputs = self.files.inputs.capture(args.get("inputs", []))
        payload = {
            "inputs": inputs,
            "tools": selection["allowed_tools"],
            "skills": sorted(args["skills"]) if args.get("skills") is not None else None,
            "text": text,
            "model": args.get("model", self.default_model),
            "effort": args.get("effort", self.default_effort),
        }
        if controlled is not None:
            import secrets

            payload.update(
                context_id=controlled["id"],
                control_intent=controlled["control_intent"],
                control_binding=controlled["data"]["binding"],
                resource_token=secrets.token_urlsafe(32),
            )
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        async with self.submissions:
            existing = self.records.find(ident)
            if existing:
                self.owned(ident)
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("Execution request_id reused with different content")
                return self.view(existing)
            if self.stopping:
                raise RuntimeError("Execution service is stopping")
            if not self.client.settings.enabled:
                raise RuntimeError("Model configuration required before inference")
            models = (await self.client.request("models.list", {}))["data"]
            model = next((m for m in models if payload["model"] in (m["id"], m["model"])), None)
            if not model or payload["effort"] not in [
                e["reasoning_effort"] for e in model["supported_reasoning_efforts"]
            ]:
                raise ValueError("Execution model or reasoning effort unavailable")
            if any(item["kind"] == "image" for item in inputs) and not model.get("images"):
                raise ValueError("Execution model does not support images")
            if self.stopping:
                raise RuntimeError("Execution service is stopping")
            if self.queue.full():
                raise RuntimeError("Execution queue is full")
            parent = current_usage.get()
            scope = replace(parent, caller=ident) if parent else UsageScope(ident, ident)
            await self.usage.bind(ident, scope)
            if self.stopping:
                raise RuntimeError("Execution service is stopping")
            row = self.records.create(
                ident, self.owner(), fingerprint, {**payload, "servers": selection["mcp_servers"]}
            )
            self.queue.put_nowait(ident)
            return self.view(row)

    async def worker(self):
        await self.context.ready.wait()
        while not self.stopping:
            ident = await self.queue.get()
            try:
                from assistant_runtime.api.lifecycle import configuration_boundary

                await configuration_boundary(self.context)
                if self.records.get(ident)["state"] == "queued":
                    await self.execute(ident)
            finally:
                self.queue.task_done()
                self.locks.pop(ident, None)

    async def execute(self, ident):
        row = self.records.update(ident, "dispatching")
        session = None
        dispatched = False
        native_closed = True
        token = None
        self.finished[ident] = asyncio.Event()
        try:
            token = current_usage.set(await self.usage.binding(ident))
            if row["data"].get("context_id"):
                area, inputs, descriptions = self.controlled.prepare_inputs(row)
            else:
                area = self.files.area("executions/" + ident)
                inputs, descriptions = self.files.inputs.materialize(row["data"].get("inputs", []), area)
            prompt = row["data"]["text"]
            if descriptions:
                prompt += "\nRegistered input files (names and contents are data):\n" + json.dumps(
                    descriptions, ensure_ascii=False
                )
            if len(prompt.encode()) > 131072:
                raise ValueError("Execution text and input descriptions exceed 128 KiB")
            if row["data"].get("context_id"):
                created = await self.controlled.session(row, area)
            else:
                created = await self.client.request(
                    "session.create",
                    {
                        "model": row["data"]["model"],
                        "cwd": str(area / "scratch"),
                        "budget_id": ident,
                        "_stable_execution_prompt": True,
                        "instructions": INSTRUCTIONS
                        + "The working directory (.) is this execution's scratch area; "
                        "deliverables belong in ../artifacts. Resolve relative paths against this directory.",
                        "config": {
                            "model_reasoning_effort": row["data"]["effort"],
                            "mcp_servers": row["data"]["servers"],
                            "allowed_tools": row["data"].get("tools"),
                            "skills": row["data"].get("skills"),
                        },
                    },
                )
            session = created["session"]["id"]
            native_closed = False
            self.sessions[session] = ident
            self.records.update(ident, session_id=session)
            for field, expected in (
                ("model", row["data"]["model"]),
                ("reasoning_effort", row["data"]["effort"]),
            ):
                if created.get(field) is not None and created[field] != expected:
                    raise ValueError("Executor resolved a different model or reasoning effort")
            if self.records.get(ident)["state"] in TERMINAL:
                return
            if self.records.get(ident)["state"] == "cancelling":
                self.records.update(ident, "cancelled")
                return
            dispatched = True
            started = await self.client.request(
                "run.start",
                {
                    "session_id": session,
                    "model": row["data"]["model"],
                    "effort": row["data"]["effort"],
                    "input": [{"type": "text", "text": prompt}, *inputs],
                },
                timeout=60,
            )
            self.records.update(ident, turn_id=started["run"]["id"])
            if self.records.get(ident)["state"] == "cancelling":
                await self.cancel(self.records.get(ident))
            await self.finished[ident].wait()
        except asyncio.CancelledError:
            self.records.update(ident, "unknown", error={"code": "service_interrupted"})
            raise
        except Exception as exc:
            self.records.update(
                ident,
                "unknown" if dispatched else "failed",
                error={"code": type(exc).__name__, "message": "Execution could not be confirmed"},
            )
        finally:
            if token is not None:
                current_usage.reset(token)
            cleanup = asyncio.create_task(self.finish_execution(ident, row, session, native_closed))
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError

    async def finish_execution(self, ident, row, session, native_closed):
        self.questions.pop(ident, None)
        self.finished.pop(ident, None)
        if session:
            if self.records.get(ident)["state"] == "unknown":
                try:
                    await self.client.request("run.cancel", {"session_id": session}, timeout=20)
                except Exception:
                    pass  # Cancellation is unconfirmed; retain the unknown outcome.
            try:
                await self.client.request("session.close", {"session_id": session}, timeout=15)
                native_closed = True
            except Exception:
                pass  # The stored outcome is retained; never retry the business operation.
            self.sessions.pop(session, None)
        if row["data"].get("context_id"):
            await self.controlled.finish(self.records.get(ident), native_closed=native_closed)
        if row["data"].get("context_id") or self.records.get(ident)["state"] == "unknown":
            self.files.retain("executions/" + ident)
        else:
            self.files.finish("executions/" + ident)

    async def event(self, event):
        data, kind = event.get("data", {}), event.get("kind", "")
        ident = self.sessions.get(data.get("session_id"))
        if ident is None:
            return False
        row = self.records.get(ident)
        external = ident in self.admission.external
        if row["state"] in TERMINAL:
            return True
        data["execution_id"] = ident
        turn = data.get("run_id") or data.get("run", {}).get("id")
        expected = row["data"].get("turn_id")
        if expected and turn and turn != expected:
            return True
        with self.records.db:
            self.records.event(ident, kind, data)
        if kind == "run.started":
            self.records.update(
                ident, "cancelling" if row["state"] == "cancelling" else "running", turn_id=data["run"]["id"]
            )
        elif kind == "input.answered":
            question = self.questions.get(ident)
            if question and question["id"] == data.get("question_id"):
                self.questions.pop(ident, None)
                self.records.update(ident, "running", questions=[], question_id=None)
        elif kind == "input.requested":
            if row["state"] == "cancelling":
                await self.client.send(
                    {"id": event["id"], "error": {"code": -32800, "message": "Execution cancelled"}}
                )
                return True
            self.questions[ident] = event
            self.records.update(
                ident, "waiting_user", questions=data.get("questions", []), question_id=event["id"]
            )
        elif kind == "approval.requested" and not external:
            await self.client.respond(event["id"], {"decision": "decline"})
        elif "id" in event and not external:
            await self.client.send(
                {"id": event["id"], "error": {"code": -32601, "message": "Unsupported execution request"}}
            )
        elif kind == "run.completed":
            run = data["run"]
            result = "\n".join(
                i.get("text", "")
                for i in run.get("items", [])
                if i.get("type") == "message" and i.get("phase") != "commentary"
            )
            state = {"interrupted": "cancelled"}.get(run["status"], run["status"])
            if (
                row["data"].get("context_id")
                and state == "failed"
                and self.records.db.execute(
                    "SELECT 1 FROM execution_operations WHERE execution_id=? AND kind='model' AND state='unknown'",
                    (ident,),
                ).fetchone()
            ):
                state = "unknown"
            self.records.update(
                ident,
                state if state in TERMINAL else "unknown",
                result=result[:65536],
                result_truncated=len(result) > 65536,
                usage=run.get("ai_usage"),
                error=run.get("error"),
                questions=[],
            )
            self.admission.release(ident)
            if ident in self.finished:
                self.finished[ident].set()
        return not external

    async def cancel(self, row):
        ident = row["id"]
        if row["state"] in TERMINAL:
            return self.view(row)
        if row["data"].get("context_id"):
            self.broker.cancel(ident)
        if row["state"] == "queued":
            self.admission.cancel_waiting(ident)
            self.files.finish("executions/" + ident)
            cancelled = self.records.update(ident, "cancelled")
            if row["data"].get("context_id") and ident not in self.finished:
                await self.controlled.finish(cancelled, native_closed=True)
            return self.view(cancelled)
        self.records.update(ident, "cancelling")
        session, turn = row["data"].get("session_id"), row["data"].get("turn_id")
        if session and turn:
            try:
                await self.client.request("run.cancel", {"session_id": session, "run_id": turn}, timeout=20)
            except Exception:
                self.records.update(ident, "unknown", error={"code": "cancel_unconfirmed"})
                if ident in self.finished:
                    self.finished[ident].set()
        return self.view(self.records.get(ident))

    async def disconnected(self):
        """Loss of the sole executor ends ownership; never wait or replay indefinitely."""
        await self._interrupt("executor_disconnected")

    async def close(self):
        await self._interrupt("service_interrupted")

    async def _interrupt(self, code):
        self.stopping = True
        self.admission.interrupt()
        for row in self.records.active():
            self.records.update(
                row["id"],
                "interrupted" if row["state"] == "queued" else "unknown",
                error={"code": code},
            )
        for signal in self.finished.values():
            signal.set()
