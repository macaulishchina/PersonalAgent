"""Authenticated Pi transport; accounting and policy belong to ai_usage."""

from aiohttp import web


class ExecutionTransport:
    def __init__(self, client):
        self.client = client
        self.runner = None
        self.url = None
        self.pending = 0

    async def start(self):
        app = web.Application(client_max_size=96 * 1024 * 1024)
        app.router.add_post("/v1/native", self.forward)
        app.router.add_post("/v1/resources", self.resources)
        app.router.add_post("/v1/tool-result", self.tool_result)
        app.router.add_post("/v1/result-read", self.result_read)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=2, handler_cancellation=True)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1"

    def authenticate(self, request):
        from hmac import compare_digest

        key = self.client.environment.get("PA_EXECUTION_TOKEN", "")
        if not key or not compare_digest(request.headers.get("Authorization", ""), "Bearer " + key):
            raise web.HTTPUnauthorized()
        worker = self.client.workers.get(request.headers.get("x-assistant-session"))
        if (
            not worker
            or not (worker.meta.get("active_run") or worker.compacting)
            or not worker.meta.get("budget_id")
        ):
            raise web.HTTPForbidden()
        if worker.meta.get("controlled_context"):
            token = worker.meta.get("control_token", "")
            if not token or not compare_digest(request.headers.get("x-assistant-control", ""), token):
                raise web.HTTPForbidden()
        return worker

    async def tool_result(self, request):
        worker = self.authenticate(request)
        if worker.meta.get("controlled_context") or not self.client.admission:
            raise web.HTTPForbidden()
        payload = await request.json()
        if (
            not isinstance(payload, dict)
            or set(payload) != {"call_id", "payload", "images"}
            or not isinstance(payload["payload"], dict)
            or not isinstance(payload["call_id"], str)
            or len(payload["call_id"]) > 200
        ):
            raise web.HTTPBadRequest()
        if payload["call_id"] not in worker.meta.get("pending_operations", {}):
            raise web.HTTPConflict(reason="Tool call is not active")
        turn = worker.active_turn
        try:
            receipt = await self.client.admission.service.register_tool_result(
                worker.execution_session_id, turn, payload["call_id"], payload["payload"], payload["images"]
            )
            if self.authenticate(request) is not worker or worker.active_turn != turn:
                raise web.HTTPConflict()
            return web.json_response(receipt)
        except PermissionError:
            raise web.HTTPForbidden() from None
        except (ValueError, OSError):
            return web.json_response({"error": "result_registration_failed"}, status=400)

    async def result_read(self, request):
        from assistant_runtime.api.files import FileRequest
        from assistant_runtime.api.results import ResultAccessError

        worker = self.authenticate(request)
        if worker.meta.get("controlled_context") or not self.client.admission:
            raise web.HTTPForbidden()
        payload = await request.json()
        if (
            not isinstance(payload, dict)
            or set(payload) != {"action", "arguments"}
            or payload["action"] not in {"result_read", "result_get"}
        ):
            raise web.HTTPBadRequest()
        service = self.client.admission.service
        ident = service.sessions.get(worker.execution_session_id)
        if not ident:
            raise web.HTTPForbidden()
        owner = service.records.get(ident)["owner"]
        arguments = payload["arguments"]
        if not isinstance(arguments, dict):
            raise web.HTTPBadRequest()
        turn = worker.active_turn
        length = arguments.get("length", 6000)
        if (
            arguments.get("format", "text") not in {"text", "json"}
            or type(length) is not int
            or not 1 <= length <= 6000
        ):
            raise web.HTTPBadRequest()
        try:
            result = await service.files.results.request(
                FileRequest(payload["action"], arguments), owner=owner, model_limit=6000
            )
            if self.authenticate(request) is not worker or worker.active_turn != turn:
                raise web.HTTPConflict()
            return web.json_response(result)
        except ResultAccessError as error:
            return web.json_response(
                {"error": error.code, "tool_outcome": error.tool_failure().outcome.public()}, status=400
            )

    async def resources(self, request):
        worker = self.authenticate(request)
        if not worker.meta.get("controlled_context") or not self.client.admission:
            raise web.HTTPForbidden()
        payload = await request.json()
        if not isinstance(payload, dict) or set(payload) != {"operation_id", "action", "arguments"}:
            raise web.HTTPBadRequest()
        self.authenticate(request)
        service = self.client.admission.service
        execution = service.sessions.get(worker.execution_session_id)
        if execution is None:
            raise web.HTTPForbidden()
        try:
            result = await service.broker.request(
                execution, payload["operation_id"], payload["action"], payload["arguments"]
            )
            return web.json_response(result)
        except PermissionError as error:
            return web.json_response({"error": str(error)}, status=403)
        except (ValueError, FileNotFoundError) as error:
            return web.json_response({"error": str(error)}, status=400)

    async def forward(self, request):
        worker = self.authenticate(request)
        self.pending += 1
        from assistant_runtime.api.capacity import current_capacity_lease

        capacity = self.client.capacity
        admission = self.client.admission
        ident = admission.service.sessions.get(worker.execution_session_id) if admission else None
        lease = admission.slots.leases.get(ident) if admission else None
        token = current_capacity_lease.set(lease)
        try:
            async with capacity.inference():
                if self.authenticate(request) is not worker:
                    raise web.HTTPForbidden()
                return await self._native(request, worker)
        finally:
            current_capacity_lease.reset(token)
            self.pending -= 1

    async def close(self):
        if self.client.accounts:
            await self.client.accounts.close()
        if self.runner:
            await self.runner.cleanup()

    async def _native(self, request, worker):
        from assistant_runtime.core.pi.native_transport import forward

        return await forward(self, request, worker)
