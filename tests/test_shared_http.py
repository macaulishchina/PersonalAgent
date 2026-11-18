"""One public listener, independent management, optional chat in a real worker."""

import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from aiohttp import ClientError, ClientSession, CookieJar

from assistant_runtime.app.management import BusinessProcess
from assistant_runtime.core.configuration.client import operate
from assistant_runtime.core.configuration.console import Console, TLS_CA_PATH
from assistant_runtime.core.configuration.settings import ManagementSettings


class SharedHttpTest(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(shutil.which("openssl"), "requires OpenSSL to generate a temporary certificate")
    async def test_switches_protocol_on_same_port_and_keeps_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            entry = root / "config.toml"
            entry.write_text("plugins=[]\n", encoding="utf-8")
            await asyncio.to_thread(
                subprocess.run,
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=127.0.0.1",
                    "-addext",
                    "subjectAltName=IP:127.0.0.1",
                    "-out",
                    "test.crt",
                    "-keyout",
                    "test.key",
                ],
                cwd=root,
                check=True,
                capture_output=True,
                timeout=15,
            )
            console = Console(BusinessProcess(ManagementSettings(root, entry, host="127.0.0.1", port=0)))
            await console.start()
            self.addAsyncCleanup(console.close)
            port = console.site._server.sockets[0].getsockname()[1]
            # Record the actual listener port so later updates address the same endpoint.
            from dataclasses import replace

            console.settings = replace(console.settings, port=port)
            bootstrap = root / "management.toml"
            base = (
                f"root={json.dumps(str(root))}\nentry={json.dumps(str(entry))}\n"
                f'host="127.0.0.1"\nport={port}\n'
            )
            origin = f"http://127.0.0.1:{port}"
            secure = f"https://127.0.0.1:{port}"
            async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
                async with client.get(origin + "/config/") as response:
                    self.assertEqual(response.status, 200)
                bootstrap.write_text(base + 'scheme="https"\n', encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "请先在服务器安装"):
                    await console.reconfigure()
                async with client.get(origin + "/config/") as response:
                    self.assertEqual(response.status, 200)

                tls_dir = root / "config/tls"
                tls_dir.mkdir(parents=True)
                (root / "test.crt").rename(tls_dir / "server.crt")
                (root / "test.key").rename(tls_dir / "server-key.pem")
                cert = (tls_dir / "server.crt").read_bytes()
                ca_file = tls_dir / "ca.crt"
                # The download must contain public certificates even if an operator
                # accidentally puts other PEM blocks in the CA file.
                ca_file.write_bytes(cert + (tls_dir / "server-key.pem").read_bytes())
                token = console.access.path.read_text().strip()
                async with client.post(
                    origin + "/api/login", headers={"Origin": origin}, json={"token": token}
                ) as response:
                    self.assertEqual(response.status, 200)
                async with client.get(origin + "/api/configuration") as response:
                    info = await response.json()
                    self.assertEqual(info["management_transport"], {"scheme": "http", "ca_download": None})
                async with client.get(origin + TLS_CA_PATH) as response:
                    self.assertEqual(response.status, 404)
                client.cookie_jar.clear()
                self.assertEqual((await console.reconfigure())["scheme"], "https")
                with self.assertRaises(ClientError):
                    await client.get(origin + "/config/")
                with self.assertRaises(ClientError):
                    await client.get(secure + "/config/")
                tls = console.settings.client_ssl_context()
                async with client.get(secure + TLS_CA_PATH, ssl=tls) as response:
                    self.assertEqual(response.status, 401)
                async with client.post(
                    secure + "/api/login", ssl=tls, headers={"Origin": secure}, json={"token": token}
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.cookies["personal_agent_admin"]["secure"])
                self.assertIn("application", await operate(console.settings, "status"))

                ca_file.unlink()
                async with client.get(secure + "/api/configuration", ssl=tls) as response:
                    info = await response.json()
                    self.assertEqual(
                        info["management_transport"], {"scheme": "https", "ca_download": TLS_CA_PATH}
                    )
                async with client.get(secure + TLS_CA_PATH, ssl=tls) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        response.headers["Content-Disposition"], 'attachment; filename="pa-ca.crt"'
                    )
                    self.assertEqual(await response.read(), cert)

                (tls_dir / "server.crt").unlink()
                bootstrap.write_text(base, encoding="utf-8")
                self.assertEqual((await console.reconfigure())["scheme"], "http")
                async with client.get(origin + "/config/") as response:
                    self.assertEqual(response.status, 200)

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
                        + '\nplugins=["ai_usage","workfiles","models","pi","execution","conversation","web"]\n[plugin_settings.web]\nenable=true\n',
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
                        snapshot.version,
                        [{"path": ["plugin_settings", "web", "enable"], "op": "set", "value": False}],
                        [],
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
