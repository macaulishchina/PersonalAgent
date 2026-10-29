"""Operator directory checks use synthetic HTTP, saved revisions and private credentials."""

import asyncio
import json
import tomllib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from assistant_runtime.app.management import BusinessProcess
from assistant_runtime.core.configuration.console import Console
from assistant_runtime.core.configuration.settings import ManagementSettings
from assistant_runtime.core.models.catalog import query, models
from assistant_runtime.core.models.settings import ModelSettings


class QueryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.payload = {"data": [{"id": "model-b"}, {"id": "model-a"}, {"id": "model-a"}]}
        self.status = 200
        self.body = None

        async def serve(request):
            self.requests.append((request.method, request.path, dict(request.headers), dict(request.query)))
            if self.body is not None:
                return web.Response(status=self.status, body=self.body)
            return web.json_response(self.payload, status=self.status)

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", serve)
        self.provider = TestServer(app)
        await self.provider.start_server()
        self.addAsyncCleanup(self.provider.close)

    def spec(self, protocol="openai_responses"):
        return {
            "protocol": protocol,
            "base_url": str(self.provider.make_url("/v1")),
            "key_env": "TEST_KEY",
            "enabled": False,
        }

    async def test_directory_is_bounded_sorted_and_never_calls_inference(self):
        result = await query(self.spec(), "private-test-key")
        self.assertTrue(result["ok"])
        self.assertEqual([m["id"] for m in result["models"]], ["model-a", "model-b"])
        self.assertEqual(len(self.requests), 1)
        method, path, headers, _ = self.requests[0]
        self.assertEqual((method, path), ("GET", "/v1/models"))
        self.assertEqual(headers["Authorization"], "Bearer private-test-key")
        self.assertNotIn("private-test-key", json.dumps(result))
        self.assertEqual(len(models([{"id": str(i)} for i in range(600)])), 500)

    async def test_native_protocol_authentication_and_directory_shapes(self):
        self.payload = {"data": [{"id": "claude-example", "display_name": "Example"}], "has_more": True}
        result = await query(self.spec("anthropic"), "private-test-key")
        self.assertTrue(result["truncated"])
        self.assertEqual(self.requests[-1][1], "/v1/models")
        self.assertEqual(self.requests[-1][2]["x-api-key"], "private-test-key")
        self.assertEqual(self.requests[-1][2]["anthropic-version"], "2023-06-01")
        self.payload = {
            "models": [{"name": "models/gemini-example", "displayName": "Example"}],
            "nextPageToken": "more",
        }
        result = await query(self.spec("gemini"), "private-test-key")
        self.assertEqual(result["models"][0]["id"], "gemini-example")
        self.assertEqual(self.requests[-1][2]["x-goog-api-key"], "private-test-key")
        self.assertTrue(result["truncated"])
        await query(
            {**self.spec("anthropic"), "auth_scheme": "bearer", "session_header": "x-session"},
            "private-test-key",
        )
        self.assertIn("Authorization", self.requests[-1][2])
        self.assertTrue(self.requests[-1][2]["x-session"].startswith("pa-catalog-"))

    async def test_opencode_protocols_share_their_declared_openai_directory(self):
        for prefix, path in (("opencode", "/inference"), ("opencode_go", "/inference/go")):
            for suffix in ("responses", "chat", "anthropic", "gemini"):
                with self.subTest(preset=prefix + "_" + suffix):
                    spec = ModelSettings.parse(
                        {"providers": {"test": {"preset": prefix + "_" + suffix}}}
                    ).providers["test"]
                    self.assertEqual(
                        spec["catalog"],
                        {"url": "https://opencode.ai" + path + "/v1/models", "format": "openai"},
                    )
                    inference = spec["base_url"]
                    spec["catalog"]["url"] = str(self.provider.make_url(path + "/v1/models"))
                    response = await query(spec, "private-test-key")
                    self.assertTrue(response["ok"])
                    self.assertEqual([m["id"] for m in response["models"]], ["model-a", "model-b"])
                    method, actual_path, headers, params = self.requests[-1]
                    self.assertEqual((method, actual_path), ("GET", path + "/v1/models"))
                    self.assertEqual(params, {})
                    self.assertEqual(headers["Authorization"], "Bearer private-test-key")
                    self.assertNotIn("x-goog-api-key", headers)
                    self.assertNotIn("x-api-key", headers)
                    self.assertNotIn("anthropic-version", headers)
                    self.assertEqual(spec["base_url"], inference)
                    self.assertNotIn("private-test-key", json.dumps(response))

    async def test_shared_directory_errors_retain_their_meaning(self):
        spec = {
            **self.spec("gemini"),
            "auth_scheme": "bearer",
            "catalog": {"url": str(self.provider.make_url("/shared/models")), "format": "openai"},
        }
        for status, code in ((401, "authentication"), (404, "catalog_unavailable"), (429, "rate_limited")):
            self.status = status
            response = await query(spec, "private-test-key")
            self.assertFalse(response["ok"])
            self.assertEqual(response["code"], code)

    async def test_errors_redact_bodies_and_unsupported_directory_is_not_success(self):
        self.body = b"private-test-key upstream body"
        for status, expected in [
            (401, "authentication"),
            (403, "authentication"),
            (404, "catalog_unavailable"),
            (405, "catalog_unavailable"),
            (429, "rate_limited"),
            (500, "http_error"),
            (302, "redirect"),
        ]:
            self.status = status
            result = await query(self.spec(), "private-test-key")
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], expected)
            self.assertNotIn("private-test-key", json.dumps(result))
        self.status = 200
        self.assertEqual((await query(self.spec(), "key"))["code"], "invalid_response")
        self.body = b"x" * (1024 * 1024 + 1)
        self.assertEqual((await query(self.spec(), "key"))["code"], "response_limit")

    async def test_redirect_is_not_followed_with_credentials(self):
        requests = []

        async def redirect(request):
            requests.append(request.path)
            raise web.HTTPFound("/destination")

        app = web.Application()
        app.router.add_get("/{path:.*}", redirect)
        async with TestServer(app) as server:
            result = await query({**self.spec(), "base_url": str(server.make_url("/v1"))}, "private-test-key")
        self.assertEqual(result["code"], "redirect")
        self.assertEqual(requests, ["/v1/models"])

    async def management(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        entry = root / "config.toml"
        entry.write_text(
            'plugins=[]\n[model_api.providers.test]\nprotocol="openai_responses"\nbase_url="'
            + str(self.provider.make_url("/v1"))
            + '"\nkey_env="PA_TEST_DIRECTORY_KEY"\nenabled=false\n',
            encoding="utf-8",
        )
        (root / ".env").write_text("PA_TEST_DIRECTORY_KEY=private-test-key\n")
        business = BusinessProcess(ManagementSettings(root, entry))
        console = Console(business)
        console.provider_queries.references.download = AsyncMock(side_effect=OSError("offline fixture"))
        self.addAsyncCleanup(console.provider_queries.close)
        app = web.Application(middlewares=[console.guard])
        app.add_routes(
            [
                web.post("/api/login", console.login),
                web.post("/api/provider-query", console.provider_query),
                web.post("/api/model-capabilities", console.model_capabilities),
                web.post("/api/model-test", console.model_test),
                web.post("/api/configuration", console.save),
            ]
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        self.addAsyncCleanup(console.accounts.close)
        headers = {"Origin": str(client.make_url("/")).rstrip("/")}
        return root, business, console, client, headers

    async def test_catalog_background_refresh_requires_auth_and_preserves_model_configuration(self):
        from test_reference_catalog import compiled
        import asyncio

        root, business, console, client, headers = await self.management()
        before = business.files.snapshot()
        body = {"action": "search", "query": "new-model", "version": before.version}
        response = await client.post("/api/model-capabilities", json=body, headers=headers)
        self.assertEqual(response.status, 401)
        console.provider_queries.references.download.assert_not_called()
        await client.post(
            "/api/login", json={"token": console.access.path.read_text().strip()}, headers=headers
        )
        ready = asyncio.Event()

        async def download():
            await ready.wait()
            return compiled()

        console.provider_queries.references.download = AsyncMock(side_effect=download)
        response = await client.post("/api/model-capabilities", json=body, headers=headers)
        first = await response.json()
        self.assertEqual(first["total"], 0)
        self.assertTrue(first["catalog"]["updating"])
        await client.post("/api/model-capabilities", json=body, headers=headers)
        console.provider_queries.references.download.assert_awaited_once()
        ready.set()
        await console.provider_queries.references.task
        response = await client.post(
            "/api/model-capabilities", json={**body, "update": False}, headers=headers
        )
        self.assertEqual((await response.json())["total"], 1)
        response = await client.post(
            "/api/model-capabilities", json={"action": "catalog", "version": before.version}, headers=headers
        )
        self.assertFalse((await response.json())["catalog"]["updating"])
        self.assertEqual(business.files.snapshot().contents, before.contents)
        self.assertEqual(business.files.snapshot().version, before.version)
        console.provider_queries.references.download.assert_awaited_once()

    async def test_capabilities_need_auth_revision_but_not_credentials_or_inference(self):
        root, business, console, client, headers = await self.management()
        before = business.files.snapshot()
        body = {"provider": self.spec(), "model": "private-model", "version": before.version}
        response = await client.post("/api/model-capabilities", json=body, headers=headers)
        self.assertEqual(response.status, 401)
        await client.post(
            "/api/login", json={"token": console.access.path.read_text().strip()}, headers=headers
        )
        response = await client.post("/api/model-capabilities", json=body, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["capabilities"]["source"], "unknown")
        self.assertEqual(business.files.snapshot(), before)
        self.assertFalse(self.requests)
        response = await client.post(
            "/api/model-capabilities", json={**body, "version": "stale"}, headers=headers
        )
        self.assertEqual(response.status, 409)

    async def test_model_probe_auth_draft_revision_and_no_secret_persistence(self):
        import uuid

        root, business, console, client, headers = await self.management()
        before = business.files.snapshot()
        business.request = AsyncMock(
            return_value={"ok": True, "code": "completed", "message": "ok", "reply": "OK"}
        )
        body = {
            "request_id": str(uuid.uuid4()),
            "version": before.version,
            "name": "unsaved",
            "draft": {
                "entry": {"provider": "new-connection", "model": "synthetic"},
                "connection": {"provider": self.spec(), "key": "private-draft-model-key"},
            },
        }
        response = await client.post("/api/model-test", json=body, headers=headers)
        self.assertEqual(response.status, 401)
        business.request.assert_not_called()
        await client.post(
            "/api/login", json={"token": console.access.path.read_text().strip()}, headers=headers
        )
        response = await client.post("/api/model-test", json=body, headers={"Origin": "http://other.invalid"})
        self.assertEqual(response.status, 403)
        response = await client.post("/api/model-test", json={**body, "version": "stale"}, headers=headers)
        self.assertEqual(response.status, 409)
        business.request.assert_not_called()
        response = await client.post("/api/model-test", json=body, headers=headers)
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertTrue(result["ok"])
        self.assertNotIn("private-draft-model-key", json.dumps(result))
        sent = business.request.call_args.args[0]
        self.assertEqual(sent["command"], "model-test")
        self.assertEqual(sent["arguments"]["key"], "private-draft-model-key")
        self.assertEqual(sent["arguments"]["entry"], body["draft"]["entry"])
        self.assertEqual(business.files.snapshot(), before)

    async def test_reference_search_is_authenticated_read_only_and_invalid_reference_is_specific(self):
        root, business, console, client, headers = await self.management()
        before = business.files.snapshot()
        body = {"action": "search", "query": "deepseek-v4.1-flash", "version": before.version}
        response = await client.post("/api/model-capabilities", json=body, headers=headers)
        self.assertEqual(response.status, 401)
        await client.post(
            "/api/login", json={"token": console.access.path.read_text().strip()}, headers=headers
        )
        response = await client.post("/api/model-capabilities", json=body, headers=headers)
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertGreater(result["total"], 1)
        self.assertEqual(len(result["candidates"]), result["total"])
        self.assertGreater(result["total"], 50)
        for patch_body, status in [
            ({"version": "stale"}, 409),
            ({"query": []}, 400),
            ({"query": "x" * 201}, 400),
            ({"query": "bad\nquery"}, 400),
        ]:
            response = await client.post(
                "/api/model-capabilities", json={**body, **patch_body}, headers=headers
            )
            self.assertEqual(response.status, status)
        response = await client.post(
            "/api/model-capabilities", json=body, headers={"Origin": "http://other.invalid"}
        )
        self.assertEqual(response.status, 403)
        draft = {"provider": self.spec(), "model": "private-alias", "version": before.version}
        response = await client.post(
            "/api/model-capabilities", json={**draft, "reference": "absent/model"}, headers=headers
        )
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["field"], "reference")
        selected = result["candidates"][0]["reference"]
        response = await client.post(
            "/api/model-capabilities", json={**draft, "reference": selected}, headers=headers
        )
        self.assertEqual(response.status, 200)
        metadata = (await response.json())["capabilities"]
        self.assertEqual(metadata["reference"], selected)
        self.assertEqual(metadata["source"], "models.dev")
        self.assertEqual(metadata["reasoning_efforts"], ["low", "high", "max"])
        self.assertEqual(business.files.snapshot(), before)
        self.assertFalse(self.requests)

    async def test_dotted_model_alias_and_public_efforts_save_through_enabled_owner(self):
        from assistant_runtime.core.models.capabilities import profile
        from assistant_runtime.core.models.settings import ModelSettings

        root, business, console, client, headers = await self.management()
        entry_file = root / "config.toml"
        entry_file.write_text(entry_file.read_text().replace("plugins=[]", 'plugins=["ai_usage", "models"]'))
        await client.post(
            "/api/login", json={"token": console.access.path.read_text().strip()}, headers=headers
        )
        provider = self.spec()
        cap = profile(provider, "deepseek-v4.1-flash", reference="deepseek/deepseek-flash")
        model = {
            "provider": "test",
            "model": "deepseek-v4.1-flash",
            "capabilities": cap,
            "reasoning_efforts": cap["reasoning_efforts"],
            "default_reasoning_effort": "high",
        }
        response = await client.post(
            "/api/configuration",
            headers=headers,
            json={
                "version": business.files.snapshot().version,
                "patches": [
                    {"op": "set", "path": ["model_api", "entries", "deepseek-v4.1-flash"], "value": model}
                ],
            },
        )
        self.assertEqual(response.status, 200, await response.text())
        if business.apply_task:
            await business.apply_task
        saved = tomllib.loads(entry_file.read_text())["model_api"]
        parsed = ModelSettings.parse(saved)
        self.assertEqual(parsed.entries["deepseek-v4.1-flash"]["reasoning_efforts"], ["low", "high", "max"])
        self.assertEqual(parsed.entries["deepseek-v4.1-flash"]["default_reasoning_effort"], "high")
        before = business.files.snapshot()
        response = await client.post(
            "/api/configuration",
            headers=headers,
            json={
                "version": before.version,
                "patches": [{"op": "set", "path": ["model_api", "entries", "bad name"], "value": model}],
            },
        )
        self.assertEqual(response.status, 400)
        error = await response.json()
        self.assertIn("模型名称", error["error"])
        self.assertEqual(error["issues"][0]["path"], ["model_api", "entries"])
        self.assertEqual(business.files.snapshot(), before)
        self.assertFalse(self.requests)

    async def test_connection_only_save_after_probe_keeps_name_and_secret_private(self):
        root, business, console, client, headers = await self.management()
        await client.post(
            "/api/login", json={"token": console.access.path.read_text().strip()}, headers=headers
        )
        entry = root / "config.toml"
        entry.write_text(entry.read_text().replace("plugins=[]", 'plugins=["ai_usage", "models"]'))
        before = business.files.snapshot()
        provider = {**self.spec(), "key_env": "PA_ONLY_CONNECTION", "enabled": True}
        response = await client.post(
            "/api/provider-query",
            headers=headers,
            json={
                "name": "LeiHuo-OpenAI",
                "action": "test",
                "version": before.version,
                "draft": {"provider": provider, "key": "synthetic-only-secret"},
            },
        )
        self.assertTrue((await response.json())["ok"])
        self.assertEqual(business.files.snapshot(), before)
        response = await client.post(
            "/api/configuration",
            headers=headers,
            json={
                "version": before.version,
                "patches": [
                    {"op": "set", "path": ["model_api", "providers", "LeiHuo-OpenAI"], "value": provider}
                ],
                "resources": [
                    {
                        "id": "credentials",
                        "op": "replace_secret",
                        "key": "PA_ONLY_CONNECTION",
                        "value": "synthetic-only-secret",
                    }
                ],
            },
        )
        self.assertEqual(response.status, 200, await response.text())
        if business.apply_task:
            await business.apply_task
        content = (root / "config.toml").read_text(encoding="utf-8")
        saved = tomllib.loads(content)["model_api"]
        self.assertIn("LeiHuo-OpenAI", saved["providers"])
        self.assertFalse(saved.get("entries"))
        self.assertNotIn("synthetic-only-secret", content)
        self.assertIn("synthetic-only-secret", (root / ".env").read_text())
        from assistant_runtime.core.models.settings import ModelSettings

        self.assertEqual(ModelSettings.parse(saved).entries, {})
        version = business.files.snapshot().version
        response = await client.post(
            "/api/configuration",
            headers=headers,
            json={
                "version": version,
                "patches": [
                    {"op": "set", "path": ["model_api", "providers", "bad name!"], "value": provider}
                ],
            },
        )
        error = await response.json()
        self.assertEqual(response.status, 400)
        self.assertTrue(error.get("issues"))
        self.assertEqual(business.files.snapshot().version, version)

    async def test_management_auth_revision_and_no_configuration_mutation(self):
        root, business, console, client, headers = await self.management()
        before = business.files.snapshot()
        body = {"name": "test", "action": "models", "version": before.version}
        response = await client.post("/api/provider-query", json=body, headers=headers)
        self.assertEqual(response.status, 401)
        response = await client.post(
            "/api/login", json={"token": console.access.path.read_text().strip()}, headers=headers
        )
        self.assertEqual(response.status, 200)
        response = await client.post(
            "/api/provider-query", json=body, headers={"Origin": "http://other.invalid"}
        )
        self.assertEqual(response.status, 403)
        response = await client.post(
            "/api/provider-query", json={**body, "version": "stale"}, headers=headers
        )
        self.assertEqual(response.status, 409)
        self.assertFalse(self.requests)
        response = await client.post("/api/provider-query", json=body, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["ok"])
        self.assertEqual(business.files.snapshot(), before)
        response = await client.post("/api/provider-query", json={**body, "action": "test"}, headers=headers)
        self.assertNotIn("models", await response.json())
        (root / ".env").write_text("")
        response = await console.provider_queries.action("test", "models", business.files.snapshot().version)
        self.assertEqual(response["code"], "configuration")

    async def test_configuration_change_during_query_rejects_stale_result(self):
        root, business, console, _, _ = await self.management()
        version = business.files.snapshot().version

        async def changed(*args):
            (root / ".env").write_text("PA_TEST_DIRECTORY_KEY=changed\n")
            return {"ok": True, "models": []}

        from assistant_runtime.core.configuration.files import Conflict

        with patch("assistant_runtime.core.configuration.provider_queries.query", side_effect=changed):
            with self.assertRaises(Conflict):
                await console.provider_queries.action("test", "models", version)

    async def test_native_directory_and_auth_are_not_remote_acceptance(self):
        _, business, console, _, _ = await self.management()
        version = business.files.snapshot().version
        with patch.object(
            console.provider_queries, "capture", return_value=({"protocol": "pi", "account": "native"}, None)
        ):
            with patch.object(console.accounts, "action", new=AsyncMock(return_value={"connected": True})):
                result = await console.provider_queries.action("native", "test", version)
                self.assertEqual(result["code"], "local_auth")
            with patch.object(
                console.accounts, "action", new=AsyncMock(return_value=[{"id": "native-model"}])
            ):
                result = await console.provider_queries.action("native", "models", version)
                self.assertEqual(result["source"], "native")
            with patch.object(console.accounts, "action", new=AsyncMock(side_effect=asyncio.TimeoutError)):
                self.assertEqual(
                    (await console.provider_queries.action("native", "models", version))["code"], "timeout"
                )
        self.assertFalse(self.requests)

    async def test_unsaved_provider_and_secret_are_used_without_persistence(self):
        root, business, console, client, headers = await self.management()
        before = business.files.snapshot()
        await client.post(
            "/api/login", json={"token": console.access.path.read_text().strip()}, headers=headers
        )
        body = {
            "name": "unsaved",
            "action": "models",
            "version": before.version,
            "draft": {"provider": self.spec(), "key": "draft-private-key"},
        }
        response = await client.post("/api/provider-query", json=body, headers=headers)
        payload = await response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(self.requests[-1][2]["Authorization"], "Bearer draft-private-key")
        self.assertNotIn("draft-private-key", json.dumps(payload))
        self.assertEqual(business.files.snapshot(), before)
        self.assertNotIn("draft-private-key", (root / ".env").read_text())
        self.assertNotIn("unsaved", business.files.snapshot().contents["config.toml"].decode())

    async def test_draft_validation_identifies_field_and_does_not_dispatch(self):
        _, business, console, _, _ = await self.management()
        version = business.files.snapshot().version
        for field, value, expected in (
            ("session_header", "33", "session_header"),
            ("base_url", "", "base_url"),
            ("protocol", "bad", "protocol"),
            ("key_env", "bad-key", "key_env"),
        ):
            response = await console.provider_queries.action(
                "test", "test", version, {"provider": {**self.spec(), field: value}, "key": "draft-key"}
            )
            self.assertFalse(response["ok"])
            self.assertEqual(response["field"], expected)
        for secret in ("", "key\nvalue", 33):
            response = await console.provider_queries.action(
                "test", "test", version, {"provider": self.spec(), "key": secret}
            )
            self.assertEqual(response["field"], "key")
        self.assertFalse(self.requests)
        response = await console.provider_queries.action(
            "test", "test", version, {"provider": {**self.spec(), "session_header": ""}, "key": "draft-key"}
        )
        self.assertTrue(response["ok"])

    async def test_draft_reuses_only_selected_saved_key_and_ignores_other_invalid_providers(self):
        root, business, console, _, _ = await self.management()
        entry = root / "config.toml"
        entry.write_text(entry.read_text() + '\n[model_api.providers.broken]\nprotocol="wrong"\n')
        before = business.files.snapshot()
        provider = {**self.spec(), "key_env": "PA_TEST_DIRECTORY_KEY"}
        response = await console.provider_queries.action(
            "new", "test", before.version, {"provider": provider}
        )
        self.assertTrue(response["ok"])
        self.assertEqual(self.requests[-1][2]["Authorization"], "Bearer private-test-key")
        response = await console.provider_queries.action(
            "new", "test", before.version, {"provider": self.spec()}
        )
        self.assertEqual(response["field"], "key")
        self.assertEqual(business.files.snapshot(), before)
