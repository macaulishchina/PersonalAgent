"""Application host, device resources and management IPC. Voice is pending acceptance."""

import asyncio
import uuid
from assistant_runtime.api.system import system
import json
import logging
import os
import time


from assistant_runtime.app.store import Store
from assistant_runtime.api.contracts import (
    Control,
    MemoryRequest,
    DeviceQuery,
    VideoCapture,
    VideoRecord,
    CameraMotion,
)

log = logging.getLogger(__name__)


class AssistantService:
    def __init__(self, config, no_mic=False):
        self.config, self.no_mic = config, True  # TODO: independent voice acceptance
        config.state_dir.mkdir(parents=True, exist_ok=True)
        system.private(config.state_dir, directory=True)
        self.lock_file = (config.state_dir / "service.lock").open("w")
        try:
            system.lock(self.lock_file, blocking=False)
            from .plugin_selection import resolve_selection
            from .release_state import attempted
            from .release_inventory import releases

            resolve_selection(config.enabled_plugins, config.plugin_backends, system.name)
            installed, _ = releases()
            attempted(config.root, {name: installed[name]["version"] for name in config.enabled_plugins})
            self.store = Store(config.state_dir / "assistant.sqlite3")
        except BaseException:
            self.lock_file.close()
            raise
        self.stop_event = asyncio.Event()
        self.started = time.time()
        from assistant_runtime.api.lifecycle import WorkIntake

        self.intake = WorkIntake()
        from .maintenance import Maintenance

        self.maintenance = Maintenance(self)
        from .configuration_updates import ConfigurationRuntime

        self.configuration = ConfigurationRuntime(self)
        self.startup_complete = False
        self.assistant = None
        from assistant_runtime.app.bootstrap import compose

        try:
            self.runtime = compose(self)
        except BaseException:
            self.store.db.close()
            self.lock_file.close()
            raise

    def _tool_timeout(self):
        from assistant_runtime.core.models.settings import ModelSettings

        return ModelSettings.parse(self.config.model_api).outer_timeout_seconds

    def status(self):
        if self.assistant is not None:
            return self.assistant.status()
        return {
            "service": "running",
            "plugins": self.runtime.health(),
            "uptime_seconds": round(time.time() - self.started),
            "configuration_required": not self.config.model_api.get("enabled", False),
        }

    async def _record_for_client(self, reader, recording):
        async def operation():
            clip = await self.runtime.invoke(recording, timeout=recording.duration_seconds + 35)
            result = await self.conversation.recording_reply(clip)
            return {**result, "operation_id": recording.operation_id}

        return await self._operation_for_client(reader, operation())

    async def _operation_for_client(self, reader, operation):
        work = asyncio.create_task(operation, name="requested-capability")
        disconnected = asyncio.create_task(reader.read(1), name="capability-caller")
        try:
            done, _ = await asyncio.wait((work, disconnected), return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                raise ConnectionError("Caller disconnected; operation canceled, prior effects may be partial")
            return await work
        finally:
            for task in (work, disconnected):
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(work, disconnected, return_exceptions=True)

    async def _connection(self, reader, writer):
        from assistant_runtime.api.ai_usage import current_usage
        from assistant_runtime.api.capacity import current_capacity_lease

        token = current_usage.set(None)
        lease_token = current_capacity_lease.set(None)
        try:
            await AssistantService._connection_body(self, reader, writer)
        finally:
            current_usage.reset(token)
            current_capacity_lease.reset(lease_token)
            if not writer.is_closing():
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, ConnectionError):
                    pass

    async def _connection_body(self, reader, writer):
        action = None
        try:
            d = json.loads(await asyncio.wait_for(reader.readline(), 5))
            action = d["command"]
            if action == "model-test":
                if d.get("execution_session"):
                    raise ValueError("Model testing is operator-only")
                plugin = self.runtime.plugins.get("models")
                if plugin is None:
                    raise ValueError("Models plugin unavailable")
                result = await plugin.test_model(d.get("arguments", {}))
                writer.write((json.dumps({"ok": True, "result": result}) + "\n").encode())
                await writer.drain()
                return
            if action == "configuration-action":
                if d.get("execution_session"):
                    raise ValueError("Configuration control is operator-only")
                result = await self.configuration.action(
                    d["owner"],
                    d["action"],
                    d.get("arguments", {}),
                    d.get("confirmed", False),
                    d.get("version"),
                )
                writer.write((json.dumps({"ok": True, "result": result}) + "\n").encode())
                await writer.drain()
                return
            if action == "configuration-update":
                if d.get("execution_session"):
                    raise ValueError("Configuration control is operator-only")
                result = await self.configuration.submit(
                    d["entry"],
                    d["version"],
                    d.get("resources", []),
                    preview=d.get("preview", False),
                    attempt_id=d.get("attempt_id"),
                )
                writer.write((json.dumps({"ok": True, "result": result}) + "\n").encode())
                await writer.drain()
                return
            if action in {"management-pause", "management-resume", "management-work", "management-cancel"}:
                if d.get("execution_session"):
                    raise ValueError("Lifecycle control is operator-only")
                callback = {
                    "management-pause": self.maintenance.pause,
                    "management-resume": self.maintenance.resume,
                    "management-work": self.maintenance.status,
                    "management-cancel": self.maintenance.cancel,
                }[action]
                result = callback()
                if asyncio.iscoroutine(result):
                    result = await result
                writer.write((json.dumps({"ok": True, "result": result}) + "\n").encode())
                await writer.drain()
                return
            if action == "frontend-manage":
                if d.get("execution_session"):
                    raise ValueError("Frontend management is operator-only")
                frontend = getattr(self, "http_frontends", {}).get(d.get("owner"))
                if not frontend or not frontend.management:
                    raise ValueError("Entrance management is unavailable")
                result = await frontend.management(d.get("action"), d.get("arguments", {}))
                writer.write((json.dumps({"ok": True, "result": result}) + "\n").encode())
                await writer.drain()
                return
            if action == "http-frontend":
                if d.get("execution_session"):
                    raise ValueError("HTTP routing is operator-only")
                frontend = getattr(self, "http_frontends", {}).get(d.get("owner"))
                result = frontend.endpoint if frontend else None
                writer.write((json.dumps({"ok": True, "result": result}) + "\n").encode())
                await writer.drain()
                return
            if action == "management-status":
                writer.write(
                    (
                        json.dumps(
                            {
                                "ok": True,
                                "result": {
                                    "startup_complete": self.startup_complete,
                                    "pid": os.getpid(),
                                    "supervisor_pid": int(
                                        os.environ.get("PERSONAL_AGENT_SUPERVISOR_PID", os.getpid())
                                    ),
                                    "configuration_update": self.configuration.status(),
                                    "configuration_version": os.environ.get(
                                        "PERSONAL_AGENT_CONFIGURATION_VERSION"
                                    ),
                                    "plugins": self.runtime.health(),
                                    "critical": [
                                        name
                                        for name, plugin in self.runtime.plugins.items()
                                        if plugin.manifest.critical
                                    ],
                                },
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await writer.drain()
                return
            if action == "shutdown":
                if d.get("execution_session"):
                    raise ValueError("Lifecycle control is operator-only")
                self.stop_event.set()
                writer.write(b'{"ok":true,"result":{"state":"stopping"}}\n')
                await writer.drain()
                return
            if self.intake.paused and not d.get("execution_session"):
                query = d.get("action", action)
                if query not in {
                    "status",
                    "get",
                    "list",
                    "models",
                    "events",
                    "trace",
                    "health",
                    "artifacts",
                    "answer",
                    "cancel",
                    "supplement",
                    "cancel_run",
                    "pause_run",
                }:
                    raise RuntimeError("Service is waiting to restart; new work is paused")
            if d.get("execution_session") and action != "plugin-tools":
                from assistant_runtime.api.ai_usage import UsageScope, UsageError, current_usage

                worker = self.backend.client.workers.get(d["execution_session"])
                if not worker or not worker.active_turn or not worker.meta.get("budget_id"):
                    raise UsageError("usage_inactive_execution")
                scope = await self.ai_usage.service.binding(
                    worker.meta.get("budget_scope") or worker.meta["budget_id"]
                )
                if scope is None:
                    raise UsageError("usage_unknown_execution")
                if (
                    worker.meta.get("budget_scope")
                    and d.get("caller_run") is not None
                    and d["caller_run"] != worker.meta["budget_scope"]
                ):
                    raise UsageError("usage_caller_mismatch")
                current_usage.set(
                    UsageScope(
                        scope.work_id,
                        scope.caller,
                        scope.task_id,
                        parent_call=getattr(worker, "last_usage_call", ""),
                        execution_version=worker.active_turn,
                    )
                )
                from assistant_runtime.api.capacity import current_capacity_lease

                execution = getattr(self.backend, "executions", None)
                if execution is not None:
                    ident = execution.sessions.get(d["execution_session"])
                    current_capacity_lease.set(execution.admission.slots.leases.get(ident))
                from assistant_runtime.api.conversation import current_conversation

                conversation = self.backend.conversations.get(d["execution_session"])
                current_conversation.set(conversation.session_id if conversation else None)
            if action == "plugin-tools":
                if d.get("execution_session") and d["execution_session"] not in self.backend.client.workers:
                    raise ValueError("Unknown execution session")
                result = self.extension_tools.describe()
            elif action == "plugin-invoke":
                from assistant_runtime.api import CallIdentity
                from assistant_runtime.api.ai_usage import current_usage

                if not d.get("execution_session"):
                    raise ValueError("Plugin tools require a bound execution session")
                scope = current_usage.get()
                if scope is None:
                    raise ValueError("Plugin call has no work identity")
                identity = CallIdentity(
                    uuid.uuid4().hex,
                    scope.work_id,
                    scope.caller if scope.task_id else None,
                    time.monotonic() + self._tool_timeout(),
                )
                result = await self.extension_tools.invoke(
                    d["name"], d.get("arguments", {}), identity=identity, timeout=self._tool_timeout()
                )
            elif action == "wake":
                if self.assistant is None:
                    raise ValueError("Assistant interaction is not enabled")
                await self.assistant.wake(source="cli")
            elif action in {"sleep", "stop", "cancel", "volume", "progress"}:
                await self.runtime.invoke(Control(action, d.get("value")))
            elif action == "gesture":
                await self.runtime.plugins["gesture"].simulate(d.get("value", "wave"))
            elif action == "devices":
                operation = {"operation_id": d["operation_id"]} if d.get("operation_id") else {}
                if d.get("action") == "snapshot":
                    capture = VideoCapture(d.get("device_id"), **operation)
                    frame = await self._operation_for_client(reader, self.runtime.invoke(capture))
                    result = {
                        **await self.conversation.capture_reply(frame),
                        "operation_id": capture.operation_id,
                    }
                elif d.get("action") == "record":
                    recording = VideoRecord(d.get("device_id"), d.get("duration_seconds", 10), **operation)
                    result = await self._record_for_client(reader, recording)
                elif d.get("action") in {"move", "stop-camera"}:
                    stopping = d["action"] == "stop-camera"
                    result = await self._operation_for_client(
                        reader,
                        self.runtime.invoke(
                            CameraMotion(
                                d.get("device_id"),
                                "stop" if stopping else d.get("direction"),
                                0 if stopping else d.get("duration_ms", 80),
                                **operation,
                            )
                        ),
                    )
                elif d.get("action") == "volume":
                    from assistant_runtime.api.contracts import AudioVolume

                    result = await self._operation_for_client(
                        reader,
                        self.runtime.invoke(AudioVolume(d.get("device_id"), d.get("percent"), **operation)),
                    )
                elif d.get("action") in {"operations", "cancel-operation"}:
                    from assistant_runtime.api.contracts import DeviceOperationQuery, DeviceOperationCancel

                    request_type = (
                        DeviceOperationCancel if d["action"] == "cancel-operation" else DeviceOperationQuery
                    )
                    result = await self.runtime.invoke(
                        request_type(d.get("device_id"), d.get("operation_id"))
                    )
                else:
                    result = await self.runtime.invoke(
                        DeviceQuery(d.get("action", "list"), d.get("device_id"))
                    )
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "search":
                from assistant_runtime.api.contracts import SearchRequest

                result = await self._operation_for_client(
                    reader,
                    self.runtime.invoke(
                        SearchRequest(d["action"], d.get("arguments", {}), d.get("caller_run")), timeout=65
                    ),
                )
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "media":
                from assistant_runtime.api.contracts import MediaRequest

                result = await self.runtime.invoke(
                    MediaRequest(d["action"], d.get("arguments", {}), d.get("caller_run")), timeout=90
                )
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "ai-usage":
                from assistant_runtime.api.ai_usage import UsageRequest

                result = await self.runtime.invoke(
                    UsageRequest(d["action"], d.get("arguments", {}), d.get("caller_run"))
                )
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "models":
                from assistant_runtime.api.models import ModelRequest

                result = await self.runtime.invoke(
                    ModelRequest(d["action"], d.get("arguments", {}), d.get("caller_run"))
                )
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "voice-decision":
                from assistant_runtime.api.contracts import VoiceDecisionRequest

                result = await self.runtime.invoke(
                    VoiceDecisionRequest(d["action"], d.get("arguments", {}), d.get("caller_run"))
                )
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "decisions":
                from assistant_runtime.api.contracts import DecisionRequest, DecisionQuery, DecisionOutcome

                request = (
                    DecisionOutcome(**d.get("arguments", {}))
                    if d["action"] == "outcome"
                    else DecisionQuery(**d.get("arguments", {}), caller_run=d.get("caller_run"))
                    if d["action"] == "judge"
                    else DecisionRequest(d["action"], d.get("arguments", {}))
                )
                result = await self.runtime.invoke(request)
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "events":
                from assistant_runtime.api.contracts import EventRequest

                result = await self.runtime.invoke(
                    EventRequest(d["action"], d.get("arguments", {}), d.get("caller_run")), timeout=15
                )
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "notifications":
                from assistant_runtime.api.contracts import NotificationRequest

                result = await self.runtime.invoke(
                    NotificationRequest(d["action"], d.get("arguments", {}), d.get("caller_run")), timeout=15
                )
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action in {"executions", "files"}:
                from assistant_runtime.api.runs import ExecutionRequest
                from assistant_runtime.api.files import FileRequest

                contract = ExecutionRequest if action == "executions" else FileRequest
                result = await self.runtime.invoke(contract(d["action"], d.get("arguments", {})), timeout=90)
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                return
            elif action == "tasks":
                from assistant_runtime.api.contracts import TaskRequest

                result = await self.runtime.invoke(
                    TaskRequest(d["action"], d.get("arguments", {}), d.get("caller_run")), timeout=90
                )
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "mcp":
                from assistant_runtime.api.contracts import IntegrationRequest

                result = await self._operation_for_client(
                    reader,
                    self.runtime.invoke(
                        IntegrationRequest(d["action"], d.get("arguments", {}), d.get("caller_run")),
                        timeout=3660,
                    ),
                )
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "memory":
                result = await self.runtime.invoke(
                    MemoryRequest(d["action"], d.get("arguments", {})), timeout=90
                )
                response = {"ok": True, "result": result}
                writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            elif action == "conversation":
                if d.get("execution_session") or "cli" not in self.runtime.plugins:
                    raise ValueError("CLI conversation entrance is not enabled for this caller")
                from assistant_runtime.api.conversation import ConversationRequest

                result = await self.runtime.invoke(ConversationRequest(d["action"], d.get("arguments", {})))
                writer.write((json.dumps({"ok": True, "result": result}, ensure_ascii=False) + "\n").encode())
                await writer.drain()
                return
            elif action == "text":
                if "conversation" not in self.runtime.plugins:
                    raise ValueError("Conversation is not enabled")
                result = await self.conversation.submit(
                    d.get("request_id") or str(uuid.uuid4()), d["text"], []
                )
                response = {"ok": True, "result": result}
            elif action in {"say", "inject-wav"}:
                if self.assistant is None:
                    raise ValueError("Assistant interaction is not enabled")
                await self.assistant.wake(source="cli:" + action)
                if self.assistant.connect_task:
                    await asyncio.shield(self.assistant.connect_task)
                if self.assistant.mode != "active":
                    raise RuntimeError(self.assistant.last_error or "Voice not connected")
                if action == "say":
                    await self.assistant.speak(d["text"])
                elif action == "text":
                    await self.assistant.voice.send_text(d["text"])
                else:
                    if not self.no_mic:
                        raise ValueError(
                            "Inject tests require run --no-mic to avoid mixing live microphone audio"
                        )
                    import miniaudio

                    a = await asyncio.to_thread(
                        miniaudio.decode_file,
                        d["path"],
                        output_format=miniaudio.SampleFormat.SIGNED16,
                        nchannels=1,
                        sample_rate=16000,
                    )
                    pcm = a.samples.tobytes()
                    if len(pcm) > 60 * 32000:
                        raise ValueError("Test WAV must be at most 60 seconds")
                    for offset in range(0, len(pcm), 640):
                        self.assistant.voice.feed(pcm[offset : offset + 640])
                        self.assistant.last_activity = time.monotonic()
                        await asyncio.sleep(0.02)
            elif action == "history" and "conversation" not in self.runtime.plugins:
                raise ValueError("Conversation history is not enabled; query executions instead")
            elif action not in {"status", "history"}:
                raise ValueError("Unknown command")
            if action not in {"plugin-tools", "plugin-invoke"}:
                result = self.store.executions.recent() if action == "history" else self.status()
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)[:200]}
            from assistant_runtime.api.tool_calls import ToolFailure
            from assistant_runtime.api.models import ModelCallError

            if isinstance(exc, ModelCallError):
                response["model_failure"] = exc.failure
                response["error_code"] = exc.code
            if isinstance(exc, ToolFailure):
                response["tool_outcome"] = exc.outcome.public()
                if exc.result is not None:
                    try:
                        json.dumps(exc.result)
                    except (TypeError, ValueError):
                        pass
                    else:
                        response["tool_result"] = exc.result
            if action in {"configuration-update", "configuration-action", "model-test"}:
                from assistant_runtime.api.configuration import ConfigurationError

                response = {"ok": False, "error": "Configuration operation failed"}
                if isinstance(exc, ConfigurationError):
                    response["issue"] = exc.public()
        try:
            writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def run(self):
        server = None
        waiters = []
        try:
            from assistant_runtime.platforms.ipc import serve

            server = await serve(self.config.socket, self._connection)
            system.stop_signals(self.stop_event.set)
            await self.runtime.start()
            self.startup_complete = not self.runtime.fatal.is_set()
            if self.startup_complete and all(
                item["state"] == "ready" for item in self.runtime.health().values()
            ):
                from .release_state import successful

                successful(
                    self.config.root,
                    {name: plugin.manifest.version for name, plugin in self.runtime.plugins.items()},
                )
            if getattr(self, "backend", None) and self.backend.session_id:
                self.store.set("session_id", self.backend.session_id)
            waiters = [
                asyncio.create_task(self.stop_event.wait()),
                asyncio.create_task(self.runtime.fatal.wait()),
            ]
            log.info("service_ready backend=pi microphone=%s standby", not self.no_mic)
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if self.runtime.fatal.is_set():
                raise RuntimeError("Critical plugin failed; supervisor will restart without replay")
        finally:
            if server:
                server.close()
                await server.wait_closed()
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            try:
                if self.assistant is not None:
                    await self.assistant.sleep()
            finally:
                await self.configuration.close()
                await self.runtime.close()
                from assistant_runtime.platforms.ipc import cleanup

                cleanup(self.config.socket)
                self.store.db.close()
                self.lock_file.close()
