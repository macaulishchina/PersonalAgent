"""Authenticated Pi transport; accounting and policy belong to ai_usage."""

import asyncio

from aiohttp import web


class ExecutionTransport:
    def __init__(self, client):
        self.client = client
        self.runner = None
        self.url = None
        self.pending = 0
        self.slots = asyncio.Semaphore(client.settings.concurrency)

    async def start(self):
        app = web.Application(client_max_size=32 * 1024 * 1024)
        app.router.add_post("/v1/native", self.forward)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=2, handler_cancellation=True)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1"

    async def forward(self, request):
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
        if self.pending >= self.client.settings.queue_size + self.client.settings.concurrency:
            raise web.HTTPServiceUnavailable()
        self.pending += 1
        try:
            async with self.slots:
                return await self._native(request, worker)
        finally:
            self.pending -= 1

    async def close(self):
        if self.client.accounts:
            await self.client.accounts.close()
        if self.runner:
            await self.runner.cleanup()

    async def _native(self, request, worker):
        from assistant_runtime.core.pi.native_transport import forward

        return await forward(self, request, worker)
