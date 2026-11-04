"""Pi foreground execution backend with unified model_api routing."""

import asyncio
import logging
import re
import uuid

from assistant_runtime.core.pi.rpc import PiClient, RpcError
from assistant_runtime.core.pi.mcp import server_config

log = logging.getLogger(__name__)


class PiBackend:
    def __init__(
        self,
        config,
        store,
        on_speech,
        on_control,
        mcp_registry=None,
        usage=None,
        *,
        skill_paths=(),
        runtime_dir=None,
        foreground=True,
    ):
        self.config, self.store = config, store
        self.foreground = foreground
        self.on_speech, self.on_control = on_speech, on_control
        from assistant_runtime.core.models.settings import ModelSettings

        settings = ModelSettings.parse(config.model_api)
        from assistant_runtime.core.pi.accounts import NativeAccounts

        accounts = NativeAccounts(
            config.root, settings, state_dir=config.state_dir / "model-accounts", binary=config.pi_binary
        )
        self.client = PiClient(
            config.pi_binary,
            config.workspace,
            settings=settings,
            environment={},
            runtime_dir=runtime_dir if runtime_dir is not None else config.state_dir / "executions",
            skill_paths=skill_paths,
            usage=usage,
            accounts=accounts,
            shell=getattr(config, "pi_shell", "auto"),
        )
        self.integrations = None
        if mcp_registry:
            from assistant_runtime.core.pi.integrations import PiIntegrations

            self.integrations = PiIntegrations(self, mcp_registry)
        self.voice_active = lambda: False
        self.jobs = None
        self.executions = None
        self.owner = self
        self.conversations = {}
        self._initialize_conversation()

    def _initialize_conversation(self):
        self.execution_session_id = None
        self.session_id = None
        self.setting_prefix = "pi"
        self.resumed_turns = []
        self.resolved_model = None
        self.resolved_reasoning_effort = None
        self.active_turn = self.active_task = None
        self.progress = ""
        from assistant_runtime.core.pi.turns import Turns

        self.turns = Turns()
        self.muted_turns = set()
        self.awaiting_new_message = set()
        self.buffers = {}
        self.last_error = None
        self.events_task = None
        self.pending_question = None
        self.submit_lock = asyncio.Lock()
        self.command_items = {}
        self.last_turn = None
        self.server_tasks = set()
        self.on_task_done = None
        self.on_execution_started = None
        self.on_conversation = None
        self.on_conversation_done = None
        self.on_conversation_notice = None
        self.last_user_text = "网页用户请求"

    @property
    def busy(self):
        return self.active_task is not None

    @property
    def context_compacting(self):
        worker = self.client.workers.get(self.execution_session_id)
        return bool(worker and worker.compacting)

    async def start(self):
        import secrets

        if not self.client.settings.enabled:
            if self.foreground:
                self.session_id = self.session_id or self.store.executions.logical_session("local", "main")
            return  # Web/configuration remains available; no provider is dispatched.
        self.client.environment["PA_EXECUTION_TOKEN"] = secrets.token_urlsafe(32)
        await self.client.start()
        if self.integrations:
            await self.integrations.start()
        if not self.foreground:
            self.events_task = asyncio.create_task(self._events())
            return
        await self._start_foreground()

    async def _start_foreground(self):
        from assistant_runtime.core.conversation.instructions import instructions

        if self.store.get("execution.generation") != "pi-v1":
            tables = {
                r[0] for r in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            legacy = self.store.get("thread_id") or any(
                self.store.db.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone()
                for table in ("tasks", "job_definitions")
                if table in tables
            )
            if legacy:
                raise RuntimeError(
                    "Legacy execution state requires stopped-instance backup/reset; run scripts.reset_execution_state --root ROOT first"
                )
            self.store.set("execution.generation", "pi-v1")
        saved = self.store.get(self.setting_prefix + ".execution_session_id")
        revision = (
            self.integrations.runtime_data["revision"]
            if self.integrations and self.integrations.runtime_data is not None
            else self.integrations.registry.revision
            if self.integrations
            else None
        )
        params = {
            "model": self.config.main_model,
            "cwd": self.config.workspace,
            "instructions": instructions(self.config),
            "config": {
                "model_reasoning_effort": self.config.main_reasoning_effort,
                "mcp_servers": self.integrations.configuration("foreground")
                if self.integrations
                else server_config(self.config.socket, plugins=self.config.enabled_plugins),
            },
        }
        policy_table = self.store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_policies'"
        ).fetchone()
        policy_row = (
            self.store.db.execute(
                "SELECT data FROM conversation_policies WHERE session_id=?", (self.session_id,)
            ).fetchone()
            if policy_table
            else None
        )
        if policy_row:
            import json

            policy = json.loads(policy_row[0])
            if "tools" in policy:
                params["config"]["allowed_tools"] = policy["tools"]
            if policy.get("instructions"):
                params["instructions"] += "\n" + policy["instructions"]
        if saved:
            prior = await self.client.request("session.read", {"session_id": saved, "include_runs": True})
            if prior["session"]["status"]["type"] == "unknown" or any(
                r.get("status") == "unknown" for r in prior["session"].get("runs", [])[-1:]
            ):
                self.store.set(self.setting_prefix + ".previous_unconfirmed_execution", saved)
                saved = None
        if saved:
            result = await self.client.request("session.resume", {"session_id": saved, **params}, timeout=60)
        if not saved:
            result = await self.client.request(
                "session.create",
                {
                    **params,
                },
                timeout=60,
            )
        self.execution_session_id = result["session"]["id"]
        if self.integrations:
            self.integrations.applied_revision = revision
            self.integrations.applied_configuration = params["config"]["mcp_servers"]
        self.resolved_model = result.get("model")
        self.resolved_reasoning_effort = result.get("reasoning_effort")
        self.store.set(self.setting_prefix + ".execution_session_id", self.execution_session_id)
        self.resumed_turns = result["session"].get("runs", [])
        self.session_id = self.session_id or self.store.executions.logical_session(
            "pi", self.execution_session_id
        )
        self.owner.conversations[self.execution_session_id] = self
        from assistant_runtime.core.workfiles import WorkFiles

        work = WorkFiles(self.config.state_dir, self.store.db, self.config.work_files)
        area = work.area("conversations/" + self.session_id)
        await self.client.request(
            "session.context",
            {
                "session_id": self.execution_session_id,
                "items": [
                    {
                        "type": "message",
                        "role": "developer",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "本会话临时脚本、下载和中间文件请放 "
                                + str(area / "scratch")
                                + "；交付文件放 "
                                + str(area / "artifacts")
                                + "。修改用户项目仍写目标项目，不将临时文件散落到仓库。",
                            }
                        ],
                    }
                ],
            },
        )
        if self is self.owner:
            self.store.set("session_id", self.session_id)
        turns = {t["id"]: t for t in result["session"].get("runs", [])}
        for turn in list(turns.values())[-20:]:
            self.command_items[turn["id"]] = {
                i["id"] for i in turn.get("items", []) if i.get("type") == "command"
            }
            self.last_turn = turn["id"]
        for ident, turn, _ in self.store.executions.unfinished(self.session_id):
            t = turns.get(turn)
            status = (t or {}).get("status", "unknown")
            if status == "running":
                status = "unknown"
            self.store.executions.update_task(ident, status)
        if self is self.owner:
            self.events_task = asyncio.create_task(self._events())
        log.info(
            "pi_ready resumed=%s model=%s reasoning_effort=%s",
            bool(saved),
            self.resolved_model,
            self.resolved_reasoning_effort,
        )

    async def open_conversation(self, session_id):
        for conversation in self.conversations.values():
            if conversation.session_id == session_id:
                return conversation
        child = object.__new__(PiBackend)
        child.config, child.store = self.config, self.store
        child.client, child.integrations = self.client, None
        if self.integrations:
            from assistant_runtime.core.pi.integrations import PiIntegrations

            child.integrations = PiIntegrations(child, self.integrations.registry)
            child.integrations.inherited = self.integrations.inherited.copy()
            child.integrations.runtime_data = self.integrations.runtime_data
        child.owner = self
        child.conversations = self.conversations
        child.foreground = True
        child.voice_active = lambda: False
        child.jobs, child.executions = self.jobs, self.executions
        child.on_speech = child.on_control = None
        child._initialize_conversation()
        child.session_id = session_id
        child.setting_prefix = "conversation." + session_id
        if self.client.settings.enabled:
            await child._start_foreground()
        return child

    def restored_conversation_events(self):
        from assistant_runtime.core.pi.conversation import task_event

        for turn in self.resumed_turns:
            for item in turn.get("items", []):
                event = task_event("activity.completed", {"run_id": turn["id"], "item": item})
                if event:
                    yield event
            if turn.get("status") not in ("running", "running"):
                yield task_event("run.completed", {"run": turn})

    async def import_voice_messages(self, messages):
        """Append confirmed voice context without executing another task."""
        await self.client.request(
            "session.context",
            {
                "session_id": self.execution_session_id,
                "items": [
                    {
                        "type": "message",
                        "role": m["role"],
                        "content": [
                            {
                                "type": "input_text" if m["role"] == "user" else "output_text",
                                "text": m["text"],
                            }
                        ],
                    }
                    for m in messages
                ],
            },
        )

    async def submit(self, text, images=(), *, cancelled=lambda: False, retry_of=None):
        if not self.foreground:
            raise RuntimeError("Conversation capability is not enabled")
        if not self.client.settings.enabled:
            raise RuntimeError("Model configuration required before inference")
        if self.integrations:
            await self.integrations.apply()
        async with self.submit_lock:
            if cancelled():
                from assistant_runtime.api.conversation import ConversationCancelled

                raise ConversationCancelled("Conversation input cancelled before dispatch")
            if self.integrations:
                self.integrations.require_confirmed()
            if retry_of and (
                self.active_turn
                or self.pending_question
                or not await self.control_checkpoint(retry_of, stopped=True)
            ):
                raise ValueError("无法安全恢复：当前执行或操作状态需要先核对")
            if self.pending_question:
                if images:
                    raise ValueError("请先用文字回答待确认的问题，再发送图片")
                q = self.pending_question
                self.pending_question = None
                answers = {item["id"]: {"answers": [text]} for item in q["data"].get("questions", [])}
                await self.client.respond(q["id"], {"answers": answers})
                return "answered"
            inputs = [{"type": "text", "text": text}]
            inputs.extend({"type": "localImage", "path": path} for path in images)
            if self.active_turn:
                self.awaiting_new_message.add(self.active_turn)
                await self.client.request(
                    "run.steer",
                    {
                        "session_id": self.execution_session_id,
                        "expected_run_id": self.active_turn,
                        "input": inputs,
                    },
                )
                return "steered"
            ident = uuid.uuid4().hex
            self.active_task = ident
            self.progress = "已经收到，正在处理。"
            self.store.executions.create_task(ident, text, self.session_id)
            try:
                result = await self.client.request(
                    "run.start",
                    {
                        "session_id": self.execution_session_id,
                        "execution_context": {
                            "source": "conversation",
                            "reference": ident,
                            "work_area": "conversations/" + self.session_id,
                        },
                        "input": inputs,
                        "model": self.config.main_model,
                        "effort": self.config.main_reasoning_effort,
                        **({"retry_of": retry_of} if retry_of else {}),
                    },
                )
                turn = result["run"]["id"]
                if result.get("execution_id"):
                    self.store.executions.bind(ident, result["execution_id"])
                if self.active_task == ident:
                    self.active_turn = turn
                    self.store.executions.update_task(ident, "running", turn)
            except BaseException as exc:
                interrupted = False
                if self.client.admission:
                    admission = self.client.admission.service
                    attempt = admission.sessions.get(self.execution_session_id)
                    row = admission.records.get(attempt) if attempt else None
                    interrupted = bool(
                        row
                        and row["data"].get("reference") == ident
                        and row["state"] in {"cancelled", "interrupted"}
                        and not row["data"].get("turn_id")
                    )
                self.store.executions.update_task(ident, "interrupted" if interrupted else "unknown")
                self.active_task = self.active_turn = None
                if interrupted and isinstance(exc, Exception):
                    from assistant_runtime.api.conversation import ConversationCancelled

                    raise ConversationCancelled("Conversation input cancelled before dispatch") from exc
                raise
            return "started"

    def mute_current(self):
        if self.active_turn:
            self.muted_turns.add(self.active_turn)

    async def conversation_input(self, action, text, expected_turn, question_id=None):
        async with self.submit_lock:
            if self.active_turn != expected_turn:
                raise ValueError("Conversation execution changed")
            if action == "answer":
                question = self.pending_question
                if not question or str(question["id"]) != str(question_id):
                    raise ValueError("Question is no longer pending")
                answers = {item["id"]: {"answers": [text]} for item in question["data"].get("questions", [])}
                await self.client.respond(question["id"], {"answers": answers})
                if self.pending_question is question:
                    self.pending_question = None
                return {"state": "answered"}
            if self.pending_question:
                raise ValueError("Answer the pending question explicitly before supplementing")
            await self.client.request(
                "run.steer",
                {
                    "session_id": self.execution_session_id,
                    "expected_run_id": expected_turn,
                    "input": [{"type": "text", "text": text}],
                },
            )
            return {"state": "supplemented"}

    async def cancel(self, expected_turn=None):
        if self.active_task and self.active_turn is None and self.client.admission:
            await self.client.request("run.cancel", {"session_id": self.execution_session_id})
            return True
        if expected_turn is not None and self.active_turn != expected_turn:
            return False
        turn = expected_turn or self.active_turn or self.last_turn
        interrupted = self.active_turn is not None
        if not turn:
            return False
        thread = self.execution_session_id
        owned_items = set(self.command_items.get(turn, set()))
        if interrupted:
            self.muted_turns.add(turn)
            await self.client.request("run.cancel", {"session_id": thread, "run_id": turn})
        cursor = None
        terminals = []
        while True:
            params = {"session_id": thread}
            if cursor:
                params["cursor"] = cursor
            page = await self.client.request("operations.list", params)
            terminals.extend(page.get("data", []))
            cursor = page.get("next_cursor")
            if not cursor:
                break
        for terminal in terminals:
            if terminal["activity_id"] in owned_items:
                await self.client.request(
                    "operations.stop",
                    {"session_id": thread, "process_id": terminal["process_id"]},
                )
                interrupted = True
        return interrupted

    async def control_checkpoint(self, turn, *, stopped=False):
        """Read provider evidence; never infer a stop from interrupt RPC acceptance."""
        data = await self.client.request(
            "session.read", {"session_id": self.execution_session_id, "include_runs": True}
        )
        record = next((t for t in data["session"].get("runs", []) if t["id"] == turn), None)
        if not record or (stopped and record.get("status") not in {"interrupted", "completed", "failed"}):
            return False
        if not stopped and self.active_turn != turn:
            return False
        for item in record.get("items", []):
            if item.get("type") not in {"mcp", "command", "file"}:
                continue
            if item.get("server") == "decisions" and item.get("tool") == "confirm_voice_decision":
                continue
            if item.get("tool_outcome", {}).get("outcome") == "unknown":
                return False
            if item.get("status") not in {"completed", "failed", "declined"}:
                return False
        terminals = await self.client.request("operations.list", {"session_id": self.execution_session_id})
        return (
            (stopped or self.active_turn == turn)
            and not terminals.get("data")
            and not terminals.get("next_cursor")
        )

    async def _speech(self, text, turn):
        return  # TODO: independently accepted voice adapter.

    async def _events(self):
        while True:
            event = await self.client.events.get()
            method, p = event.get("kind", ""), event.get("data", {})
            if self.integrations and await self.integrations.event(event):
                continue
            if method == "executor.disconnected":
                if self.executions:
                    await self.executions.disconnected()
                for conversation in {self, *self.conversations.values()}:
                    if conversation.active_task:
                        self.store.executions.update_task(conversation.active_task, "unknown")
                        if conversation.on_conversation_done:
                            await conversation.on_conversation_done(conversation.active_turn, "", "unknown")
                raise RpcError("Agent runtime disconnected; pending work will not be automatically repeated")
            if self.executions and await self.executions.event(event):
                continue
            if self.jobs and await self.jobs.event(event):
                continue
            target = self.conversations.get(p.get("session_id"), self)
            await target._conversation_event(event)

    async def _conversation_event(self, event):
        method, p = event.get("kind", ""), event.get("data", {})
        if "id" in event and "kind" in event:
            if not self.foreground or p.get("session_id") and p["session_id"] != self.execution_session_id:
                await self.client.send(
                    {
                        "id": event["id"],
                        "error": {"code": -32800, "message": "Execution no longer owns this request"},
                    }
                )
                return
            task = asyncio.create_task(self._server_request(event))
            self.server_tasks.add(task)
            task.add_done_callback(self.server_tasks.discard)
            return
        if not self.foreground or p.get("session_id") and p["session_id"] != self.execution_session_id:
            return
        if self.on_conversation:
            from assistant_runtime.core.pi.conversation import task_event

            mapped = task_event(method, p)
            if mapped:
                self.on_conversation(mapped)
        if method == "input.answered":
            if self.pending_question and self.pending_question["id"] == p.get("question_id"):
                self.pending_question = None
        if method == "run.started":
            ident = p["run"]["id"]
            if ident in self.turns.records:
                return
            if self.active_task is None or self.active_turn is not None and self.active_turn != ident:
                self.active_task = uuid.uuid4().hex
                self.store.executions.create_task(self.active_task, self.last_user_text, self.session_id)
            self.turns.start(ident, self.active_task)
            self.progress = ""
            self.buffers.clear()
            self.active_turn = ident
            if self.on_execution_started:
                self.on_execution_started(ident)
            self.last_turn = self.active_turn
            self.command_items.setdefault(self.active_turn, set())
            if len(self.command_items) > 20:
                self.command_items.pop(next(iter(self.command_items)))
            if self.active_task:
                if p.get("execution_id"):
                    self.store.executions.bind(self.active_task, p["execution_id"])
                self.store.executions.update_task(self.active_task, "running", self.active_turn)
        elif method == "activity.started" and p.get("item", {}).get("type") == "message":
            turn = p.get("run_id", self.active_turn)
            if turn in self.awaiting_new_message:
                self.awaiting_new_message.discard(turn)
                self.muted_turns.discard(turn)
        elif method == "activity.started":
            if p.get("run_id", self.active_turn) != self.active_turn:
                return
            kind = p.get("item", {}).get("type")
            if kind == "command":
                self.progress = "正在执行本地命令。"
                turn = p.get("run_id", self.active_turn)
                self.command_items.setdefault(turn, set()).add(p["item"]["id"])
            elif kind == "file":
                self.progress = "正在修改本地文件。"
        elif method == "message.delta":
            if p.get("run_id", self.active_turn) != self.active_turn:
                return
            ident = p.get("activity_id", "message")
            value = self.buffers.get(ident, "") + p.get("delta", "")
            pieces = re.split(r"(?<=[。！？\n])", value)
            self.buffers[ident] = pieces.pop()
            for sentence in pieces:
                await self._speech(sentence, p.get("run_id", self.active_turn))
        elif method == "activity.completed":
            item = p.get("item", {})
            if item.get("type") == "message":
                ident = p.get("run_id", self.active_turn)
                record = self.turns.active(ident)
                if not record or ident != self.active_turn or not record.message(item):
                    return
                tail = self.buffers.pop(item.get("id"), "")
                await self._speech(tail, p.get("run_id", self.active_turn))
                text = item.get("text", "")
                self.progress = record.progress[:600]
                if self is self.owner:
                    self.store.transcripts.append("assistant", text)
                if self.active_task:
                    self.store.executions.update_task(self.active_task, "running", result=text)
        elif method == "run.completed":
            turn = p["run"]
            ident = turn["id"]
            record = self.turns.active(ident)
            if not record:
                return
            for item in turn.get("items", []):
                if item.get("type") == "message":
                    record.message(item)
            record.status = turn["status"]
            if "result" in p:
                record.result = p["result"]
            self.store.executions.update_task(record.task_id, turn["status"], ident, result=record.result)
            if self.on_conversation_done:
                task = asyncio.create_task(self.on_conversation_done(ident, record.result, turn["status"]))
                self.server_tasks.add(task)
                task.add_done_callback(self.server_tasks.discard)
            if self.active_task and ident == self.active_turn:
                self.store.executions.update_task(self.active_task, turn["status"])
                self.active_task = self.active_turn = None
                self.pending_question = None
                self.progress = "任务已经结束。" + (record.result or "本轮没有文字结果。")
                if self is self.owner and turn["status"] == "completed" and record.result:
                    self.store.transcripts.append(
                        "verified",
                        "执行器已完成任务，provider turn="
                        + ident
                        + "。最终报告（模型报告，不是额外事实核验）："
                        + record.result[:1500],
                    )
                if self.on_task_done:
                    self.on_task_done()
            if turn.get("status") == "failed":
                self.last_error = str(turn.get("error", "Task failed"))[:500]
                await self._speech("这次任务没有完成，错误原因已经记录。", ident)
            self.muted_turns.discard(ident)
            self.awaiting_new_message.discard(ident)
        elif method == "error":
            self.last_error = str(p.get("error", "Provider error"))[:500]
            log.warning("provider_error_received")

    async def _client_notice(self, text, turn_id):
        if self.on_conversation_notice:
            await self.on_conversation_notice(text, turn_id or self.active_turn)
        elif self.on_speech:
            await self.on_speech(text)

    async def _server_request(self, event):
        method, p = event["kind"], event.get("data", {})
        if method == "input.requested":
            self.pending_question = event
            if self.on_conversation:
                self.on_conversation(
                    {
                        "kind": "question",
                        "question_id": event["id"],
                        "questions": p.get("questions", []),
                        "turn_id": p.get("run_id"),
                    }
                )
            for question in p.get("questions", []):
                await self._client_notice(question["question"], p.get("run_id"))
        else:
            await self.client.send(
                {"id": event["id"], "error": {"code": -32601, "message": "Unsupported client request"}}
            )

    async def close(self):
        for child in self.conversations.values():
            if child is not self:
                for task in list(child.server_tasks):
                    task.cancel()
                await asyncio.gather(*child.server_tasks, return_exceptions=True)
        for task in list(self.server_tasks):
            task.cancel()
        await asyncio.gather(*self.server_tasks, return_exceptions=True)
        if self.events_task:
            self.events_task.cancel()
            await asyncio.gather(self.events_task, return_exceptions=True)
        await self.client.close()
