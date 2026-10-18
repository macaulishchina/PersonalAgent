"""Real subscription speech of API task results, without a physical audio device."""

import asyncio
import json
import time
import uuid
from types import MethodType, SimpleNamespace

import numpy as np

from assistant_runtime.assistant.service import AssistantService
from assistant_runtime.kernel.contracts import TaskRequest
from assistant_runtime.mcp.client import LocalRuntimeClient
from assistant_runtime.plugins.codex.native import NativeVoice
from scripts.prepare_model_gateway import private_write


async def voice_active(host, output):
    """Native execution request with live subscription voice → API MCP → spoken result."""
    from assistant_runtime.plugins.codex.backend import CodexBackend
    from assistant_runtime.conversation.service import Conversation
    from scripts.verify_model_workflows import wait_for

    original_backend, original_conversation = host.backend, host.conversation
    servers = await original_backend.integrations.capture("foreground", cwd=host.config.workspace)
    for name, descriptor in servers.items():
        if name != "models":
            descriptor["enabled"] = False
    response = await host.client.request(
        "thread/start",
        {
            "model": "gpt-6-luna",
            "cwd": host.config.workspace,
            "sandbox": host.config.codex_sandbox,
            "approvalPolicy": host.config.approval_policy,
            "baseInstructions": "Synthetic coding voice handoff acceptance. Use only the configured models MCP for the exact request. No shell, files, network, apps or tasks. Never answer from memory. On tool failure report failure, without retrying or changing models.",
            "config": {"mcp_servers": servers, "web_search": "disabled", "model_reasoning_effort": "low"},
        },
    )
    thread = response["thread"]["id"]
    host.foreground_threads.append(thread)

    async def noop(*args):
        pass

    # The host's event dispatcher already owns the real subscription RPC stream.
    # Reuse it; the real foreground backend consumes only its unmatched events.
    proxy = SimpleNamespace(
        request=host.client.request,
        send=host.client.send,
        respond=host.client.respond,
        events=host.foreground_events,
        process=host.client.process,
        close=noop,
    )
    backend = CodexBackend(host.config, host.store, noop, noop)
    backend.client = proxy
    backend.integrations = None
    backend.thread_id = thread
    backend.session_id = host.store.logical_session("codex", thread)
    host.backend = backend
    transcripts, frames, owned = [], [], []

    async def notice(event):
        if event.get("kind") == "transcript":
            transcripts.append(event)
            if event.get("role") == "user":
                backend.last_user_text = event["text"]

    def feed(pcm):
        if np.max(np.abs(np.frombuffer(pcm, dtype="<i2").astype("int32")), initial=0) > 150:
            frames.append(time.monotonic())

    host.interaction, host.memory = None, None
    host.active, host.mode = True, "active"
    host.quiet_active = lambda: False
    host.player = SimpleNamespace(playing=False, manual_muted=False)
    host.voice = NativeVoice(host.config, backend, feed, notice)
    backend.on_realtime = host.voice.notification
    backend.on_execution_started = host.voice.execution_started
    conversation = Conversation(host)
    host.conversation = conversation

    def spawn(coroutine, label):
        task = asyncio.create_task(coroutine)
        owned.append(task)
        return task

    await conversation.start(SimpleNamespace(spawn=spawn))
    backend.events_task = asyncio.create_task(backend._events())
    evidence = []
    try:
        await host.voice.open()
        for kind, model in (("success", "go-anthropic"), ("failure", "unregistered-test-model")):
            ident = str(uuid.uuid4())
            offset, prior_frames = len(transcripts), len(frames)
            request = {
                "request_id": ident,
                "model": model,
                "text": "Synthetic coding check: evaluate Python 17+25, reply only 42.",
            }
            await backend.submit(
                "现在做一次编程工具测试。实际调用 models MCP 的 infer_with_model，参数为 "
                + json.dumps(request)
                + "。只调用一次，不重试、不换模型。成功就简短说计算结果，工具报错就简短说失败且没有结果，不能自己计算或猜测。"
            )

            def completed():
                rows = [r for r in backend.turns.records.values() if r.status]
                speech = [r["text"] for r in transcripts[offset:] if r.get("role") == "assistant"]
                if kind == "success":
                    return speech if rows and any("42" in s or "四十二" in s for s in speech) else None
                return (
                    speech
                    if len(rows) >= 2 and any("失败" in s or "没有结果" in s or "未注册" in s for s in speech)
                    else None
                )

            spoken = await wait_for(completed, timeout=100)
            rows = host.store.db.execute(
                "SELECT state,model FROM model_calls WHERE id=?", ("foreground:" + ident,)
            ).fetchall()
            if (kind == "success" and rows != [("completed", model)]) or (kind == "failure" and rows):
                raise RuntimeError(
                    "Voice inference receipt missing, duplicated, or dispatched after validation failure"
                )
            if len(frames) <= prior_frames:
                raise RuntimeError("Voice reply transcript arrived without audio")
            evidence.append(
                {
                    "kind": kind,
                    "spoken": spoken,
                    "inference_receipts": rows,
                    "audible_frames": len(frames) - prior_frames,
                }
            )
        old_epoch = host.voice.epoch
        await host.voice.close()
        before = len(frames)
        host.voice.deliver_audio(bytes([255, 127]) * 480, old_epoch)
        await asyncio.sleep(1)
        if len(frames) != before:
            raise RuntimeError("Exited voice played late audio")
        return {
            "passed": True,
            "cases": evidence,
            "physical_audio": False,
            "entry": "synthetic execution request with live subscription voice",
            "late_audio_rejected": True,
        }
    finally:
        await host.voice.close()
        for task in owned:
            task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)
        private_write(
            output / "voice-active.json",
            json.dumps(
                {
                    "cases": evidence,
                    "transcripts": transcripts,
                    "turns": [
                        {"id": key, "status": r.status, "result": r.result}
                        for key, r in backend.turns.records.items()
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        await backend.close()
        host.backend, host.conversation = original_backend, original_conversation


async def voice(host, output):
    from scripts.verify_model_workflows import create, finished, wait_for

    inherited = (
        await host.client.request("config/read", {"cwd": host.config.workspace, "includeLayers": False})
    )["config"].get("mcp_servers", {})
    reply = await host.client.request(
        "thread/start",
        {
            "model": "gpt-6-luna",
            "cwd": host.config.workspace,
            "sandbox": host.config.codex_sandbox,
            "approvalPolicy": host.config.approval_policy,
            "baseInstructions": "Synthetic voice transport acceptance. Never use tools or execute actions. Only read the supplied execution results; preserve failure and negation.",
            "config": {"mcp_servers": {k: {"enabled": False} for k in inherited}, "web_search": "disabled"},
        },
    )
    thread = reply["thread"]["id"]
    host.foreground_threads.append(thread)
    backend = SimpleNamespace(
        client=host.client, thread_id=thread, submit_lock=asyncio.Lock(), integrations=None
    )
    transcripts, notices, audio_times, unexpected = [], [], [], []

    async def notice(event):
        notices.append(event)
        if event.get("kind") == "transcript" and event.get("role") == "assistant":
            transcripts.append(event["text"])

    async def stop():
        pass

    def feed(pcm):
        if np.max(np.abs(np.frombuffer(pcm, dtype="<i2").astype("int32")), initial=0) > 150:
            audio_times.append(time.monotonic())

    host.interaction = None
    host.active = False
    host.mode = "standby"
    host.notification_output = False
    host.last_output = None
    host.last_activity = time.monotonic()
    host.quiet_active = lambda: False
    host.transition = asyncio.Lock()
    host.player = SimpleNamespace(manual_muted=False, playing=False, feed=feed, stop=stop)
    host.voice = NativeVoice(host.config, backend, MethodType(AssistantService.audio_received, host), notice)
    host.voice.on_conversation = lambda event, epoch: None
    host.notify_task = MethodType(AssistantService.notify_task, host)

    async def events():
        while True:
            event = await host.foreground_events.get()
            method, params = event.get("method", ""), event.get("params", {})
            if params.get("threadId") != thread:
                continue
            if method.startswith("thread/realtime/"):
                await host.voice.notification(method, params)
            if method == "turn/started":
                unexpected.append(event)
                await host.client.request(
                    "turn/interrupt", {"threadId": thread, "turnId": params["turn"]["id"]}
                )
            if "id" in event:
                await host.client.send(
                    {
                        "id": event["id"],
                        "error": {"code": -32601, "message": "No actions in output-only acceptance"},
                    }
                )

    pump = asyncio.create_task(events())
    receipts = []
    try:
        for kind in ("success", "failure"):
            offset, audio_offset = len(transcripts), len(audio_times)
            prompt = (
                "Synthetic coding test. Run exactly one shell command python -c 'print(17+25)' and nothing else. "
                "After verifying the actual result, reply only in Chinese: 计算结果是四十二。"
                if kind == "success"
                else "Synthetic coding failure test. Run exactly one shell command python -c 'raise RuntimeError(\"synthetic failure\")' and nothing else. "
                "Do not retry or fix the deliberate failure. Report the actual failure, only in Chinese: 测试执行失败，没有计算结果。"
            )
            task = await create(
                LocalRuntimeClient(host.config.socket),
                "go-anthropic",
                "语音验收",
                prompt,
                notify_completion=True,
                notify_voice=True,
            )
            ended = await wait_for(lambda: finished(host, task["id"]))

            def delivered():
                row = host.store.db.execute(
                    "SELECT n.id,d.state,d.attempts FROM notifications n JOIN notification_deliveries d ON n.id=d.notification_id "
                    "WHERE n.event_key LIKE ? AND d.channel='voice'",
                    ("task-result:" + ended["id"] + ":%",),
                ).fetchone()
                return row if row and row[1] in {"delivered", "unknown", "failed"} else None

            delivery = await wait_for(delivered, timeout=75)
            spoken = "\n".join(transcripts[offset:])
            if delivery[1] != "delivered" or delivery[2] != 1 or len(audio_times) <= audio_offset:
                raise RuntimeError(
                    "Voice delivery did not produce confirmed audio exactly once: " + str(delivery[1:])
                )
            if (kind == "success" and not ("四十二" in spoken or "42" in spoken)) or (
                kind == "failure" and not any(word in spoken for word in ("失败", "没有计算结果"))
            ):
                raise RuntimeError(
                    "Actual speech transcript did not preserve the execution result: " + spoken[:160]
                )
            trace = await LocalRuntimeClient(host.config.socket).invoke(
                TaskRequest("trace", {"run_id": ended["id"]})
            )
            commands = [
                i for i in trace["items"] if i["phase"] == "completed" and i.get("kind") == "commandExecution"
            ]
            # Native traces, rather than the spoken claim, establish the single action.
            if len(commands) != 1:
                raise RuntimeError("Synthetic task did not execute exactly one command")
            old_epoch = host.voice.epoch
            before = len(audio_times)
            host.voice.deliver_audio(bytes([255, 127]) * 480, old_epoch - 1)
            await asyncio.sleep(1)
            if len(audio_times) != before or host.voice.opened or host.notification_output:
                raise RuntimeError("Closed voice connection admitted late output")
            receipts.append(
                {
                    "kind": kind,
                    "run_id": ended["id"],
                    "result": ended["data"].get("result"),
                    "delivery": delivery,
                    "spoken": spoken,
                    "audible_frames": len(audio_times) - audio_offset,
                    "late_audio_rejected": True,
                    "command_count": len(commands),
                }
            )
        if unexpected:
            raise RuntimeError("Voice notification initiated an unexpected execution")
        return {"passed": True, "cases": receipts, "physical_audio": False, "subscription_voice": True}
    finally:
        await host.voice.close()
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        private_write(
            output / "voice.json",
            json.dumps(
                {
                    "receipts": receipts,
                    "transcripts": transcripts,
                    "notices": notices,
                    "unexpected_execution": unexpected,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
