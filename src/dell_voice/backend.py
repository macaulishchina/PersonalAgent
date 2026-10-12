import asyncio
import json
import logging
import re
import uuid
from typing import Protocol

from .rpc import CodexClient, RpcError

log = logging.getLogger(__name__)

TOOL = {
    "type": "function", "name": "audio_control", "description": "Control this assistant's room speaker, volume, spoken output or voice session.",
    "inputSchema": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["room", "volume", "stop", "sleep"]},
        "room": {"type": "string", "enum": ["living", "study"]},
        "volume": {"type": "integer", "minimum": 0, "maximum": 100}}, "required": ["action"], "additionalProperties": False},
}
INSTRUCTIONS = """你是部署在用户 DELL 上的个人语音助手。默认中文，回答简洁自然，通常一到三句话。
你接收的是语音识别的文字。沿用已有项目上下文并实际完成用户交办的文件和项目工作；不要把建议当作完成。
较长任务给出简短进展。避免朗读代码、长路径和大段列表，把细节保存在项目中并概括结果。
音箱控制使用 audio_control。living 是客厅，study 是书房。不要用 shell 修改系统声音或杀掉本语音服务。
除非用户明确要求，否则不要主动设置音量；低于配置下限时的自动提高由本地音频层处理，较高音量保持原值。
普通补充消息不会取消原任务；只有明确取消才停止任务。用户只是要求停播时，工作仍应继续。
用户表达结束这次交流（如先这样、回头再聊、我先去忙了）时，调用 audio_control 的 sleep 返回待机，不取消后台任务，也无需追加告别播报。区分真正的结束意图与引用、否定、讨论该说法，或单纯的“谢谢”“好的”。
原始音频默认不留存。你没有持续视频输入。不要主动启用摄像头。
"""


class TaskBackend(Protocol):
    async def start(self): ...
    async def submit(self, text: str): ...
    async def cancel(self): ...
    async def close(self): ...


