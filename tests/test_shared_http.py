"""One public listener, independent management, optional chat in a real worker."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp import ClientSession, CookieJar

from assistant_runtime.app.management import BusinessProcess
from assistant_runtime.core.configuration.console import Console
from assistant_runtime.core.configuration.settings import ManagementSettings


class SharedHttpTest(unittest.IsolatedAsyncioTestCase):
    async def test_optional_chat_auth_lifecycle_and_management_survives_worker_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            entry = root / "config.toml"
            entry.write_text("root=" + json.dumps(str(root)) + "\nplugins=[]\n", encoding="utf-8")
            business = BusinessProcess(
                ManagementSettings(
                    root, entry, host="127.0.0.1", port=0, startup_seconds=20, shutdown_seconds=8
                )
            )
            console = Console(business)
            await business.start()
            await console.start()
            origin = f"http://127.0.0.1:{console.site._server.sockets[0].getsockname()[1]}"
            try:
                async with ClientSession(
                    cookie_jar=CookieJar(unsafe=True), headers={"Origin": origin}
                ) as client:

                    async def get(path):
                        return await client.get(origin + path)

                    self.assertEqual((await get("/")).status, 404)
                    configuration = await get("/config")
                    self.assertEqual(configuration.status, 200)
                    self.assertEqual(configuration.url.path, "/config/")
                    self.assertIn("登录系统配置", await configuration.text())
                    self.assertEqual((await get("/chat/")).status, 404)
                    self.assertEqual((await get("/api/configuration")).status, 401)
                    token = console.access.path.read_text().strip()
                    self.assertEqual(
                        (await client.post(origin + "/api/login", json={"token": token})).status, 200
                    )
                    self.assertEqual((await get("/api/configuration")).status, 200)
                    await business.stop()
                    entry.write_text(
                        "root="
                        + json.dumps(str(root))
                        + '\nplugins=["ai_usage","workfiles","models","pi","execution","conversation","web"]\n[web]\nenable=true\n',
                        encoding="utf-8",
                    )
                    business.resources.provision(business.files.snapshot().contents)
                    await business.start(business.files.snapshot())
                    self.assertIn(business.outcome, {"running", "degraded"}, business.error)
                    chat = await get("/")
                    self.assertEqual(chat.status, 200)
                    self.assertEqual(chat.url.path, "/")
                    self.assertEqual(await chat.text(), await (await get("/chat/")).text())
                    self.assertEqual((await get("/config/")).status, 200)
                    self.assertEqual(
                        (await client.get(origin + "/", headers={"Origin": "http://evil.test"})).status,
                        403,
                    )
                    self.assertEqual((await get("/chat/api/messages")).status, 401)  # Admin != chat.
                    with business.files.locked():
                        snapshot = business.files.snapshot()
                    credential = snapshot.resources["web_access"].decode().strip()
                    login = await client.post(origin + "/chat/api/login", json={"token": credential})
                    self.assertEqual(login.status, 200)
                    self.assertEqual(login.cookies["personal_agent_session"]["path"], "/chat/")
                    self.assertEqual((await get("/chat/api/messages")).status, 200)
                    descriptor = await business.request({"command": "http-frontend", "owner": "web"})
                    async with ClientSession() as raw:
                        port = descriptor["port"]
                        self.assertEqual((await raw.get(f"http://127.0.0.1:{port}/")).status, 403)
                        chat_cookies = {
                            "personal_agent_session": client.cookie_jar.filter_cookies(origin + "/chat/")[
                                "personal_agent_session"
                            ].value
                        }
                        self.assertEqual(
                            (await raw.get(origin + "/api/configuration", cookies=chat_cookies)).status, 401
                        )
                    # /chat never grants admin access, even to a logged-in chat user.
                    bad = await client.post(
                        origin + "/chat/api/logout", json={}, headers={"Origin": "http://evil.test"}
                    )
                    self.assertEqual(bad.status, 403)
                    await console.save_flow.save(
                        snapshot.version, [{"path": ["web", "enable"], "op": "set", "value": False}], []
                    )
                    await asyncio.wait_for(business.apply_task, 15)
                    self.assertEqual(business.application["phase"], "applied", business.application)
                    self.assertEqual((await get("/chat/")).status, 404)
                    self.assertEqual((await get("/")).status, 404)
                    self.assertEqual((await get("/config/")).status, 200)
                    self.assertEqual((await get("/api/configuration")).status, 200)
                    await business.stop()
                    self.assertEqual((await get("/chat/")).status, 503)
                    self.assertEqual((await get("/")).status, 503)
                    self.assertEqual((await get("/config/")).status, 200)
                    self.assertEqual((await get("/api/configuration")).status, 200)
            finally:
                await business.stop()
                await console.close()
