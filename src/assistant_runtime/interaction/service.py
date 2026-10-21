"""Command entrance and structured feedback; execution stays in the tasks capability."""

import asyncio
import json
import time
import uuid

from ..assistant.commands import parse_command, normalize
from ..kernel.contracts import DecisionQuery, TaskRequest, Control, NotificationRequest
from ..kernel.decisions import decide
from ..kernel.speech import FeedbackIntent
from .state import InteractionState


class Interaction:
    def __init__(self, host, settings, recognizer, output):
        self.host, self.settings, self.recognizer, self.output = host, settings, recognizer, output
        self.state = InteractionState(settings.context_seconds)
        self.ready = False
        self.error = None
        self.path = "command"
        self.partial = ""
        self.first_sequence = None
        self.last_sequence = 0
        self.dispatch_lock = asyncio.Lock()
        self.jobs = set()
        self.pending = None
        self.operation = None
        self.input_revision = 0
        self.utterance_started = None
        self.input_blocked = False

    def spawn(self, coroutine, name):
        started = False

        async def guarded():
            nonlocal started
            started = True
            try:
                return await coroutine
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = name + ":" + type(exc).__name__

        task = self.host.context.spawn(guarded(), name)
        self.jobs.add(task)
        task.add_done_callback(self.jobs.discard)
        task.add_done_callback(lambda _: coroutine.close() if not started else None)
        return task

    async def prepare(self):
        try:
            await self.host.runtime.ready.wait()
            await self.recognizer.reset()
            # One local warmup; no recurring cloud requests or stored audio.
            await self.output.renderer.render("准备好了。")
            self.ready = True
        except Exception as exc:
            self.error = type(exc).__name__

    async def begin(self, chunks):
        self.path = "command"
        self.state.attending = True
        self.input_blocked = False
        self.partial = ""
        self.utterance_started = None
        self.first_sequence = chunks[0].sequence if chunks else None
        self.last_sequence = 0
        await self.recognizer.reset()
        for chunk in chunks:
            await self.accept(chunk)

    async def accept(self, chunk):
        if self.input_blocked:
            return
        if chunk.sequence <= self.last_sequence:
            return
        self.last_sequence = chunk.sequence
        if self.first_sequence is None:
            self.first_sequence = chunk.sequence
        source = getattr(self.host, "input_buffer", None)
        gaps = source.gaps if source else 0
        try:
            result = await self.recognizer.accept(chunk)
        except Exception as exc:
            self.error = type(exc).__name__
            await self.gap()
            return
        if source and source.gaps != gaps:
            await self.gap()
            return
        if self.path != "command" or not self.state.attending:
            return
        if result.text and result.text != self.partial:
            if not self.partial:
                self.utterance_started = chunk.started
                self.input_revision += 1
                await self.output.interrupt()
                self.host.player.manual_muted = False
                self.host.dialogue.user()
            self.partial = result.text
        if self.utterance_started is not None and chunk.ended - self.utterance_started > 25:
            await self.gap()
            return
        if not result.final:
            return
        text = result.text
        for word in sorted(self.host.config.wake_words, key=len, reverse=True):
            if text.startswith(word):
                text = text[len(word) :].lstrip("，,。！! ")
                break
        sequence = self.first_sequence
        self.partial = ""
        self.utterance_started = None
        self.first_sequence = chunk.sequence + 1
        await self.recognizer.reset()
        if not text:
            return
        self.host.last_input = text
        self.host.input_count += 1
        self.host.dialogue.user()
        previous = self.state.context(self.operation)
        request = self.state.begin(text)
        request.stages["input_revision"] = self.input_revision
        request.stages["input_ended"] = result.speech_ended_at
        request.stages["endpoint_observed"] = chunk.ended
        request.stages["transcript_stable"] = time.monotonic()
        self.host.store.transcript("user", text)
        self.host.conversation.store.put(
            "command:" + request.ref.id,
            self.host.backend.session_id,
            role="user",
            source="voice",
            text=text,
            status="complete",
            kind="message",
        )
        # Pending classification may be cancelled. Never cancel an uncertain task
        # submission and then submit the same request via another path.
        self.pending = self.spawn(self.route(request, previous, sequence), "voice-route")

    async def feedback(self, request, kind, text, *, late=False):
        if not text:
            if kind == "completed":
                kind = "unknown"
            text = {"failed": "这次操作失败了。", "unknown": "还不能确认操作结果，我不会重复执行。"}.get(
                kind, "操作已结束。"
            )
        if len(text) > 240:
            text = {
                "failed": "这次操作失败了，详细原因比较长，需要我展开说吗？",
                "unknown": "操作结果还不能确认，我没有重复执行。需要我说明详细情况吗？",
            }.get(kind, "这项操作返回了较长结果，需要我展开说吗？")
        intent = FeedbackIntent(
            request.ref,
            kind,
            text[:240],
            time.monotonic() + 30,
            important=kind in {"failed", "unknown", "clarification"},
        )
        if not late and (not self.state.attending or not self.state.valid(request.ref)):
            return "deferred"
        input_revision = self.input_revision
        if not late and request.stages.get("input_revision", input_revision) != input_revision:
            return "deferred"
        if late and (self.host.active or self.host.quiet_active()):
            return "deferred"
        result = await self.output.say(
            intent,
            lambda: (
                not self.host.active
                if late
                else self.state.attending
                and self.state.valid(request.ref)
                and self.input_revision == input_revision
            ),
            on_started=lambda at: request.stages.setdefault("first_feedback_written", at),
        )
        if result == "completed":
            request.stages["output_completed"] = time.monotonic()
            self.host.dialogue.replied()
        elif result not in {"deferred", "duplicate", "omitted"}:
            self.error = "speech_output_" + result
        return result

    async def route(self, request, previous, sequence):
        ref = request.ref
        try:
            if (
                not self.state.valid(ref)
                or not self.state.attending
                or request.stages.get("input_revision", self.input_revision) != self.input_revision
            ):
                return
            cmd = parse_command(request.text, self.host.config.wake_words)
            if normalize(request.text) in {"我自己来", "我来操作", "我自己操作"}:
                cmd = Control("cancel")
            if cmd and cmd.kind != "chat":
                if cmd.kind == "cancel":
                    async with self.dispatch_lock:
                        if not self.current(request):
                            return
                        stopped = await self.stop_operation()
                    await self.feedback(
                        request,
                        "completed",
                        "已停止后续操作；已经发出的动作可能已生效。" if stopped else "当前没有关联的操作。",
                    )
                else:
                    if cmd.kind == "progress" and self.operation:
                        await self.feedback(request, "progress", "当前操作状态：" + self.operation.state)
                        return
                    await self.host.control(cmd)
                return
            choice = (
                "dialogue"
                if cmd and cmd.kind == "chat"
                else ("correction" if previous.get("state") == "clarifying" else "command")
            )
            if not cmd:
                try:
                    async with asyncio.timeout(self.settings.routing_seconds):
                        answer = await decide(
                            self.host.runtime,
                            DecisionQuery(
                                "selection",
                                "voice-route:" + ref.id,
                                {
                                    "question": "判断当前用户话语应走哪条路径。对待执行操作的补充或改口选 correction；目标不明选 uncertain。不要把引用或假设当作命令。",
                                    "context": {"utterance": request.text, "recent_operation": previous},
                                    "options": {
                                        "command": "要求操作设备、查询或执行任务",
                                        "dialogue": "自然聊天、讨论或知识问答",
                                        "correction": "补充或纠正最近操作",
                                    },
                                },
                            ),
                        )
                    if answer["status"] == "ok":
                        choice = answer["choice"]
                except TimeoutError:
                    pass  # bounded fallback: executor understands and clarifies, never guessed device args
            if (
                not self.state.valid(ref)
                or not self.state.attending
                or request.stages.get("input_revision", self.input_revision) != self.input_revision
            ):
                return
            self.state.mark(ref, "intent_determined")
            if choice == "uncertain":
                self.state.mark(ref, "clarifying")
                await self.feedback(request, "clarification", "你想让我操作什么，还是一起聊聊？")
                return
            if choice == "dialogue":
                self.path = "dialogue"
                # Replay the bounded original utterance once. Do not also submit
                # its transcription: native dialogue owns any subsequent delegation.
                chunks = tuple(
                    c for c in self.host.input_buffer.since(sequence) if c.sequence <= self.last_sequence
                )
                self.host.mode = "connecting"
                self.host.native_pending.clear()
                self.host.connect_task = self.spawn(
                    self.host._connect(tuple(c.pcm for c in chunks)), "dialogue-connect"
                )
                self.host.dialogue.continuous = bool(cmd and cmd.kind == "chat")
                return
            async with self.dispatch_lock:
                if not self.current(request):
                    return
                if choice == "correction" and self.operation:
                    request.ref = type(ref)(self.operation.ref.id, self.operation.ref.revision + 1)
                    ref = request.ref
                    if self.operation.state == "clarifying" and self.operation.run_id:
                        request.task_id, request.run_id = self.operation.task_id, self.operation.run_id
                        await self.host.runtime.invoke(
                            TaskRequest("supplement", {"run_id": request.run_id, "text": request.text}),
                            timeout=15,
                        )
                        self.operation = request
                        self.state.mark(ref, "executing")
                        acknowledgement = self.spawn(self.acknowledge(request), "command-ack")
                        self.spawn(self.watch(request, acknowledgement), "command-result")
                        return
                    # Stop the scoped old execution before replacement, never steer
                    # an unrelated foreground/chat task. Unknown stop => no new action.
                    await self.stop_operation()
                    if not self.current(request):
                        return
                request.stages["dispatch_started"] = time.monotonic()
                if len(self.jobs) >= 32:
                    await self.feedback(
                        request, "clarification", "当前等待结果的操作较多，请先完成或取消其中一项。"
                    )
                    return
                acknowledgement = self.spawn(self.acknowledge(request), "command-ack")
                self.operation = request
                prompt = (
                    "执行这次语音请求，先确认目标与参数；指代不清或授权不足必须提问，不猜测操作。"
                    "这是一次用户请求，不创建重复任务。以工具证据核验结果，失败和未知如实说明。"
                    "最终用一两句话给出结果，保留否定和关键对象，不重复叙述过程。\n"
                    + json.dumps({"request": request.text, "recent_operation": previous}, ensure_ascii=False)
                )
                try:
                    definition = await self.host.runtime.invoke(
                        TaskRequest(
                            "create",
                            {
                                "request_id": request.ref.id
                                if request.ref.revision == 1
                                else str(uuid.uuid5(uuid.UUID(request.ref.id), str(request.ref.revision))),
                                "spec": {
                                    "title": request.text[:100],
                                    "prompt": prompt[:4000],
                                    "notify_voice": False,
                                    "notify_completion": True,
                                },
                            },
                        ),
                        timeout=30,
                    )
                    request.task_id = definition["id"]
                    self.state.mark(ref, "executing")
                    self.spawn(self.watch(request, acknowledgement), "command-result")
                except BaseException:
                    acknowledgement.cancel()
                    raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = type(exc).__name__
            self.state.mark(request.ref, "unknown")
            await self.feedback(request, "unknown", "这次请求的状态还不能确认，我不会重复执行。")

    async def acknowledge(self, request):
        await asyncio.sleep(self.settings.acknowledgement_seconds)
        if request.state in {"completed", "failed", "unknown", "clarifying"}:
            return
        text = (
            "我来处理“" + request.text + "”。"
            if len(request.text) <= 65
            else "请求已收到，我先核对具体操作。"
        )
        await self.feedback(request, "accepted", text)

    def current(self, request):
        return (
            self.state.attending
            and self.state.valid(request.ref)
            and request.stages.get("input_revision", self.input_revision) == self.input_revision
        )

    async def stop_operation(self):
        operation = self.operation
        if not operation:
            return False
        if not operation.task_id:
            raise RuntimeError("Prior submission is unknown; cannot safely replace it")
        page = await self.host.runtime.invoke(TaskRequest("get", {"task_id": operation.task_id}))
        runs = page.get("active_runs", [])
        if len(runs) != 1:
            raise RuntimeError("No unique related execution to stop")
        await self.host.runtime.invoke(TaskRequest("cancel_run", {"run_id": runs[0]["id"]}), timeout=15)
        page = await self.host.runtime.invoke(TaskRequest("get", {"task_id": operation.task_id}))
        if any(r["state"] in {"running", "dispatching", "unknown"} for r in page["runs"]):
            raise RuntimeError("In-flight execution could not be confirmed stopped")
        operation.state = "canceled"
        self.operation = None
        return True

    async def watch(self, request, acknowledgement):
        try:
            while True:
                if request.state == "canceled":
                    return
                page = await self.host.runtime.invoke(TaskRequest("get", {"task_id": request.task_id}))
                runs = page["runs"]
                if runs:
                    run = runs[0]
                    request.run_id = run["id"]
                    status = run["state"]
                    if status in {
                        "completed",
                        "failed",
                        "unknown",
                        "canceled",
                        "paused",
                        "waiting_user",
                        "missed",
                    }:
                        acknowledgement.cancel()
                        kind = "clarification" if status == "waiting_user" else status
                        if status == "missed":
                            kind = "failed"
                        request.state = "clarifying" if kind == "clarification" else kind
                        request.updated = time.monotonic()
                        request.stages["execution_result"] = time.monotonic()
                        data = run["data"]
                        text = data.get("result", "")
                        if status != "completed":
                            text = text or data.get("progress", "")
                        if status == "missed":
                            text = "这次操作没有启动，已跳过。"
                        if status == "waiting_user":
                            questions = data.get("questions", [])
                            if questions:
                                text = "；".join(
                                    q.get("question", "") for q in questions if isinstance(q, dict)
                                )
                            text = "这项操作需要你补充信息：" + (str(text)[:150] or "请说明具体目标。")
                        late = not self.state.attending
                        if status not in {"canceled", "paused"}:
                            delivery = await self.feedback(request, kind, str(text), late=late)
                            if delivery == "deferred" and (late or not self.state.valid(request.ref)):
                                # Preserve important late results through the existing
                                # durable outbox; no fresh full dialogue is required.
                                await self.host.runtime.invoke(
                                    NotificationRequest(
                                        "publish",
                                        {
                                            "event_key": f"voice-result:{request.ref.id}:{request.ref.revision}",
                                            "title": request.text[:100],
                                            "body": str(text)[:2000] or "执行状态需要核对。",
                                            "voice": True,
                                            "priority": "important"
                                            if kind in {"failed", "unknown", "clarification"}
                                            else "normal",
                                        },
                                    )
                                )
                        return
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            acknowledgement.cancel()
            self.error = type(exc).__name__
            request.state = "unknown"
            await self.feedback(request, "unknown", "暂时无法核对执行结果，任务不会自动重做。")

    async def gap(self):
        self.error = "input_gap"
        self.input_blocked = True  # A tail after missing audio is not a new request.
        self.partial = ""
        self.utterance_started = None
        self.first_sequence = None
        try:
            await self.recognizer.reset()
        except Exception:
            self.ready = False
        request = self.state.begin("")
        self.state.mark(request.ref, "clarifying")
        self.host.dialogue.replied()
        self.spawn(
            self.feedback(request, "clarification", "刚才收音中断了，请重新唤醒后再说一次。"), "input-gap"
        )

    async def end_attention(self):
        self.state.attending = False
        await self.output.interrupt()

    async def close(self):
        await self.end_attention()
        for task in tuple(self.jobs):
            task.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        await self.recognizer.close()
        await self.output.close()
