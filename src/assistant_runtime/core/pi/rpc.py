"""Native Pi JSONL adapter; no provider emulation or agent loop in the host."""

from assistant_runtime.platforms.executables import pi_command, command_tools
from assistant_runtime.api.system import system

import asyncio
import time
import base64
import copy
import json
import logging
import mimetypes
import io
import os
import sys
import re
from pathlib import Path
import uuid

from assistant_runtime.api.execution import Command
from assistant_runtime.core.pi.catalog import write_pi_models
from assistant_runtime.core.pi.mcp import normalize_services, service_owner, to_pi_mcp_json
from assistant_runtime.core.pi.conversation import retained_native_media_item

log = logging.getLogger(__name__)
MAX_FRAME_BYTES = 64 * 1024 * 1024
CONTROL_TIMEOUT_SECONDS = 30
EXTENSION_PATH = str(Path(__file__).with_name("extension.ts"))
THINKING_MAP = {
    "auto": "off",
    "none": "off",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


class RpcError(RuntimeError):
    pass


def convert_inputs(inputs):
    texts, images = [], []
    for item in inputs:
        kind = item.get("type")
        if kind in ("text", "input_text"):
            texts.append(item.get("text", ""))
        elif kind == "localImage":
            path = Path(item["path"])
            if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("Invalid image input")
            images.append(
                {
                    "type": "image",
                    "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                    "mimeType": mimetypes.guess_type(path.name)[0] or "image/jpeg",
                }
            )
        elif kind == "image" and item.get("data"):
            images.append(
                {"type": "image", "data": item["data"], "mimeType": item.get("mimeType", "image/jpeg")}
            )
        else:
            raise ValueError("Unsupported execution input")
    if (
        sum(len(t.encode()) for t in texts) > 128 * 1024
        or sum(len(i["data"]) for i in images) > 24 * 1024 * 1024
    ):
        raise ValueError("Execution input exceeds bounded size")
    if len(images) > 4:
        raise ValueError("At most four images per request")
    if images:
        from PIL import Image

        for image in images:
            try:
                raw = base64.b64decode(image["data"], validate=True)
                with Image.open(io.BytesIO(raw)) as decoded:
                    if decoded.format not in {"PNG", "JPEG", "GIF", "WEBP", "BMP"}:
                        raise ValueError("Unsupported image format")
                    decoded.verify()
            except (ValueError, OSError, SyntaxError, Image.DecompressionBombError):
                raise ValueError("Invalid execution image content") from None
    return "\n".join(texts), images


class PiSessionWorker:
    """Manages one `pi --mode rpc` child process bound to a single session file."""

    def __init__(self, parent, execution_session_id, meta):
        self.parent = parent
        self.execution_session_id = execution_session_id
        self.meta = meta
        self.process = None
        self.readers = []
        self.pending = {}
        self.counter = 0
        self.active_turn = None
        self.turn_aborted = False
        self.turn_error = None
        self.model_failure = None
        self.turn_items = []
        self.pending_msg = None
        self.pending_tools = {}
        self.active_bash = {}
        self.compacting = False
        self.compaction_error = False

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        agent_dir = Path(self.meta["agent_dir"])
        session_dir = Path(self.meta["session_dir"])
        agent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cwd = self.meta.get("cwd") or self.parent.cwd or os.getcwd()
        Path(cwd).mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()

        env.update(self.parent.environment)
        for k, v in (self.meta.get("shell_env") or {}).items():
            env[str(k)] = str(v)
        tool_token = await self.parent.prepare_tools(self)
        if tool_token:
            env["PA_SHARED_TOOL_TOKEN"] = tool_token
        else:
            env.pop("PA_SHARED_TOOL_TOKEN", None)
        env["PI_CODING_AGENT_DIR"] = str(agent_dir)
        env["PI_CODING_AGENT_SESSION_DIR"] = str(session_dir)
        env["PI_OFFLINE"] = "1"
        env["PI_SKIP_VERSION_CHECK"] = "1"
        env["PI_TELEMETRY"] = "0"
        if sys.platform == "win32":
            env["ASSISTANT_RUNTIME_PYTHON"] = sys.executable
        env["ASSISTANT_TOOL_RESULT_MAX_CHARS"] = str(self.parent.settings.tool_result_max_chars)
        env["ASSISTANT_MAX_LOADED_TOOLS"] = str(self.parent.settings.max_loaded_tools)
        env["ASSISTANT_TOOL_SCHEMA_BYTES"] = str(self.parent.settings.tool_schema_max_bytes)
        env["ASSISTANT_INVALID_TOOL_CALL_LIMIT"] = str(self.parent.settings.invalid_tool_call_limit)
        env["ASSISTANT_TOOL_READ_ATTEMPT_LIMIT"] = str(self.parent.settings.tool_read_attempt_limit)
        env["ASSISTANT_INTERNAL_MCP_SERVERS"] = json.dumps(
            [name for name, spec in self.meta.get("mcp_servers", {}).items() if spec.get("local_service")]
        )
        env["ASSISTANT_STABLE_EXECUTION_PROMPT"] = "1" if self.meta.get("stable_execution_prompt") else "0"
        env["ASSISTANT_PI_DISCOVERY"] = "1" if self.meta.get("mcp_enabled", True) else "0"
        env["ASSISTANT_PI_MODELS"] = str(agent_dir / "models.json")
        env["ASSISTANT_PI_POLICY"] = json.dumps(self.meta.get("policy") or {}, ensure_ascii=False)
        env["ASSISTANT_PI_ENABLE_USER_INPUT"] = "1" if self.meta.get("enable_user_input") else "0"
        env["ASSISTANT_PI_ENABLE_GOAL"] = "1" if self.meta.get("enable_goal") else "0"
        if self.meta.get("runtime_context"):
            env["ASSISTANT_TASK_RUNTIME"] = json.dumps(self.meta["runtime_context"], ensure_ascii=False)
        env["ASSISTANT_CONTROL_TOKEN"] = self.meta.get("control_token", "")
        env["ASSISTANT_CONTROL_SESSION"] = self.execution_session_id
        env["ASSISTANT_CONTROL_URL"] = self.parent.transport.url or ""

        self.compacting = False
        model = self.meta["model"]
        thinking = THINKING_MAP[self.meta["reasoning_effort"]]
        args = [
            *pi_command(self.parent.pi_binary),
            "--mode",
            "rpc",
            "--offline",
            "-na",
            "-np",
            "-ns",
            "-nc",
            "-ne",
            "--no-themes",
            "--provider",
            "assistant_api",
            "--model",
            model,
            "--thinking",
            thinking,
            "--session",
            self.meta["path"],
            "-e",
            EXTENSION_PATH,
        ]
        # -ne disables built-ins too. Retain the pinned engine's capabilities
        # explicitly without discovering project or user-installed extensions.
        # Discovery composes Pi's public tool-search factory in our extension.
        for extension in () if self.meta.get("controlled_context") else ("codemode", "llama.cpp"):
            args.extend(["-e", "builtin:" + extension])
        for selected_path in self.meta.get("skill_paths", self.parent.skill_paths):
            path = Path(selected_path)
            if path.is_dir():
                args.extend(["--skill", str(path)])
        if not self.meta.get("mcp_enabled", True):
            args.append("--no-mcp")

        self.process = await system.spawn(
            *args,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_FRAME_BYTES,
            start_new_session=True,
        )
        self.launch_id = str(uuid.uuid4())
        self.observe_process()
        self.readers = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]
        # Pi can continue after an extension load error. Never let that degrade
        # policy enforcement into an unrestricted default loop.
        try:
            readiness = await self.command({"type": "get_commands"})
            ready = any(
                row.get("name") == "assistant_tool_contract_v1" for row in readiness.get("commands", [])
            )
        except (RpcError, TimeoutError):
            ready = False
        if not ready:
            await self.close()
            raise RpcError("Tool contract extension failed to initialize; execution refused")
        if self.meta.get("injected_items"):
            encoded = base64.b64encode(
                json.dumps(self.meta["injected_items"], ensure_ascii=False).encode("utf-8")
            ).decode("ascii")
            self.meta["injected_items"] = []
            await self.command({"type": "prompt", "message": f"/inject_context {encoded}"})

    async def send_line(self, payload):
        if not self.process or self.process.returncode is not None:
            raise RpcError("Pi session process is not running")
        self.process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.process.stdin.drain()

    async def command(self, cmd, timeout=CONTROL_TIMEOUT_SECONDS):
        await self.start()
        self.counter += 1
        req_id = f"cmd-{self.counter}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[req_id] = fut
        try:
            await self.send_line({**cmd, "id": req_id})
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(req_id, None)

    async def _read_stderr(self):
        while self.process and self.process.stderr and await self.process.stderr.readline():
            pass

    async def _read_stdout(self):
        try:
            while line := await self.process.stdout.readline():
                msg = json.loads(line)
                await self._handle_message(msg)
        except Exception as exc:
            log.warning(
                "Pi event stream failed (%s): %s",
                self.execution_session_id,
                type(exc).__name__,
                exc_info=True,
            )
        finally:
            self.compacting = False
            self.parent.revoke_tools(self)
            for fut in list(self.pending.values()):
                if not fut.done():
                    fut.set_exception(RpcError("Pi session connection closed"))
            if self.active_turn:
                turn_id = self.active_turn
                self.active_turn = None
                from .waiting import pending

                if not pending(self):
                    await self._finalize_turn(turn_id, "unknown")

    async def _handle_message(self, msg):
        mtype = msg.get("type")
        if mtype == "response":
            req_id = msg.get("id")
            fut = self.pending.get(req_id)
            if fut and not fut.done():
                if msg.get("success"):
                    fut.set_result(msg.get("data", {}))
                else:
                    fut.set_exception(RpcError(str(msg.get("error", "Pi command failed"))))
            return

        if mtype == "extension_ui_request":
            method = msg.get("method")
            if method == "notify":
                text = str(msg.get("message", ""))
                if text.startswith("__assistant_message_delivery__:"):
                    try:
                        delivery = json.loads(text.split(":", 1)[1])
                    except ValueError:
                        return
                    if (
                        self.pending_msg
                        and self.active_turn
                        and isinstance(delivery, dict)
                        and set(delivery) == {"block", "phase", "evidence"}
                        and type(delivery["block"]) is int
                        and 0 <= delivery["block"] < 128
                        and (
                            (
                                delivery["phase"] in {"final", "commentary"}
                                and delivery["evidence"] == "provider"
                            )
                            or (delivery["phase"] == "unknown" and delivery["evidence"] == "conflict")
                        )
                    ):
                        self.pending_msg.setdefault("delivery", {})[delivery["block"]] = delivery
                        if self.pending_msg["published"]:
                            await self.parent.events.put(
                                {
                                    "kind": "message.delivery",
                                    "data": {
                                        "session_id": self.execution_session_id,
                                        "run_id": self.active_turn,
                                        "activity_id": self.pending_msg["id"],
                                        "delivery": delivery,
                                    },
                                }
                            )
                    return
                if text.startswith("__assistant_context_checkpoint__:"):
                    self.context_boundary = json.loads(text.split(":", 1)[1])
                    return
                if text.startswith("__assistant_tool_stop__:"):
                    try:
                        problem = json.loads(text.split(":", 1)[1])
                    except ValueError:
                        problem = None
                    if (
                        isinstance(problem, dict)
                        and problem.get("code") in {"repeated_invalid_call", "repeated_read_failure"}
                        and isinstance(problem.get("message"), str)
                    ):
                        self.tool_stop = {"code": problem["code"], "message": problem["message"][:1200]}
                        self.turn_error = self.tool_stop["message"]
                    else:
                        log.warning("Invalid tool stop notification session=%s", self.execution_session_id)
                if text.startswith("__assistant_tool_outcome__:"):
                    outcome = json.loads(text.split(":", 1)[1])
                    log.info(
                        "Pi tool outcome session=%s call=%s code=%s dispatch=%s preparation_ms=%s execution_ms=%s repeats=%s read_attempt=%s retryable=%s",
                        self.execution_session_id,
                        outcome.get("call_id"),
                        outcome.get("code"),
                        outcome.get("dispatch"),
                        outcome.get("preparation_ms"),
                        outcome.get("duration_ms"),
                        outcome.get("repeats", 0),
                        outcome.get("read_attempt"),
                        outcome.get("retryable"),
                    )
                if text.startswith("__assistant_discovery__:"):
                    discovery = json.loads(text.split(":", 1)[1])
                    log.info(
                        "Pi tool discovery session=%s mode=%s tools=%s",
                        self.execution_session_id,
                        discovery["mode"],
                        discovery["tools"],
                    )
                if text.startswith("__assistant_tools__:"):
                    state = json.loads(text.split(":", 1)[1])
                    log.info(
                        "Pi tool loadout session=%s active=%s catalog=%s schema_chars=%s evicted=%s",
                        self.execution_session_id,
                        len(state["active"]),
                        state["catalog"],
                        state["schema_chars"],
                        len(state["evicted"]),
                    )
                    await self.parent.events.put(
                        {
                            "kind": "tools.updated",
                            "data": {
                                "session_id": self.execution_session_id,
                                "run_id": self.active_turn,
                                **state,
                            },
                        }
                    )
                if text.startswith("__assistant_goal__:"):
                    goal = json.loads(text.split(":", 1)[1])
                    self.meta["goal"] = goal
                    self.parent._save_meta(self.execution_session_id)
                    await self.parent.events.put(
                        {
                            "kind": "goal.updated",
                            "data": {"session_id": self.execution_session_id, "goal": goal},
                        }
                    )
                return
            if method == "input" and msg.get("title") == "__assistant_compaction__":
                allowed = False
                if self.meta.get("budget_id"):
                    budget = await self.parent.usage.snapshot(self.meta["budget_id"])
                    allowed = not budget.get("blocked")
                self.compaction_error = False
                self.compacting = allowed
                await self.send_line(
                    {
                        "type": "extension_ui_response",
                        "id": msg["id"],
                        "value": json.dumps({"allowed": allowed}),
                    }
                )
                return
            if method == "input" and msg.get("title") == "__assistant_rpc__":
                ui_id = msg["id"]
                rpc_payload = json.loads(msg.get("placeholder") or "{}")
                rpc_method = rpc_payload.get("kind", "")
                rpc_params = {
                    "session_id": self.execution_session_id,
                    "run_id": self.active_turn,
                    **(rpc_payload.get("data") or {}),
                }
                req_id = self.parent.register_ui_request(self, ui_id, rpc_method)
                event = {"id": req_id, "kind": rpc_method, "data": rpc_params}
                if rpc_method == "input.requested":
                    from .waiting import capture

                    await capture(self, event)
                await self.parent.events.put(event)
            return

        if mtype == "compaction_end":
            self.compacting = False
            self.compaction_error = bool(msg.get("errorMessage"))
            log.info(
                "Pi compaction ended session=%s failed=%s aborted=%s",
                self.execution_session_id,
                self.compaction_error,
                bool(msg.get("aborted")),
            )
        turn_id = self.active_turn
        if not turn_id:
            return

        if mtype == "message_start":
            message = msg.get("message") or {}
            if message.get("role") == "assistant":
                msg_id = f"{turn_id}-msg-{len(self.turn_items) + 1}"
                # A provider message may contain only reasoning/tool calls. Publish
                # conversation activity only when it actually has visible text.
                self.pending_msg = {"id": msg_id, "text": "", "published": False}
        elif mtype == "message_update":
            ame = msg.get("assistantMessageEvent") or {}
            if ame.get("type") == "text_delta" and self.pending_msg:
                delta = ame.get("delta", "")
                if delta:
                    self.pending_msg["text"] += delta
                    if not self.pending_msg["published"]:
                        if not self.pending_msg["text"].strip():
                            return
                        self.pending_msg["published"] = True
                        delta = self.pending_msg["text"]
                        await self.parent.events.put(
                            {
                                "kind": "activity.started",
                                "data": {
                                    "session_id": self.execution_session_id,
                                    "run_id": turn_id,
                                    "item": {"id": self.pending_msg["id"], "type": "message", "text": ""},
                                },
                            }
                        )
                    await self.parent.events.put(
                        {
                            "kind": "message.delta",
                            "data": {
                                "session_id": self.execution_session_id,
                                "run_id": turn_id,
                                "activity_id": self.pending_msg["id"],
                                "delta": delta,
                                "block": ame.get("contentIndex", 0),
                                "delivery": self.pending_msg.get("delivery", {}).get(
                                    ame.get("contentIndex", 0)
                                ),
                            },
                        }
                    )
        elif mtype == "message_end":
            message = msg.get("message") or {}
            if message.get("role") == "assistant":
                if message.get("stopReason") == "error" or message.get("errorMessage"):
                    self.turn_error = message.get("errorMessage") or "Model generation failed"
        elif mtype == "tool_execution_start":
            call_id = msg.get("toolCallId") or uuid.uuid4().hex
            tool_name = msg.get("toolName") or ""
            args = msg.get("args") or {}
            if self.parent.admission:
                self.parent.admission.operation(
                    self.execution_session_id,
                    "tool",
                    call_id,
                    tool=tool_name,
                    turn_id=turn_id,
                    model_request_id=getattr(self, "last_usage_call", None),
                )
            item = self._to_tool_item(call_id, tool_name, args, status="running")
            # Execution ownership must not depend on whether the UI renders this tool.
            self.meta.setdefault("pending_operations", {})[call_id] = item or {
                "id": call_id,
                "type": "tool",
                "tool": tool_name,
                "status": "running",
            }
            self.parent._save_meta(self.execution_session_id)
            if item:
                self.pending_tools[call_id] = item
                if item["type"] == "command":
                    self.active_bash[call_id] = {"activity_id": call_id, "process_id": f"bash-{call_id}"}
                await self.parent.events.put(
                    {
                        "kind": "activity.started",
                        "data": {
                            "session_id": self.execution_session_id,
                            "run_id": turn_id,
                            "item": copy.deepcopy(item),
                        },
                    }
                )
        elif mtype == "tool_execution_end":
            outcome = ((msg.get("result") or {}).get("details") or {}).get("tool_outcome") or {}
            if self.parent.admission and msg.get("toolCallId"):
                self.parent.admission.operation(
                    self.execution_session_id,
                    "tool",
                    msg["toolCallId"],
                    "not_sent"
                    if outcome.get("dispatch") == "not_dispatched"
                    else "unknown"
                    if outcome.get("outcome") == "unknown"
                    else "failed"
                    if msg.get("isError")
                    else "completed",
                    tool_outcome={
                        k: outcome[k]
                        for k in (
                            "code",
                            "stage",
                            "dispatch",
                            "outcome",
                            "call_id",
                            "parent_call_id",
                            "definition_version",
                            "permission_version",
                            "duration_ms",
                            "preparation_ms",
                            "validation_ms",
                            "waiting_ms",
                            "result_processing_ms",
                            "repeats",
                        )
                        if k in outcome
                    },
                )
            call_id = msg.get("toolCallId") or ""
            tool_name = msg.get("toolName") or ""
            args = msg.get("args") or {}
            is_error = bool(msg.get("isError"))
            result = msg.get("result") or {}
            self.meta.setdefault("pending_operations", {}).pop(call_id, None)
            self.active_bash.pop(call_id, None)
            item = (
                self.pending_tools.pop(call_id, None)
                or self._to_tool_item(call_id, tool_name, args)
                or retained_native_media_item(call_id, tool_name, result)
            )
            if item:
                item["status"] = "failed" if is_error else "completed"
                if outcome:
                    item["tool_outcome"] = outcome
                if item["type"] == "mcp":
                    item["result"] = result
                elif item["type"] == "command":
                    text_out = "".join(
                        b.get("text", "")
                        for b in (result.get("content") or [])
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                    item["output"] = text_out[:12000]
                self.turn_items.append(copy.deepcopy(item))
                self.meta["current_items"] = list(self.turn_items)
                self.parent._save_meta(self.execution_session_id)
                await self.parent.events.put(
                    {
                        "kind": "activity.completed",
                        "data": {
                            "session_id": self.execution_session_id,
                            "run_id": turn_id,
                            "item": copy.deepcopy(item),
                        },
                    }
                )
            else:
                self.parent._save_meta(self.execution_session_id)
        elif mtype == "turn_end":
            message = msg.get("message") or {}
            tool_results = msg.get("toolResults") or []
            has_tools = bool(tool_results) or message.get("stopReason") == "toolUse"
            text = ""
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text += block.get("text", "")
            if not text and self.pending_msg:
                text = self.pending_msg["text"]
            msg_id = (
                self.pending_msg["id"] if self.pending_msg else f"{turn_id}-msg-{len(self.turn_items) + 1}"
            )
            self.pending_msg = None
            if text.strip():
                phase = "commentary" if has_tools else "final"
                item = {
                    "id": msg_id,
                    "type": "message",
                    "phase": phase,
                    "text": text,
                    "status": "completed",
                }
                self.turn_items.append(copy.deepcopy(item))
                self.meta["current_items"] = list(self.turn_items)
                self.parent._save_meta(self.execution_session_id)
                await self.parent.events.put(
                    {
                        "kind": "activity.completed",
                        "data": {
                            "session_id": self.execution_session_id,
                            "run_id": turn_id,
                            "item": item,
                        },
                    }
                )
            usage = message.get("usage")
            if isinstance(usage, dict):
                mapped_usage = {
                    "input_tokens": int(usage.get("input", 0))
                    + int(usage.get("cacheRead", 0))
                    + int(usage.get("cacheWrite", 0)),
                    "cached_input_tokens": int(usage["cacheRead"]) if usage.get("cacheRead") else None,
                    "cache_write_input_tokens": int(usage["cacheWrite"]) if usage.get("cacheWrite") else None,
                    "output_tokens": int(usage.get("output", 0)),
                    "total_tokens": int(usage.get("totalTokens", 0)),
                }
                total = self.meta.setdefault("usage", {k: 0 for k in mapped_usage})
                for key, value in mapped_usage.items():
                    total[key] = total[key] + value if total[key] is not None and value is not None else None
                await self.parent.events.put(
                    {
                        "kind": "usage.updated",
                        "data": {
                            "session_id": self.execution_session_id,
                            "usage": {"last": mapped_usage, "total": dict(total)},
                        },
                    }
                )
        elif mtype == "agent_settled":
            aborted = bool(msg.get("aborted")) or self.turn_aborted
            status = "interrupted" if aborted else ("failed" if self.turn_error else "completed")
            if getattr(self, "tool_stop", False):
                status = "failed"
            self.active_turn = None
            await self._finalize_turn(turn_id, status, self.turn_error)

    def _to_tool_item(self, call_id, tool_name, args, status="completed"):
        if tool_name in ("audio_control", "request_user_input", "update_task_goal"):
            return None
        if tool_name in ("bash", "powershell"):
            return {
                "id": call_id,
                "type": "command",
                "command": str((args or {}).get("command", "")),
                "status": status,
            }
        if tool_name in ("edit", "write"):
            return {
                "id": call_id,
                "type": "file",
                "changes": str((args or {}).get("path", "")),
                "status": status,
            }
        if tool_name.startswith("mcp__"):
            rest = tool_name[5:]
            if "__" in rest:
                server, tool = rest.split("__", 1)
                return {
                    "id": call_id,
                    "type": "mcp",
                    "server": server,
                    "tool": tool,
                    "arguments": args or {},
                    "status": status,
                }
        return None

    async def _finalize_turn(self, turn_id, status, error=None):
        self.compacting = False
        if self.parent.shared_tools:
            self.parent.shared_tools.cancel(self)
        # Pi's dialog settles on abort; discard the host correlation too, so a
        # late answer cannot address an ended turn or a later turn's question.
        for ident, (worker, _, _) in list(self.parent.ui_requests.items()):
            if worker is self:
                self.parent.ui_requests.pop(ident, None)
        turn_record = {
            "id": turn_id,
            "status": status,
            "items": list(self.turn_items),
            "itemsView": "full",
            "retry_of": self.meta.get("retry_of"),
            **self.meta.get("active_execution_facts", {}),
            "finished_at": time.time(),
        }
        if error:
            match = re.match(
                r"^(?:assistant_api API error \(|native_model_http_)?((?:401|402|403|404|408|429|500|502|503|504))(?:\)|\s|:|$)",
                str(error),
            )
            turn_record["error"] = {
                "message": "Model generation failed" + (f" (HTTP {match[1]})" if match else ""),
                "code": "generation_failed",
                **({"status": int(match[1])} if match else {}),
            }
        failure = getattr(self, "model_failure", None)
        if error and failure:
            turn_record["error"] = dict(failure)
            turn_record["failure"] = dict(failure)
        turn_record["budget_id"] = self.meta.get("budget_id")
        if getattr(self, "tool_stop", False):
            turn_record["error"] = self.tool_stop
            self.tool_stop = False
        if self.compaction_error and status in {"completed", "failed"}:
            turn_record["status"] = "failed"
            turn_record["error"] = {
                "code": "context_compaction_failed",
                "message": "Context compaction failed; original context retained. Explicit continuation may retry without replaying tools.",
            }
            self.compaction_error = False
        if self.meta.get("budget_id"):
            from assistant_runtime.api.ai_usage import UsageError

            try:
                budget = await self.parent.usage.snapshot(self.meta["budget_id"])
            except UsageError:
                budget = {**self.parent.usage.snapshots.get(self.meta["budget_id"], {}), "unavailable": True}
            turn_record["ai_usage"] = budget
            # A lost model response retains unknown usage and its reservation. It
            # does not undo Pi's authoritative terminal event. Only explicitly
            # settled generation failures may continue; process loss remains guarded.
            if getattr(self, "usage_error", None) and status != "unknown":
                turn_record["status"] = "failed"
                turn_record["error"] = {"code": "ai_usage_blocked", "message": self.usage_error}
                self.usage_error = None
        if self.meta.get("pending_operations"):
            turn_record["status"] = "unknown"
        if turn_record.get("failure"):
            unsafe = any(i.get("tool_outcome", {}).get("outcome") == "unknown" for i in self.turn_items)
            turn_record["failure"]["retry_allowed"] = bool(
                turn_record["status"] == "failed"
                and failure.get("generation_settled")
                and not unsafe
                and turn_record.get("error", {}).get("code") != "ai_usage_blocked"
            )
            if turn_record.get("error", {}).get("code") == failure.get("code"):
                turn_record["error"]["retry_allowed"] = turn_record["failure"]["retry_allowed"]
        self.meta["active_run"] = None
        boundary = getattr(self, "context_boundary", None)
        if turn_record["status"] == "completed":
            self.meta["context_completed_seen"] = True
        if turn_record["status"] == "completed" and boundary:
            try:
                with Path(self.meta["path"]).open("rb+") as session_stream:
                    os.fsync(session_stream.fileno())
            except OSError:
                log.warning(
                    "Context checkpoint persistence unavailable session=%s", self.execution_session_id
                )
                boundary = None
        if turn_record["status"] == "completed" and boundary and boundary.get("leaf"):
            lineage = self.meta.setdefault("context_lineage", [])
            reference = self.meta.get("context_input_id")
            if reference and reference not in lineage:
                lineage.append(reference)
            self.meta["context_checkpoint"] = {
                "leaf": boundary["leaf"],
                "id": uuid.uuid4().hex,
                "inputs": list(lineage),
                "coverage_known": bool(self.meta.get("context_coverage_known", False)),
            }
        elif self.meta.get("context_input_id"):
            self.meta["context_coverage_known"] = False
        self.context_boundary = None
        turns = self.meta.setdefault("runs", [])
        existing = next((t for t in turns if t["id"] == turn_id), None)
        if existing:
            existing.update(turn_record)
        else:
            turns.append(turn_record)
        self.meta["runs"] = turns[-100:]
        self.meta["current_items"] = []
        self.parent._save_meta(self.execution_session_id)
        if self.parent.closing:
            return
        await self.parent.events.put(
            {
                "kind": "run.completed",
                "data": {"session_id": self.execution_session_id, "run": turn_record},
            }
        )

    def observe_process(self):
        if self.process is not None and self.parent.admission:
            self.parent.admission.process_observed(
                self.execution_session_id,
                {
                    "launch_id": self.launch_id,
                    "pid": self.process.pid,
                    "leader": "running" if self.process.returncode is None else "exited",
                    "returncode": self.process.returncode,
                    "tree": "unknown",
                    "scope": "owned_leader_only",
                },
            )

    async def close(self):
        # A full host event queue must not prevent process/pipes from settling.
        self.compacting = False
        self.parent.revoke_tools(self)
        for task in self.readers:
            task.cancel()
        await asyncio.gather(*self.readers, return_exceptions=True)
        self.readers.clear()
        if self.process:

            async def discard(stream):
                while await stream.read(65536):
                    pass

            drains = [
                asyncio.create_task(discard(stream)) for stream in (self.process.stdout, self.process.stderr)
            ]
            try:
                if self.process.returncode is None:
                    self.process.stdin.close()
                    await system.stop_process(self.process, timeout=5)
            finally:
                for task in drains:
                    task.cancel()
                await asyncio.gather(*drains, return_exceptions=True)
        self.observe_process()
        self.process = None


class PiClient:
    def __init__(
        self,
        binary="pi",
        cwd=None,
        *,
        settings,
        environment=None,
        runtime_dir,
        usage,
        skill_paths=(),
        accounts=None,
        shell="auto",
        tool_plugins=None,
    ):
        self.pi_binary = binary
        self.shell = shell
        self.cwd = cwd
        self.skill_paths = tuple(Path(p).resolve() for p in skill_paths)
        self.settings = settings
        from assistant_runtime.api.capacity import Capacity

        self.capacity = Capacity(16)
        self.admission = None
        self.environment = environment or {}
        self.runtime_dir = Path(runtime_dir)
        self.events = asyncio.Queue(maxsize=256)
        self.workers = {}
        self.ui_requests = {}
        self.counter = 0
        self.started = False
        self.closing = False
        self.configuration_ready = asyncio.Event()
        self.configuration_ready.set()
        self.configuration_tasks = set()
        self.locks = {}
        from assistant_runtime.core.pi.transport import ExecutionTransport

        self.usage = usage
        from assistant_runtime.core.pi.accounts import NativeAccounts
        import secrets

        self.accounts = accounts or NativeAccounts(Path(cwd), settings, binary=self.pi_binary)
        self.environment["PA_EXECUTION_TOKEN"] = secrets.token_urlsafe(32)
        self.transport = ExecutionTransport(self)
        self.shared_tools = None
        self.tool_plugins = tool_plugins

    async def start(self):
        pi_command(self.pi_binary)
        self.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not Path(EXTENSION_PATH).is_file():
            raise RuntimeError("Pi extension missing from installation")
        self.closing = False
        await self.transport.start()
        self.started = True

    def _meta_path(self, ident):
        if not isinstance(ident, str) or not ident or any(c not in "0123456789abcdef-" for c in ident):
            raise ValueError("Invalid execution session identity")
        return self.runtime_dir / ident / "metadata.json"

    def _load_meta(self, ident):
        path = self._meta_path(ident)
        return (json.loads(path.read_text(encoding="utf-8")), path) if path.exists() else (None, None)

    def _save_meta(self, ident, meta=None):
        if meta is None:
            meta = self.workers[ident].meta
        path = self._meta_path(ident)
        system.atomic_write(path, json.dumps(meta, ensure_ascii=False).encode("utf-8"))

    def register_ui_request(self, worker, ui_id, method):
        self.counter += 1
        ident = uuid.uuid4().hex if method == "input.requested" else self.counter
        self.ui_requests[ident] = (worker, ui_id, method)
        return ident

    def restore_wait(self, session, turn):
        from .waiting import restore

        return restore(self, session, turn)

    def validate_answer(self, ident):
        from .waiting import pending, validate

        entry = self.ui_requests.get(ident)
        if not entry:
            from assistant_runtime.api.models import CapabilityRequestError

            raise CapabilityRequestError("question_expired", "Input request is no longer active")
        worker, ui_id, _ = entry
        if pending(worker) or ui_id is None:
            validate(self, worker, ident)

    def track_input(self, session, turn, ident, role, text, tool_call_id=None):
        from .input_receipts import track

        track(self, session, turn, ident, role, text, tool_call_id)

    async def respond(self, ident, result):
        entry = self.ui_requests.get(ident)
        if entry is None:
            raise RpcError("Input request is no longer active")
        worker, ui_id, _ = entry
        from .waiting import pending, resume

        self.validate_answer(ident)
        checkpoint = pending(worker)
        turn_id = worker.active_turn or (checkpoint or {}).get("turn_id")
        if checkpoint:
            checkpoint["state"] = "answering"
            self._save_meta(worker.execution_session_id)
        self.ui_requests.pop(ident, None)

        async def dispatch():
            if ui_id is not None and worker.process and worker.process.returncode is None:
                await worker.send_line(
                    {
                        "type": "extension_ui_response",
                        "id": ui_id,
                        "value": json.dumps(result, ensure_ascii=False),
                    }
                )
            else:
                await resume(self, worker, checkpoint, result)
            return {"run": {"id": turn_id}}

        if checkpoint and self.admission:
            await self.admission.start({"session_id": worker.execution_session_id}, dispatch, resume=True)
        else:
            await dispatch()
        if self.admission:
            await self.events.put(
                {
                    "kind": "input.answered",
                    "data": {"session_id": worker.execution_session_id, "question_id": ident},
                }
            )

    async def send(self, message):
        if "error" not in message or "id" not in message:
            raise RpcError("Unsupported executor response")
        entry = self.ui_requests.pop(message["id"], None)
        if entry:
            worker, ui_id, _ = entry
            await worker.send_line({"type": "extension_ui_response", "id": ui_id, "cancelled": True})

    async def request(self, command, arguments, timeout=None):
        if asyncio.current_task() not in self.configuration_tasks:
            await self.configuration_ready.wait()
        command = Command(command)
        if not self.started:
            raise RpcError("Executor is not started")
        if timeout is None:
            # Pi acknowledges prompt only after preflight, which can compact the session.
            # Keep control RPCs short without truncating the configured model deadline.
            timeout = (
                self.settings.outer_timeout_seconds + CONTROL_TIMEOUT_SECONDS
                if command == Command.START
                else CONTROL_TIMEOUT_SECONDS
            )
        async with asyncio.timeout(timeout):

            def dispatch():
                if command == Command.START and getattr(self, "configuration_unavailable", False):
                    raise RpcError("Executor unavailable after a configuration recovery failure")
                return self._request(command, arguments)

            if self.admission and command == Command.START:
                return await self.admission.start(arguments, dispatch)
            if self.admission and command == Command.CANCEL:
                return await self.admission.cancel(arguments, dispatch)
            result = await dispatch()
            if self.admission and command in {Command.CLOSE, Command.ARCHIVE}:
                self.admission.closed(arguments["session_id"])
            return result

    def _worker(self, ident):
        if ident in self.workers:
            return self.workers[ident]
        meta, _ = self._load_meta(ident)
        if not meta:
            raise RpcError("Unknown execution session")
        worker = PiSessionWorker(self, ident, meta)
        self.workers[ident] = worker
        return worker

    async def _request(self, command, p):
        if command == Command.MODELS:
            return {
                "data": [m for m in self.settings.public_catalog() if m["available"]],
                "next_cursor": None,
            }
        if command == Command.CONFIGURATION:
            return {"config": {"mcp_servers": {}}}
        if command == Command.MCP_LOGIN:
            raise RpcError(
                "Interactive MCP OAuth is not implemented by this adapter; configure credentials and verify"
            )
        if command == Command.CREATE:
            # Registration persists metadata only. Processes/model dispatch start
            # after shared execution admission; queued sessions must not exhaust
            # an unrelated count of idle/registered sessions.
            ident = uuid.uuid4().hex
            folder = self.runtime_dir / ident
            folder.mkdir(mode=0o700)
            cfg = p.get("config") or {}
            model = p.get("model") or next(iter(self.settings.entries), "")
            effort = (
                cfg.get("model_reasoning_effort")
                or p.get("reasoning_effort")
                or self.settings.entry(model)["default_reasoning_effort"]
            )
            self._selection(model, effort)
            from .scope import select

            scope = select(
                cfg.get("allowed_tools"), cfg.get("skills"), cfg.get("mcp_servers", {}), self.skill_paths
            )
            servers = scope["mcp_servers"]
            meta = {
                "id": ident,
                "path": str(folder / "session.jsonl"),
                "agent_dir": str(folder),
                "session_dir": str(folder),
                "cwd": str(p.get("cwd") or self.cwd),
                "model": model,
                "model_binding": self._binding(model),
                "reasoning_effort": effort,
                "runs": [],
                "goal": None,
                "injected_items": [],
                "context_coverage_known": True,
                "context_tracking_version": 1,
                "context_completed_seen": False,
                "active_run": None,
                "pending_operations": {},
                "current_items": [],
                "archived": False,
                "mcp_servers": servers,
                "mcp_enabled": any(s.get("enabled", True) for s in servers.values()),
                "enable_user_input": True,
                "enable_goal": bool(p.get("long_term")),
                "budget_scope": p.get("budget_id"),
                "shell_env": cfg.get("shell_environment_policy.set", {}),
                "allowed_tools": scope["allowed_tools"],
                "stable_execution_prompt": bool(p.get("_stable_execution_prompt")),
                "runtime_context": p.get("runtime_context"),
                "controlled_context": cfg.get("controlled_context"),
                "control_token": cfg.get("control_token", ""),
                "skill_paths": scope["skill_paths"],
            }
            self._write_configuration(meta, p.get("instructions", ""))
            self._save_meta(ident, meta)
            self.workers[ident] = PiSessionWorker(self, ident, meta)
            return self._session_reply(meta)
        ident = p["session_id"]
        if command in (Command.CHECKPOINT, Command.SNAPSHOT):
            meta = self.workers[ident].meta if ident in self.workers else self._load_meta(ident)[0]
            if not meta:
                raise RpcError("Unknown execution session")
            return await self._session_command(command, p, PiSessionWorker(self, ident, copy.deepcopy(meta)))
        if (
            command in {Command.READ, Command.RUNS, Command.OPERATIONS, Command.GOAL_GET}
            and ident not in self.workers
        ):
            meta, _ = self._load_meta(ident)
            if not meta:
                raise RpcError("Unknown execution session")
            # Recovery reads must not reserve one of the live session slots or
            # start a process. Metadata is atomically persisted by its owner.
            return await self._session_command(command, p, PiSessionWorker(self, ident, meta))
        async with self.locks.setdefault(ident, asyncio.Lock()):
            return await self._session_command(command, p, self._worker(ident))

    def _binding(self, model):
        entry = self.settings.entry(model)
        provider = self.settings.provider_for(entry)
        return {
            "provider": self.settings.route_identity(entry),
            "model": entry["model"],
            "protocol": provider["protocol"],
            **(
                {
                    "account": provider["account"],
                    "account_config": self.settings.accounts[provider["account"]],
                }
                if provider["protocol"] == "pi"
                else {"base_url": provider["base_url"]}
            ),
        }

    def _selection(self, model, effort):
        entry = self.settings.entry(model)
        if (effort != "auto" and effort not in entry["reasoning_efforts"]) or effort not in THINKING_MAP:
            raise ValueError("Unsupported execution reasoning effort")

    def _write_configuration(self, meta, instructions=None):
        previous = Path(meta["agent_dir"])
        # Pi migrates JSONL files found directly in its agent configuration directory.
        # Keep generated configuration separate from the explicitly owned session file.
        folder = self.runtime_dir / meta["id"] / "agent"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if previous != folder and instructions is None and (previous / "SYSTEM.md").is_file():
            instructions = (previous / "SYSTEM.md").read_text(encoding="utf-8")
        meta["agent_dir"] = str(folder)
        meta["mcp_servers"] = normalize_services(meta["mcp_servers"])
        meta["mcp_enabled"] = any(spec.get("enabled", True) for spec in meta["mcp_servers"].values())
        meta["policy"] = {
            "allowed_tools": meta.get("allowed_tools"),
            "mcp_policies": {
                name: {
                    "approval": spec.get("default_tools_approval_mode", "approve"),
                    "allow_tools": spec.get("enabled_tools"),
                    "deny_tools": spec.get("disabled_tools", []),
                    "tool_approvals": {
                        tool: value.get("approval_mode", "approve")
                        for tool, value in spec.get("tools", {}).items()
                    },
                }
                for name, spec in meta["mcp_servers"].items()
            },
        }
        write_pi_models(folder, self.settings, session_id=meta["id"], transport_url=self.transport.url)
        for name, data in (
            (
                "settings.json",
                {
                    "retry": {
                        "enabled": False,
                        "maxRetries": 0,
                        "provider": {
                            "maxRetries": 0,
                            "timeoutMs": self.settings.outer_timeout_seconds * 1000,
                        },
                    },
                    "compaction": {"enabled": True},
                    "cacheWarming": "off",
                    "defaultTools": ["read", command_tools(self.shell), "edit", "write"],
                },
            ),
        ):
            path = folder / name
            path.write_text(json.dumps(data), encoding="utf-8")
            system.private(path)
        if instructions is not None:
            path = folder / "SYSTEM.md"
            path.write_text(instructions, encoding="utf-8")
            system.private(path)

    async def prepare_tools(self, worker, *, inspect=False):
        meta = worker.meta
        servers = meta["mcp_servers"] = normalize_services(meta["mcp_servers"])
        if self.tool_plugins is not None:
            enabled = set(self.tool_plugins())
            for name, spec in list(servers.items()):
                if name != "extensions" and spec.get("local_service") and service_owner(name) not in enabled:
                    servers[name] = {**spec, "enabled": False}
            meta["mcp_enabled"] = any(spec.get("enabled", True) for spec in servers.values())
        names = [
            name for name, spec in servers.items() if spec.get("local_service") and spec.get("enabled", True)
        ]
        token = None
        if names:
            from assistant_runtime.core.pi.shared_tools import SharedTools

            if self.shared_tools is None:
                self.shared_tools = SharedTools(self)
            await self.shared_tools.prepare(names)
            token = self.shared_tools.issue(worker)
        if not inspect:
            path = Path(meta["agent_dir"]) / "mcp.json"
            config = to_pi_mcp_json(
                servers,
                preload=meta.get("stable_execution_prompt", False),
                allowed=meta.get("allowed_tools"),
                limit=self.settings.max_loaded_tools,
                shared_url=self.shared_tools.url if token else None,
            )
            system.atomic_write(path, json.dumps(config).encode("utf-8"))
        return token

    def revoke_tools(self, worker):
        if self.shared_tools:
            self.shared_tools.revoke(worker)

    def _session_reply(self, meta, active=False):
        return {
            **({"context_seed": meta["context_seed"]} if meta.get("context_seed") else {}),
            "session": {
                "id": meta["id"],
                "path": meta["path"],
                "status": {"type": "active" if active else ("unknown" if meta.get("active_run") else "idle")},
                "runs": copy.deepcopy(meta.get("runs", [])),
                "archived": meta.get("archived", False),
            },
            "model": meta["model"],
            "reasoning_effort": meta["reasoning_effort"],
        }

    async def _session_command(self, command, p, w):
        ident, meta = w.execution_session_id, w.meta
        active = bool(w.active_turn)
        if command == Command.READ:
            result = self._session_reply(meta, active)
            if not p.get("include_runs"):
                result["session"]["runs"] = []
            elif meta.get("active_run"):
                result["session"]["runs"].append(
                    {
                        "id": meta["active_run"],
                        "status": "running" if active else "unknown",
                        "items": meta.get("current_items", []),
                        "itemsView": "full",
                        "retry_of": meta.get("retry_of"),
                    }
                )
            return result
        if command == Command.RUNS:
            rows = list(meta.get("runs", []))
            if p.get("sort") == "desc":
                rows.reverse()
            offset = int(p.get("cursor") or 0)
            limit = min(100, max(1, p.get("limit", 50)))
            return {
                "data": rows[offset : offset + limit],
                "next_cursor": str(offset + limit) if offset + limit < len(rows) else None,
            }
        if command in (Command.ARCHIVE, Command.UNARCHIVE):
            if active or meta.get("active_run") or meta.get("pending_operations"):
                raise RpcError("Cannot archive an active or unconfirmed execution")
            meta["archived"] = command == Command.ARCHIVE
            self._save_meta(ident)
            return {}
        if command == Command.CLOSE:
            if active:
                raise RpcError("Cannot close an active execution; cancel and verify first")
            await w.close()
            self.workers.pop(ident, None)
            self.locks.pop(ident, None)
            return {}
        if command == Command.RESUME:
            if (
                active
                or meta.get("active_run")
                or meta.get("pending_operations")
                or any(r.get("status") == "unknown" for r in meta.get("runs", [])[-1:])
            ):
                raise RpcError("Prior execution outcome is unconfirmed; no replay")
            await w.close()
            cfg = p.get("config") or {}
            model = p.get("model", meta["model"])
            effort = cfg.get("model_reasoning_effort", meta["reasoning_effort"])
            self._selection(model, effort)
            if meta.get("model_binding") != self._binding(meta["model"]):
                raise RpcError("Saved model binding changed; a fresh verified execution context is required")
            meta.update(model=model, model_binding=self._binding(model), reasoning_effort=effort)
            if meta.get("controlled_context"):
                if (
                    cfg.get("controlled_context") != meta["controlled_context"]
                    or not cfg.get("control_token")
                    or not p.get("budget_id")
                ):
                    raise RpcError(
                        "Controlled continuation requires the original context and a new round binding"
                    )
                meta["control_token"] = cfg["control_token"]
                meta["budget_scope"] = p["budget_id"]
            elif cfg.get("controlled_context"):
                raise RpcError("Cannot convert a full-permission session into a controlled context")
            if "mcp_servers" in cfg:
                meta["mcp_servers"] = cfg["mcp_servers"]
            if "runtime_context" in p:
                meta["runtime_context"] = p["runtime_context"]
            self._write_configuration(meta, p.get("instructions"))
            self._save_meta(ident)
            return self._session_reply(meta)
        if command == Command.CONTEXT:
            if active:
                raise RpcError("Context injection requires an idle boundary")
            meta["injected_items"].extend(p.get("items", []))
            if len(json.dumps(meta["injected_items"]).encode()) > 1024 * 1024:
                raise ValueError("Context exceeds 1 MiB")
            self._save_meta(ident)
            if w.process and w.process.returncode is None:
                await w.close()
            return {}
        if command in (Command.CHECKPOINT, Command.SNAPSHOT):
            checkpoint = meta.get("context_checkpoint")
            if command == Command.SNAPSHOT:
                checkpoint = p.get("checkpoint")
            if checkpoint is None:
                if meta.get("context_completed_seen") or (
                    meta.get("context_tracking_version") != 1
                    and (meta.get("runs") or Path(meta["path"]).exists())
                ):
                    # Fresh first rounds (including cancelled ones) are provably
                    # empty at the complete boundary. Legacy/failed persistence is not.
                    raise RpcError("context_checkpoint_unavailable")
                checkpoint = {
                    "id": uuid.uuid5(uuid.NAMESPACE_OID, "pa-context-empty:" + ident).hex,
                    "leaf": None,
                    "inputs": [],
                    "coverage_known": True,
                }
            if command == Command.CHECKPOINT:
                return copy.deepcopy(checkpoint)
            from .context import native_context

            bundle = (
                await native_context(
                    self.pi_binary, action="export", path=meta["path"], leaf=checkpoint["leaf"]
                )
                if checkpoint.get("leaf")
                else {"format": "pi-context-v1", "messages": [], "compacted": False}
            )
            from .context import result_references

            return {
                "checkpoint": checkpoint,
                "content": bundle,
                "compacted": bundle["compacted"],
                "references": result_references(bundle),
            }
        if command == Command.SEED:
            if active or meta.get("runs") or meta.get("context_seed") or Path(meta["path"]).exists():
                raise RpcError("context_seed_requires_new_session")
            from .context import native_context

            if not self.settings.entry(meta["model"]).get("images") and any(
                part.get("type") == "image"
                for message in p["content"]["messages"]
                for part in message.get("content", [])
                if isinstance(part, dict)
            ):
                raise RpcError("context_model_unsupported_images")

            await native_context(
                self.pi_binary,
                action="seed",
                path=meta["path"],
                cwd=meta["cwd"],
                bundle=p["content"],
                additional=p.get("additional", ""),
            )
            meta["context_seed"] = p["snapshot_ref"]
            self._save_meta(ident)
            return {"snapshot_ref": meta["context_seed"]}
        if command in (Command.GOAL_SET, Command.GOAL_GET):
            if command == Command.GOAL_SET:
                meta["goal"] = {
                    **(meta.get("goal") or {}),
                    **{k: p[k] for k in ("objective", "status") if k in p},
                }
                self._save_meta(ident)
            return {"goal": meta.get("goal")}
        if command == Command.OPERATIONS:
            return {
                "data": [{"activity_id": k, "process_id": k} for k in meta.get("pending_operations", {})],
                "next_cursor": None,
            }
        if command == Command.STOP_OPERATION:
            raise RpcError("Independent tool termination is not supported; cancel and verify the run")
        if command == Command.MCP_INSPECT:
            return await self._inspect(w, p.get("serverName"))
        if command == Command.START:
            if (
                active
                or meta.get("active_run")
                or meta.get("pending_operations")
                or any(r.get("status") == "unknown" for r in meta.get("runs", [])[-1:])
            ):
                raise RpcError("Prior execution active or unknown; no new run started")
            retry_of = p.get("retry_of")
            prior = meta.get("runs", [])[-1] if meta.get("runs") else None
            if retry_of:
                if (
                    not prior
                    or prior["id"] != retry_of
                    or prior.get("status") != "failed"
                    or not prior.get("failure", {}).get("retry_allowed")
                ):
                    raise RpcError("Failed execution is not a current safe recovery checkpoint")
                if any(i.get("tool_outcome", {}).get("outcome") == "unknown" for i in prior.get("items", [])):
                    raise RpcError("Unknown tool outcome requires reconciliation")
            model, effort = p.get("model", meta["model"]), p.get("effort", meta["reasoning_effort"])
            if p.get("expected_model") is not None:
                from assistant_runtime.api.models import CapabilityRequestError

                self.settings.require_route(p["expected_model"], model)
                if (
                    p.get("execution_context", {}).get("source") == "conversation"
                    and self.admission
                    and self.admission.service.default_model != model
                ):
                    raise CapabilityRequestError(
                        "model_binding_changed", "The default conversation model has changed"
                    )
            self._selection(model, effort)
            meta["recover_user_input"] = p.get("execution_context", {}).get("source") == "task"
            if any(
                i.get("type") in {"image", "localImage"} for i in p.get("input", [])
            ) and not self.settings.entry(model).get("images"):
                raise ValueError("Execution model does not support images")
            text, images = convert_inputs(p.get("input", []))
            # Restart at a verified idle boundary. Pi reports the selected model/effort before prompt.
            if (model, effort) != (meta["model"], meta["reasoning_effort"]) or meta.get(
                "model_binding"
            ) != self._binding(model):
                await w.close()
                meta.update(model=model, model_binding=self._binding(model), reasoning_effort=effort)
            await w.start()
            state = await w.command({"type": "get_state"})
            actual = state.get("model") or {}
            if actual.get("id") != model or state.get("thinkingLevel") != THINKING_MAP[effort]:
                raise RpcError("Pi resolved a different model or reasoning effort")
            if p.get("expected_model") is not None:
                self.settings.require_route(p["expected_model"], model)
            run_id = uuid.uuid4().hex
            w.active_turn, w.turn_aborted, w.turn_error = run_id, False, None
            w.context_boundary = None
            meta["context_input_id"] = p.get("context_input_id")
            if not meta.get("context_input_id"):
                meta["context_coverage_known"] = False
            w.compaction_error = False
            w.model_failure = None
            w.turn_items, w.pending_msg, w.pending_tools = [], None, {}
            meta["budget_id"] = prior["budget_id"] if retry_of else meta.get("budget_scope") or run_id
            from assistant_runtime.api.ai_usage import UsageScope

            existing_scope = await self.usage.binding(meta["budget_id"])
            if not existing_scope and not retry_of and meta.get("context_input_id"):
                existing_scope = await self.usage.binding(meta["context_input_id"])
            if existing_scope:
                meta["budget_id"] = existing_scope.work_id
            else:
                await self.usage.bind(meta["budget_id"], UsageScope(meta["budget_id"], "foreground"))
            self.last_work_id = meta["budget_id"]
            started_at = time.time()
            route = self.settings.route(model)
            meta["active_execution_facts"] = {
                "route": route,
                "reasoning_effort": effort,
                "started_at": started_at,
            }
            meta["active_run"] = run_id
            meta["retry_of"] = retry_of
            meta["public_inputs"] = {}
            user_item = {
                "id": run_id + "-input",
                "type": "input",
                "content": [{"type": "input_text", "text": text}],
            }
            w.turn_items.append(user_item)
            meta["current_items"] = list(w.turn_items)
            self._save_meta(ident)
            await self.events.put(
                {"kind": "run.started", "data": {"session_id": ident, "run": {"id": run_id}}}
            )
            await self.events.put(
                {
                    "kind": "activity.completed",
                    "data": {"session_id": ident, "run_id": run_id, "item": user_item},
                }
            )
            try:
                await w.command(
                    {"type": "prompt", "message": text, **({"images": images} if images else {})},
                    timeout=self.settings.outer_timeout_seconds,
                )
            except BaseException:
                # Submission may have reached the engine. Leave the durable run unknown.
                raise
            return {
                "run": {"id": run_id},
                "model": model,
                "reasoning_effort": effort,
                "route": route,
                "started_at": started_at,
            }
        if command == Command.STEER:
            if not active or p.get("expected_run_id", w.active_turn) != w.active_turn:
                raise RpcError("No matching active run")
            text, images = convert_inputs(p.get("input", []))
            await w.command({"type": "steer", "message": text, **({"images": images} if images else {})})
            return {}
        if command == Command.CANCEL:
            expected = w.active_turn or meta.get("active_run")
            if p.get("run_id") and expected and p["run_id"] != expected:
                raise RpcError("Cancellation belongs to another run")
            if not active:
                from .waiting import pending

                checkpoint = pending(w)
                if checkpoint and set(meta.get("pending_operations", {})) == {checkpoint["call_id"]}:
                    checkpoint["state"] = "canceled"
                    meta["pending_operations"].pop(checkpoint["call_id"])
                    if self.admission:
                        self.admission.operation(ident, "tool", checkpoint["call_id"], "failed")
                    w.turn_items = copy.deepcopy(meta.get("current_items", []))
                    await w._finalize_turn(checkpoint["turn_id"], "interrupted")
                    return {"state": "stopped"}
                if meta.get("active_run") or meta.get("pending_operations"):
                    raise RpcError("Execution outcome unknown; cancellation cannot be confirmed")
                return {}
            if meta.get("input_checkpoint"):
                meta["input_checkpoint"]["state"] = "canceled"
                self._save_meta(ident)
            w.turn_aborted = True
            if self.shared_tools:
                self.shared_tools.cancel(w)
            await w.command({"type": "clear_queue"}, timeout=5)
            await w.command({"type": "abort_bash"}, timeout=5)
            await w.command({"type": "abort"}, timeout=10)
            async with asyncio.timeout(10):
                while w.active_turn:
                    await asyncio.sleep(0.02)
            if meta.get("pending_operations"):
                raise RpcError("Tools still active or unknown after cancellation")
            return {"state": "stopped"}
        raise RpcError("Unsupported executor command")

    async def _inspect(self, worker, server):
        from fastmcp import Client

        token = await self.prepare_tools(worker, inspect=True)
        rows = []
        for name, spec in worker.meta["mcp_servers"].items():
            if server and name != server:
                continue
            if not spec.get("enabled", True):
                rows.append({"name": name, "runtimeStatus": "disabled", "tools": {}})
                continue
            entry = to_pi_mcp_json({name: spec}, shared_url=self.shared_tools.url if token else None)[
                "mcpServers"
            ][name]
            transport = {
                k: v for k, v in entry.items() if k in {"command", "args", "cwd", "env", "url", "headers"}
            }
            if "command" in transport:
                transport["env"] = {**os.environ, **self.environment, **transport.get("env", {})}
            if "headers" in transport:
                environment = {**os.environ, **self.environment}
                if spec.get("local_service"):
                    environment["PA_SHARED_TOOL_TOKEN"] = token
                for header, value in transport["headers"].items():
                    for key, secret in environment.items():
                        value = value.replace("${" + key + "}", secret)
                    transport["headers"][header] = value
            try:
                startup_timeout = spec.get("startup_timeout_sec", 60)
                async with Client(
                    {"mcpServers": {name: transport}}, timeout=startup_timeout, init_timeout=startup_timeout
                ) as client:
                    tools = await asyncio.wait_for(client.list_tools(), startup_timeout)
                rows.append(
                    {
                        "name": name,
                        "runtimeStatus": "connected",
                        "authStatus": "unsupported",
                        "toolsError": None,
                        "tools": {
                            t.name: {"description": t.description, "inputSchema": t.input_schema}
                            for t in tools
                        },
                    }
                )
            except Exception as exc:
                rows.append(
                    {
                        "name": name,
                        "runtimeStatus": "failed",
                        "authStatus": "unsupported",
                        "toolsError": type(exc).__name__,
                        "tools": {},
                    }
                )
        return {"data": rows}

    async def close(self):
        self.closing = True
        self.started = False
        await asyncio.gather(*(w.close() for w in list(self.workers.values())), return_exceptions=True)
        self.workers.clear()
        self.ui_requests.clear()
        try:
            if self.shared_tools:
                await self.shared_tools.close()
        finally:
            await self.transport.close()
