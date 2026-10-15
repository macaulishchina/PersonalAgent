"""Bounded local transport; no memory storage or hardware implementation."""

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from typing import Protocol

from ..kernel.contracts import Control, DeviceQuery, MemoryRequest, VideoCapture, CameraMotion, VideoRecord

Request = Control | DeviceQuery | MemoryRequest | VideoCapture | CameraMotion | VideoRecord


class CapabilityClient(Protocol):
    async def invoke(self, request: Request) -> dict: ...
    async def status(self) -> dict: ...


class LocalRuntimeClient:
    def __init__(self, socket: str | Path, timeout: float = 95):
        self.socket, self.timeout = str(socket), timeout

    async def invoke(self, request: Request) -> dict:
        if isinstance(request, MemoryRequest):
            payload = {"command": "memory", **asdict(request)}
        elif isinstance(request, DeviceQuery):
            payload = {"command": "devices", **asdict(request)}
        elif isinstance(request, VideoCapture):
            payload = {"command": "devices", "action": "snapshot", "device_id": request.device_id}
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
