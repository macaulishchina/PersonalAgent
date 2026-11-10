"""Instance-local MCP projections with execution-bound request context."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from importlib import import_module
import logging
import secrets
import socket
from typing import TYPE_CHECKING

import uvicorn
from fastmcp.server.middleware import Middleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response

from assistant_runtime.api.tool_calls import ToolFailure, ToolOutcome
from assistant_runtime.core.plugin_catalog import services
from assistant_runtime.core.pi.mcp import service_owner
from assistant_runtime.mcp.client import LocalRuntimeClient
from assistant_runtime.mcp.outcomes import ToolOutcomes

if TYPE_CHECKING:
    from assistant_runtime.core.pi.rpc import PiSessionWorker

log = logging.getLogger(__name__)
binding = ContextVar("shared_tool_binding")


def rejected(code, stage, message):
    return ToolFailure(ToolOutcome(code, stage, "not_dispatched", "failed", message))


@dataclass(frozen=True)
class Access:
    worker: "PiSessionWorker"
    spec: dict
    turn: str | None

    def allows(self, name):
        allowed = self.spec.get("enabled_tools")
        return (
            self.spec.get("default_tools_approval_mode", "approve") == "approve"
            and (allowed is None or name in allowed)
            and name not in self.spec.get("disabled_tools", ())
            and self.spec.get("tools", {}).get(name, {}).get("approval_mode", "approve") == "approve"
        )

    def require_active(self):
        if not self.turn or self.worker.active_turn != self.turn or self.worker.turn_aborted:
            raise rejected(
                "tool_execution_inactive", "preparation", "This tool execution is no longer active"
            )


class ScopedTools(Middleware):
    async def on_list_tools(self, context, call_next):
        access = binding.get()
        return [tool for tool in await call_next(context) if access.allows(tool.name)]

    @staticmethod
    def admit(name):
        access = binding.get()
        access.require_active()
        if not access.allows(name):
            raise rejected(
                "tool_permission_denied", "authorization", "Tool is outside this execution's scope"
            )


class BoundClient(LocalRuntimeClient):
    @property
    def caller_run(self):
        return binding.get().spec.get("task_run")

    async def request(self, payload):
        access = binding.get()
        request = dict(payload)
        if request["command"] != "plugin-tools":
            access.require_active()
            request["execution_turn"] = access.turn
        if "caller_run" in request:
            request["caller_run"] = self.caller_run
        client = LocalRuntimeClient(
            access.spec["socket"], execution_session=access.worker.execution_session_id
        )
        return await client.request(request)


class LoopbackServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The application host owns process signals.
        yield


class SharedTools:
    MAX_REQUESTS = 256
    MAX_REQUESTS_PER_WORKER = 32
    MAX_BODY = 128 * 1024

    def __init__(self, client):
        self.client = client
        self.url = None
        self.lock = asyncio.Lock()
        self.tokens = {}
        self.inflight = {}
        self.apps = {}
        self.lifespans = []
        self.stopping = asyncio.Event()
        self.server = self.listener = self.task = None
        self.pending = 0
        self.pending_by_worker = {}

    async def prepare(self, names):
        if not names:
            return
        async with self.lock:
            if not self.url:
                await self._start()
            for name in names:
                if name not in self.apps:
                    await self._prepare_app(name)

    async def _start(self):
        self.stopping.clear()
        listener = socket.socket()
        self.listener = listener
        try:
            listener.bind(("127.0.0.1", 0))
            self.server = LoopbackServer(
                uvicorn.Config(
                    self,
                    lifespan="off",
                    ws="none",
                    log_config=None,
                    access_log=False,
                    timeout_graceful_shutdown=2,
                    http="h11",
                )
            )
            self.task = asyncio.create_task(self.server.serve(sockets=[listener]), name="shared-mcp")
            async with asyncio.timeout(5):
                while not self.server.started:
                    if self.task.done():
                        self.task.result()
                        raise RuntimeError("Shared MCP listener did not start")
                    await asyncio.sleep(0.01)
            self.url = f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
        except BaseException:
            await self._close()
            raise

    async def _prepare_app(self, name):
        client = BoundClient("")
        if name == "extensions":
            from assistant_runtime.mcp.extensions import create_remote_server

            server = create_remote_server(client)
        else:
            descriptor = services((service_owner(name),))[name]
            module, factory = descriptor.tools_factory.split(":")
            server = getattr(import_module(module), factory)(client)
        server.add_middleware(ToolOutcomes(admit=ScopedTools.admit))
        server.add_middleware(ScopedTools())
        app = server.http_app(
            path=f"/mcp/{name}", stateless_http=True, json_response=True, host_origin_protection=True
        )
        ready = asyncio.get_running_loop().create_future()

        async def lifespan():
            try:
                async with app.router.lifespan_context(app) as state:
                    ready.set_result(state or {})
                    await self.stopping.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                else:
                    raise

        task = asyncio.create_task(lifespan(), name=f"shared-mcp:{name}")
        try:
            state = await ready
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        self.lifespans.append(task)
        self.apps[name] = (app, state)

    def issue(self, worker):
        if worker not in self.tokens.values():
            self.tokens[secrets.token_urlsafe(32)] = worker
        return next(token for token, owner in self.tokens.items() if owner is worker)

    def cancel(self, worker):
        for task in tuple(self.inflight.get(worker, ())):
            task.cancel()

    def revoke(self, worker):
        self.cancel(worker)
        for token, owner in list(self.tokens.items()):
            if owner is worker:
                del self.tokens[token]

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        scheme, _, key = request.headers.get("authorization", "").partition(" ")
        worker = self.tokens.get(key) if scheme.lower() == "bearer" else None
        name = request.url.path.removeprefix("/mcp/")
        if not worker or self.client.workers.get(worker.execution_session_id) is not worker:
            await Response(status_code=401)(scope, receive, send)
            return
        spec = worker.meta.get("mcp_servers", {}).get(name, {})
        if (
            request.url.path != f"/mcp/{name}"
            or spec.get("local_service") != name
            or not spec.get("enabled", True)
            or name not in self.apps
        ):
            await Response(status_code=404)(scope, receive, send)
            return
        owned = self.pending_by_worker.get(worker, 0)
        if self.pending >= self.MAX_REQUESTS or owned >= self.MAX_REQUESTS_PER_WORKER:
            await Response(status_code=503)(scope, receive, send)
            return

        access = Access(worker, spec, worker.active_turn)
        self.pending += 1
        self.pending_by_worker[worker] = owned + 1
        try:
            await self._request(request, scope, receive, send, access, key, self.apps[name])
        finally:
            self.pending -= 1
            if self.pending_by_worker[worker] == 1:
                del self.pending_by_worker[worker]
            else:
                self.pending_by_worker[worker] -= 1

    async def _request(self, request, scope, receive, send, access, key, application):
        try:
            async with asyncio.timeout(5):
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > self.MAX_BODY:
                        await Response(status_code=413)(scope, receive, send)
                        return
        except TimeoutError:
            await Response(status_code=408)(scope, receive, send)
            return
        except ClientDisconnect:
            return
        if self.tokens.get(key) is not access.worker:
            await Response(status_code=401)(scope, receive, send)
            return
        app, state = application
        delivered = False
        response_started = False

        async def body_once():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await disconnected

        async def reply(message):
            nonlocal response_started
            response_started |= message["type"] == "http.response.start"
            await send(message)

        async def serve():
            context = binding.set(access)
            try:
                await app({**scope, "state": dict(state)}, body_once, reply)
            finally:
                binding.reset(context)

        disconnected = asyncio.create_task(receive())
        task = asyncio.create_task(serve())
        calls = self.inflight.setdefault(access.worker, set())
        calls.add(task)
        try:
            done, _ = await asyncio.wait((task, disconnected), return_when=asyncio.FIRST_COMPLETED)
            if disconnected not in done:
                if task.cancelled():
                    if not response_started:
                        await Response(status_code=503)(scope, receive, send)
                else:
                    await task
        finally:
            for pending in (task, disconnected):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, disconnected, return_exceptions=True)
            calls.discard(task)
            if not calls:
                self.inflight.pop(access.worker, None)

    async def close(self):
        async with self.lock:
            await self._close()

    async def _close(self):
        for worker in tuple(self.inflight):
            self.cancel(worker)
        self.tokens.clear()
        self.url = None
        if self.server:
            self.server.should_exit = True
        try:
            if self.task:
                await self.task
        finally:
            if self.listener:
                self.listener.close()
            self.stopping.set()
            for result in await asyncio.gather(*self.lifespans, return_exceptions=True):
                if isinstance(result, Exception):
                    log.warning("Shared MCP shutdown failed: %s", type(result).__name__)
            self.lifespans.clear()
            self.apps.clear()
            self.server = self.listener = self.task = None