class CodexBackend:
    def __init__(self, config, store, on_speech, on_control):
        self.config, self.store = config, store
        self.on_speech, self.on_control = on_speech, on_control
        self.client = CodexClient(config.codex_binary, config.workspace)
        self.thread_id = None
        self.active_turn = self.active_task = None
        self.progress = ""
        self.muted_turns = set()
        self.awaiting_new_message = set()
        self.buffers = {}
        self.last_error = None
        self.events_task = None
        self.pending_question = None
        self.submit_lock = asyncio.Lock()
        self.command_items = {}
        self.last_turn = None
        self.on_realtime = None
        self.last_user_text = "实时语音委派的任务"

    @property
    def busy(self):
        return self.active_task is not None

    async def start(self):
        await self.client.start()
        saved = self.store.get("thread_id")
        params = {"cwd": self.config.workspace, "sandbox": self.config.codex_sandbox,
                  "approvalPolicy": self.config.approval_policy,
                  "developerInstructions": f"你的名字是{self.config.assistant_name}。\n" + INSTRUCTIONS}
        if saved:
            # A failed resume is surfaced; do not silently lose context or replay work.
            result = await self.client.request("thread/resume", {"threadId": saved, **params}, timeout=60)
        else:
            result = await self.client.request("thread/start", {"dynamicTools": [TOOL], **params}, timeout=60)
        self.thread_id = result["thread"]["id"]
        self.store.set("thread_id", self.thread_id)
        turns = {t["id"]: t for t in result["thread"].get("turns", [])}
        for turn in list(turns.values())[-20:]:
            self.command_items[turn["id"]] = {i["id"] for i in turn.get("items", []) if i.get("type") == "commandExecution"}
            self.last_turn = turn["id"]
        for ident, turn, _ in self.store.unfinished():
            t = turns.get(turn)
            status = (t or {}).get("status", "unknown")
            if status == "inProgress":
                status = "unknown"
            self.store.update_task(ident, status)
        self.events_task = asyncio.create_task(self._events())
        log.info("codex_ready resumed=%s", bool(saved))

    async def submit(self, text):
        async with self.submit_lock:
            if self.pending_question:
                q = self.pending_question
                self.pending_question = None
                answers = {item["id"]: {"answers": [text]} for item in q["params"].get("questions", [])}
                await self.client.respond(q["id"], {"answers": answers})
                return "answered"
            inputs = [{"type": "text", "text": text}]
            if self.active_turn:
                self.awaiting_new_message.add(self.active_turn)
                await self.client.request("turn/steer", {"threadId": self.thread_id,
                    "expectedTurnId": self.active_turn, "input": inputs})
                return "steered"
            ident = uuid.uuid4().hex
            self.active_task = ident
            self.progress = "已经收到，正在处理。"
            self.store.create_task(ident, text)
            try:
                result = await self.client.request("turn/start", {"threadId": self.thread_id, "input": inputs}, timeout=60)
                turn = result["turn"]["id"]
                # The notification reader may have already observed completion.
                if self.active_task == ident:
                    self.active_turn = turn
                    self.store.update_task(ident, "running", turn)
            except BaseException:
                self.store.update_task(ident, "unknown")
                self.active_task = self.active_turn = None
                raise
            return "started"

    def mute_current(self):
        if self.active_turn:
            self.muted_turns.add(self.active_turn)

    async def cancel(self):
        turn = self.active_turn or self.last_turn
        interrupted = self.active_turn is not None
        if not turn:
            return False
        if interrupted:
            self.muted_turns.add(turn)
            await self.client.request("turn/interrupt", {"threadId": self.thread_id, "turnId": turn})
        # Interrupting model generation does NOT terminate yielded shell sessions.
        # Stop only terminals belonging to this task, leaving older user services alone.
        cursor = None
        terminals = []
        while True:
            params = {"threadId": self.thread_id}
            if cursor:
                params["cursor"] = cursor
            page = await self.client.request("thread/backgroundTerminals/list", params)
            terminals.extend(page.get("data", []))
            cursor = page.get("nextCursor")
            if not cursor:
                break
        for terminal in terminals:
            if terminal["itemId"] in self.command_items.get(turn, set()):
                await self.client.request("thread/backgroundTerminals/terminate", {
                    "threadId": self.thread_id, "processId": terminal["processId"]})
                interrupted = True
        return interrupted

    async def _speech(self, text, turn):
        if self.config.backend == "native":
            return  # Codex realtime forwards its own task results to voice.
        text = re.sub(r"```.*?```", "代码已保存在项目中。", text, flags=re.S)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"[\*`#]", "", text).strip()
        if text and turn not in self.muted_turns:
            await self.on_speech(text)

    async def _events(self):
        while True:
            event = await self.client.events.get()
            method, p = event.get("method", ""), event.get("params", {})
            if method == "local/disconnected":
                if self.active_task:
                    self.store.update_task(self.active_task, "unknown")
                raise RpcError("Codex disconnected; pending work will not be automatically repeated")
            if "id" in event and "method" in event:
                await self._server_request(event)
                continue
            if p.get("threadId") and p["threadId"] != self.thread_id:
                continue
            if method.startswith("thread/realtime/"):
                if method == "thread/realtime/transcript/done" and p.get("role") == "user":
                    self.last_user_text = p.get("text", "")
                if self.on_realtime:
                    await self.on_realtime(method, p)
                continue
            if method == "turn/started":
                if self.config.backend == "native" and self.active_task is None:
                    self.active_task = uuid.uuid4().hex
                    self.store.create_task(self.active_task, self.last_user_text)
                self.active_turn = p["turn"]["id"]
                self.last_turn = self.active_turn
                self.command_items.setdefault(self.active_turn, set())
                if len(self.command_items) > 20:
                    self.command_items.pop(next(iter(self.command_items)))
                if self.active_task:
                    self.store.update_task(self.active_task, "running", self.active_turn)
            elif method == "item/started" and p.get("item", {}).get("type") == "agentMessage":
                turn = p.get("turnId", self.active_turn)
                if turn in self.awaiting_new_message:
                    self.awaiting_new_message.discard(turn)
                    self.muted_turns.discard(turn)
            elif method == "item/started":
                kind = p.get("item", {}).get("type")
                if kind == "commandExecution":
                    self.progress = "正在执行本地命令。"
                    turn = p.get("turnId", self.active_turn)
                    self.command_items.setdefault(turn, set()).add(p["item"]["id"])
                elif kind == "fileChange":
                    self.progress = "正在修改本地文件。"
            elif method == "item/agentMessage/delta":
                ident = p.get("itemId", "message")
                value = self.buffers.get(ident, "") + p.get("delta", "")
                # Speak complete short sentences while the answer is still arriving.
                pieces = re.split(r"(?<=[。！？\n])", value)
                self.buffers[ident] = pieces.pop()
                for sentence in pieces:
                    await self._speech(sentence, p.get("turnId", self.active_turn))
            elif method == "item/completed":
                item = p.get("item", {})
                if item.get("type") == "agentMessage":
                    tail = self.buffers.pop(item.get("id"), "")
                    await self._speech(tail, p.get("turnId", self.active_turn))
                    text = item.get("text", "")
                    self.progress = text[:600]
                    self.store.transcript("assistant", text)
                    if self.active_task:
                        self.store.update_task(self.active_task, "running", result=text)
            elif method == "turn/completed":
                turn = p["turn"]
                ident = turn["id"]
                if self.active_task and ident == self.active_turn:
                    self.store.update_task(self.active_task, turn["status"])
                    self.active_task = self.active_turn = None
                    self.pending_question = None
                    self.progress = "任务已经结束。" + self.progress
                if turn.get("status") == "failed":
                    self.last_error = str(turn.get("error", "Task failed"))[:500]
                    await self._speech("这次任务没有完成，错误原因已经记录。", ident)
                self.muted_turns.discard(ident)
                self.awaiting_new_message.discard(ident)
                self.buffers.clear()
            elif method == "error":
                self.last_error = str(p.get("error", "Codex error"))[:500]
                log.warning("codex_error_received")

    async def _server_request(self, event):
        method, p = event["method"], event.get("params", {})
        if method == "item/tool/call":
            try:
                if p["tool"] != "audio_control":
                    raise ValueError("Unknown local tool")
                args = p["arguments"]
                if isinstance(args, str):
                    args = json.loads(args)
                result = await self.on_control(args)
                response = {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps(result, ensure_ascii=False)}]}
            except Exception as exc:
                response = {"success": False, "contentItems": [{"type": "inputText", "text": str(exc)[:200]}]}
            await self.client.respond(event["id"], response)
        elif method == "item/tool/requestUserInput":
            self.pending_question = event
            for question in p.get("questions", []):
                await self.on_speech(question["question"])
        elif method.endswith("/requestApproval"):
            # Do not auto-approve permissions that the configured Codex policy did not grant.
            if method == "item/permissions/requestApproval":
                await self.client.respond(event["id"], {"permissions": {}, "scope": "turn"})
            else:
                await self.client.respond(event["id"], {"decision": "decline"})
            await self.on_speech("这项操作需要当前配置之外的权限，尚未执行。")
        else:
            await self.client.send({"id": event["id"], "error": {"code": -32601, "message": "Unsupported client request"}})

    async def close(self):
        if self.events_task:
            self.events_task.cancel()
            await asyncio.gather(self.events_task, return_exceptions=True)
        await self.client.close()
