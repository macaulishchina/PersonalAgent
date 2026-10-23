"""Bounded local transport; no memory storage or hardware implementation."""

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from typing import Protocol

from assistant_runtime.api.contracts import (
    Control,
    DeviceQuery,
    MemoryRequest,
    VideoCapture,
    CameraMotion,
    VideoRecord,
)

from assistant_runtime.api.contracts import (
    TaskRequest,
    NotificationRequest,
    DeviceOperationQuery,
    DeviceOperationCancel,
    AudioVolume,
    IntegrationRequest,
    EventRequest,
    MediaRequest,
    SearchRequest,
    DecisionRequest,
    DecisionQuery,
    DecisionOutcome,
    VoiceDecisionRequest,
)

from assistant_runtime.api.models import ModelRequest
from assistant_runtime.api.ai_usage import UsageRequest

Request = (
    UsageRequest
    | ModelRequest
    | VoiceDecisionRequest
    | DecisionOutcome
    | DecisionQuery
    | DecisionRequest
    | MediaRequest
    | SearchRequest
    | EventRequest
    | IntegrationRequest
    | NotificationRequest
    | TaskRequest
    | Control
    | DeviceQuery
    | MemoryRequest
    | VideoCapture
    | CameraMotion
    | VideoRecord
    | DeviceOperationQuery
    | DeviceOperationCancel
    | AudioVolume
)


class CapabilityClient(Protocol):
    async def invoke(self, request: Request) -> dict: ...
    async def status(self) -> dict: ...


class LocalRuntimeClient:
    def __init__(self, socket: str | Path, timeout: float = 95, *, execution_session=None):
        self.socket, self.timeout = str(socket), timeout
        self.execution_session = execution_session

    async def invoke(self, request: Request) -> dict:
        if isinstance(request, UsageRequest):
            payload = {"command": "ai-usage", **asdict(request)}
        elif isinstance(request, ModelRequest):
            payload = {"command": "models", **asdict(request)}
        elif isinstance(request, VoiceDecisionRequest):
            payload = {"command": "voice-decision", **asdict(request)}
        elif isinstance(request, DecisionOutcome):
            payload = {"command": "decisions", "action": "outcome", "arguments": asdict(request)}
        elif isinstance(request, DecisionQuery):
            payload = {
                "command": "decisions",
                "action": "judge",
                "arguments": {k: v for k, v in asdict(request).items() if k != "caller_run"},
                "caller_run": request.caller_run,
            }
        elif isinstance(request, DecisionRequest):
            payload = {"command": "decisions", **asdict(request)}
        elif isinstance(request, SearchRequest):
            payload = {"command": "search", **asdict(request)}
        elif isinstance(request, MediaRequest):
            payload = {"command": "media", **asdict(request)}
        elif isinstance(request, EventRequest):
            payload = {"command": "events", **asdict(request)}
        elif isinstance(request, IntegrationRequest):
            payload = {"command": "mcp", **asdict(request)}
        elif isinstance(request, NotificationRequest):
            payload = {"command": "notifications", **asdict(request)}
        elif isinstance(request, TaskRequest):
            payload = {"command": "tasks", **asdict(request)}
        elif isinstance(request, MemoryRequest):
            payload = {"command": "memory", **asdict(request)}
        elif isinstance(request, DeviceQuery):
            payload = {"command": "devices", **asdict(request)}
        elif isinstance(request, DeviceOperationQuery):
            payload = {"command": "devices", "action": "operations", **asdict(request)}
        elif isinstance(request, DeviceOperationCancel):
            payload = {"command": "devices", "action": "cancel-operation", **asdict(request)}
        elif isinstance(request, AudioVolume):
            payload = {"command": "devices", "action": "volume", **asdict(request)}
        elif isinstance(request, VideoCapture):
            payload = {"command": "devices", "action": "snapshot", **asdict(request)}
        elif isinstance(request, CameraMotion):
            payload = {"command": "devices", "action": "move", **asdict(request)}
        elif isinstance(request, VideoRecord):
            payload = {"command": "devices", "action": "record", **asdict(request)}
        elif isinstance(request, Control):
            payload = {"command": request.kind, "value": request.value}
        else:
            raise ValueError("Unsupported capability request")
        return await self.request(payload)

    async def status(self) -> dict:
        return await self.request({"command": "status"})

    async def request(self, payload: dict) -> dict:
        if self.execution_session:
            payload = {**payload, "execution_session": self.execution_session}
        wire = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        if len(wire) > 64 * 1024:
            raise ValueError("Capability request exceeds 64 KiB")
        writer = None
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_unix_connection(self.socket, limit=4 * 1024 * 1024)
                writer.write(wire)
                await writer.drain()
                line = await reader.readline()
                if not line:
                    raise RuntimeError("Capability connection closed; outcome unconfirmed")
                reply = json.loads(line)
                if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
                    raise ValueError("Invalid capability response")
                if not reply["ok"]:
                    raise RuntimeError(reply.get("error", "Capability failed"))
                return reply["result"]
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()
