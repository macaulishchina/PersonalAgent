"""Real Pi and Web approval capability with a synthetic device and local model."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from aiohttp import ClientSession, CookieJar, web
from assistant_runtime.api.conversation import ConversationAccessRequest
from assistant_runtime.app.config import Config
from assistant_runtime.app.initialize import initialize
from assistant_runtime.app.host import AssistantService
from assistant_runtime.core.configuration.chat_gateway import ChatGateway
from pi_gateway import FakeResponsesGateway, sse_text_reply
from test_pi_skeleton import configuration


class WebAccessNativeTest(unittest.IsolatedAsyncioTestCase):
    async def test_both_modes_share_real_pi_context_and_survive_restart_without_replay(self):
        model = FakeResponsesGateway(lambda step, body: sse_text_reply("access-" + str(step), "访问验证完成"))
        host = None
        running = None
        runner = None
        gateway = None
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = replace(
                Config.load(initialize(root / "instance", preset="slim")),
                main_model="model-a",
                main_reasoning_effort="low",
                workspace=str(root),
                model_api=configuration(model.base_url),
                web={"enable": True, "host": "127.0.0.1", "port": 0},
            )
            config = replace(config, plugins=[*config.plugins, "web"])
            (config.root / ".env").write_text("TEST_KEY=synthetic-key\n")
            try:
                host = AssistantService(config)
                running = asyncio.create_task(host.run())
                await asyncio.wait_for(host.runtime.ready.wait(), 30)

                async def endpoint(_):
                    return host.http_frontends["web"].endpoint

                gateway = ChatGateway(
                    SimpleNamespace(process=SimpleNamespace(returncode=None), request=endpoint)
                )

                @web.middleware
                async def routing(request, handler):
                    request["gateway_scheme"] = "http"
                    return await handler(request)

                app = web.Application(middlewares=[routing])
                app.router.add_route("*", "/chat/{path:.*}", gateway.forward)
                runner = web.AppRunner(app, shutdown_timeout=1)
                await runner.setup()
                site = web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

                async def manage(action, args=None):
                    return await host.http_frontends["web"].management(action, args or {})

                session = await manage("create_session", {"title": "访问授权原生验收"})
                entry = await manage(
                    "create_entrance", {"session_id": session["id"], "endpoint_id": "mock:native"}
                )
                base = origin + entry["path"].rstrip("/")
                async with ClientSession(
                    cookie_jar=CookieJar(unsafe=True), headers={"Origin": origin}
                ) as client:
                    response = await client.post(base + "/api/request", json={})
                    self.assertEqual(response.status, 202)
                    item = (await response.json())["request"]
                    pending = await host.runtime.invoke(ConversationAccessRequest("pending", "mock:native"))
                    self.assertEqual(pending["requests"][0]["request_id"], item["id"])
                    await host.runtime.invoke(
                        ConversationAccessRequest(
                            "decide",
                            "mock:native",
                            {"request_id": item["id"], "decision": "approve", "seconds": 600},
                        )
                    )
                    response = await client.post(base + "/api/claim", json={})
                    self.assertEqual(response.status, 200)
                    visitor = origin + (await response.json())["path"].rstrip("/")

                    async def send(where, text):
                        ident = str(uuid.uuid4())
                        body = {"request_id": ident, "text": text}
                        for _ in range(2):
                            response = await client.post(where + "/api/messages", json=body)
                            self.assertEqual(response.status, 202, await response.text())
                        async with asyncio.timeout(25):
                            while True:
                                chat = await host.conversations.get(session["id"])
                                if not chat.queue() and any(
                                    m["role"] == "user" and m["text"] == text
                                    for m in chat.store.history(session["id"])
                                ):
                                    break
                                await asyncio.sleep(0.05)

                    await send(visitor, "ACCESS_NATIVE_FIRST")
                    temporary = next(
                        a for a in (await manage("accesses"))["accesses"] if a["kind"] == "temporary"
                    )
                    await manage("close_access", {"id": temporary["id"], "kind": "temporary"})
                    self.assertEqual((await client.get(visitor + "/api/messages")).status, 401)
                    owner_token = (config.state_dir / "web-access-token").read_text().strip()
                    self.assertEqual(
                        (await client.post(origin + "/chat/api/login", json={"token": owner_token})).status,
                        200,
                    )
                    response = await client.post(base + "/api/account", json={})
                    self.assertEqual(response.status, 200)
                    account = origin + (await response.json())["path"].rstrip("/")
                    await send(account, "ACCESS_NATIVE_SECOND")
                    self.assertEqual(len(model.requests), 2)
                    self.assertIn("ACCESS_NATIVE_FIRST", json.dumps(model.requests[1]))
                    self.assertIn("ACCESS_NATIVE_SECOND", json.dumps(model.requests[1]))
                    host.stop_event.set()
                    await asyncio.wait_for(running, 20)
                    host = AssistantService(config)
                    running = asyncio.create_task(host.run())
                    await asyncio.wait_for(host.runtime.ready.wait(), 30)
                    self.assertEqual((await client.get(account + "/api/messages")).status, 200)
                    self.assertEqual(len(model.requests), 2)
                    root_login = next(
                        a for a in (await manage("accesses"))["accesses"] if a["kind"] == "account_login"
                    )
                    await manage("close_access", {"id": root_login["id"], "kind": "account_login"})
                    self.assertEqual((await client.get(account + "/api/session")).status, 401)
            finally:
                if host and running:
                    host.stop_event.set()
                    await asyncio.wait_for(running, 20)
                if runner:
                    await runner.cleanup()
                if gateway:
                    await gateway.close()
                model.close()
