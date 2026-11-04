"""Provider-neutral executor contract. The host owns task policy and durable business state."""

from enum import StrEnum
from typing import Protocol, TypedDict
import asyncio


class Command(StrEnum):
    MODELS = "models.list"
    CONFIGURATION = "configuration.read"
    CREATE = "session.create"
    RESUME = "session.resume"
    READ = "session.read"
    RUNS = "session.runs"
    CLOSE = "session.close"
    ARCHIVE = "session.archive"
    UNARCHIVE = "session.unarchive"
    CONTEXT = "session.context"
    START = "run.start"
    STEER = "run.steer"
    CANCEL = "run.cancel"
    OPERATIONS = "operations.list"
    STOP_OPERATION = "operations.stop"
    MCP_INSPECT = "mcp.inspect"
    MCP_LOGIN = "mcp.login"
    GOAL_SET = "goal.set"
    GOAL_GET = "goal.get"


class ExecutionEvent(TypedDict):
    kind: str
    data: dict


class Executor(Protocol):
    events: asyncio.Queue[ExecutionEvent]
    started: bool

    async def start(self) -> None: ...
    async def request(self, command: str, arguments: dict, timeout: float | None = None) -> dict:
        """None selects the adapter's command deadline; explicit deadlines remain bounded."""
        ...

    async def respond(self, ident: int, result: dict) -> None: ...
    async def close(self) -> None: ...
