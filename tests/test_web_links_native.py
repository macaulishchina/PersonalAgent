"""Real manager, Web and Pi with an isolated synthetic model; no PP or paid API."""

import asyncio
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from aiohttp import ClientSession, CookieJar
import tomlkit

from assistant_runtime.app.config import Config
from assistant_runtime.app.initialize import initialize
from assistant_runtime.app.management import BusinessProcess
from assistant_runtime.core.configuration.console import Console
from assistant_runtime.core.configuration.settings import ManagementSettings
from pi_gateway import FakeResponsesGateway, sse_text_reply
from test_pi_skeleton import configuration


class WebLinksNativeTest(unittest.IsolatedAsyncioTestCase):
    async def test_manager_authority_real_pi_context_restart_and_revocation(self):
        gateway = FakeResponsesGateway(
            lambda step, body: sse_text_reply("reply-" + str(step), "独立会话验证通过")
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            config = replace(
                Config.load(initialize(root / "instance", preset="slim")),
                main_model="model-a",
                main_reasoning_effort="low",
                workspace=str(root),
                model_api=configuration(gateway.base_url),
                web={"enable": True, "host": "127.0.0.1", "port": 0},
            )
            config = replace(config, plugins=[*config.plugins, "web"])
            (config.root / ".env").write_text("TEST_KEY=synthetic-key\n", encoding="utf-8")
            entry = config.root / "acceptance.toml"
            data = asdict(config)
            data["root"] = str(config.root)
            entry.write_text(tomlkit.dumps(data), encoding="utf-8")
            business = BusinessProcess(
                ManagementSettings(
                    config.root, entry, host="127.0.0.1", port=0, startup_seconds=30, shutdown_seconds=10
                )
            )
            console = Console(business)
            try:
                await business.start()
                self.assertIn(business.outcome, {"running", "degraded"}, business.error)
                await console.start()
                origin = f"http://127.0.0.1:{console.site._server.sockets[0].getsockname()[1]}"
                async with (
                    ClientSession(cookie_jar=CookieJar(unsafe=True), headers={"Origin": origin}) as admin,
                    ClientSession(cookie_jar=CookieJar(unsafe=True), headers={"Origin": origin}) as browser,
                ):
                    management = origin + "/api/entrances/web"
                    response = await browser.post(management, json={"action": "sessions", "arguments": {}})
                    self.assertEqual(response.status, 401)
                    response = await admin.post(
                        origin + "/api/login",
                        json={
                            "token": console.access.path.read_text().strip(),
                        },
                    )
                    self.assertEqual(response.status, 200)

                    async def manage(action, arguments=None):
                        response = await admin.post(
                            management, json={"action": action, "arguments": arguments or {}}
                        )
                        self.assertEqual(response.status, 200, await response.text())
                        return await response.json()

                    session = await manage("create_session", {"title": "原生链路独立验证"})
                    link = await manage("create_link", {"session_id": session["id"]})
                    response = await browser.post(
                        origin + "/chat/c/" + link["id"] + "/api/link",
                        json={"token": link["path"].split("#")[1]},
                    )
                    self.assertEqual(response.status, 200)
                    base = origin + (await response.json())["path"].rstrip("/")
                    self.assertEqual((await browser.get(origin + "/api/configuration")).status, 401)
                    self.assertEqual((await browser.get(origin + "/chat/api/messages")).status, 401)
                    ids = []
                    for text in ("LINK_NATIVE_FIRST", "LINK_NATIVE_SECOND"):
                        request_id = str(uuid.uuid4())
                        ids.append(request_id)
                        body = {"request_id": request_id, "text": text}
                        response = await browser.post(base + "/api/messages", json=body)
                        self.assertEqual(response.status, 202, await response.text())
                        # Lost acknowledgement recovery must not dispatch a second operation.
                        self.assertEqual((await browser.post(base + "/api/messages", json=body)).status, 202)
                        async with asyncio.timeout(30):
                            while True:
                                response = await browser.get(base + "/api/messages")
                                history = await response.json()
                                if any(
                                    m["role"] == "assistant" and m["status"] == "complete"
                                    for m in history["messages"]
                                    if m.get("turn_id")
                                    == next(
                                        (
                                            u.get("turn_id")
                                            for u in history["messages"]
                                            if u["id"] == "web:" + request_id
                                        ),
                                        None,
                                    )
                                ):
                                    break
                                await asyncio.sleep(0.05)
                    self.assertEqual(len(gateway.requests), 2)
                    self.assertIn("LINK_NATIVE_FIRST", json.dumps(gateway.requests[1]))
                    self.assertIn("LINK_NATIVE_SECOND", json.dumps(gateway.requests[1]))
                    self.assertEqual(gateway.requests[1]["model"], "model-a")
                    self.assertEqual({m["session_id"] for m in history["messages"]}, {session["id"]})
                    await business.stop()
                    await business.start()
                    self.assertIn(business.outcome, {"running", "degraded"}, business.error)
                    response = await browser.get(base + "/api/messages")
                    self.assertEqual(response.status, 200, await response.text())
                    self.assertEqual(len(gateway.requests), 2)  # Viewing never replays work.
                    await manage("revoke_link", {"id": link["id"]})
                    self.assertEqual((await browser.get(base + "/api/messages")).status, 401)
                    self.assertEqual((await admin.get(origin + "/api/configuration")).status, 200)
            finally:
                await business.stop()
                await console.close()
                gateway.close()
