"""Model configuration, bounded admission and durable invocation receipts."""

from usage_fixture import attach_usage

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import uuid

from fastmcp import Client
from assistant_runtime.kernel.models import ModelRequest
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.store import Store
from assistant_runtime.plugins.models.settings import ModelSettings, PRESETS
from assistant_runtime.plugins.models.gateway import configuration
from assistant_runtime.plugins.models.gateway_transport import upstream_headers, prepare_request
from assistant_runtime.plugins.pi.catalog import MODEL_SESSION_HEADER
from assistant_runtime.plugins.models.plugin import ModelsPlugin
from assistant_runtime.mcp.models import create_server


def settings(**changes):
    return {
        "enabled": True,
        "concurrency": 1,
        "queue_size": 1,
        "providers": {"test": {"preset": "deepseek", "enabled": True}},
        "entries": {
            "test-model": {
                "provider": "test",
                "model": "upstream",
                "structured": True,
                "reasoning_efforts": ["none"],
            }
        },
        **changes,
    }


class SettingsTest(unittest.TestCase):
    def test_execution_limits_belong_only_to_ai_usage(self):
        with self.assertRaises(ValueError):
            ModelSettings.parse(settings(execution_limits={"max_requests": 3}))

    def test_missing_effort_declaration_does_not_silently_advertise_none(self):
        config = settings()
        del config["entries"]["test-model"]["reasoning_efforts"]
        with self.assertRaisesRegex(ValueError, "reasoning efforts"):
            ModelSettings.parse(config)

    def test_reasoning_default_is_explicit_and_not_the_first_supported_level(self):
        raw = settings()
        raw["entries"]["test-model"].update(
            reasoning_efforts=["none", "low", "high"], default_reasoning_effort="low"
        )
        parsed = ModelSettings.parse(raw)
        self.assertEqual(parsed.catalog()[0]["default_reasoning_effort"], "low")
        from assistant_runtime.plugins.pi.catalog import pi_models_config

        self.assertTrue(pi_models_config(parsed)["providers"]["assistant_api"]["models"][0]["reasoning"])
        route = configuration(parsed)["model_list"][0]["litellm_params"]
        self.assertEqual(route["max_tokens"], parsed.execution_max_output_tokens)
        for invalid in (
            {"default_reasoning_effort": "medium"},
            {"reasoning_efforts": ["low", "low"]},
            {"reasoning_efforts": [[]]},
            {"execution_max_output_tokens": 131073},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ModelSettings.parse(
                    settings(entries={"test-model": {**raw["entries"]["test-model"], **invalid}})
                )

    def test_gateway_preserves_effort_and_disables_anthropic_thinking_explicitly(self):
        for protocol in ("openai_responses", "openai_chat", "anthropic", "gemini"):
            route = {
                "protocol": protocol,
                "reasoning_efforts": ["low", "high"],
                "default_reasoning_effort": "low",
            }
            self.assertEqual(prepare_request(route, {}, {}, [])["reasoning"], {"effort": "low"})
            self.assertEqual(
                prepare_request(route, {"reasoning": {"effort": "high"}}, {}, [])["reasoning"],
                {"effort": "high"},
            )
            self.assertEqual(
                prepare_request(route, {"reasoning": {"effort": None}, "reasoning_effort": "high"}, {}, [])[
                    "reasoning"
                ],
                {"effort": "high"},
            )
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                prepare_request(route, {"reasoning": {"effort": "medium"}}, {}, [])
        route = {
            "protocol": "anthropic",
            "reasoning_efforts": ["none", "high"],
            "default_reasoning_effort": "high",
        }
        result = prepare_request(route, {"reasoning": {"effort": "none"}}, {}, [])
        self.assertEqual(result["thinking"], {"type": "disabled"})
        self.assertEqual(result["reasoning"], {"effort": "none"})
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            prepare_request(route, {"reasoning": {"effort": "high"}, "reasoning_effort": "none"}, {}, [])
        with self.assertRaisesRegex(ValueError, "Invalid"):
            prepare_request(route, {"reasoning": "high"}, {}, [])

    def test_disabled_provider_cannot_be_called_or_routed(self):
        from assistant_runtime.plugins.pi.catalog import pi_models_config

        for enabled in (False, True):
            parsed = ModelSettings.parse(
                settings(providers={"test": {"preset": "deepseek", "enabled": enabled}})
            )
            self.assertEqual(parsed.catalog()[0]["available"], enabled)
            self.assertEqual(bool(configuration(parsed)["model_list"]), enabled)
            self.assertEqual(bool(pi_models_config(parsed)["providers"]["assistant_api"]["models"]), enabled)
            if enabled:
                self.assertEqual(parsed.entry("test-model")["provider"], "test")
            else:
                with self.assertRaisesRegex(ValueError, "provider is disabled"):
                    parsed.entry("test-model")

    def test_provider_switch_requires_boolean_and_rejects_old_name(self):
        for switch in ({"enabled": "false"}, {"enabled": 1}, {"external_data_consent": True}):
            with self.subTest(switch=switch), self.assertRaises(ValueError):
                ModelSettings.parse(settings(providers={"test": {"preset": "deepseek", **switch}}))

    def test_native_protocols_normalize_only_the_nonsemantic_cache_hint(self):
        data = {
            "prompt_cache_key": "cache-hint",
            "tools": [{"type": "custom"}],
            "input": [{"type": "input_image"}],
            "reasoning": {"effort": "low"},
            "text": {"format": {"type": "json_schema"}},
            "extra_headers": {"Authorization": "untrusted"},
        }
        result = prepare_request(PRESETS["claude"], data.copy(), {}, [])
        self.assertNotIn("prompt_cache_key", result)
        for key in ("tools", "input", "reasoning", "text"):
            self.assertEqual(result[key], data[key])
        self.assertNotIn("Authorization", result["extra_headers"])
        gemini = prepare_request(PRESETS["gemini"], data.copy(), {}, [])
        self.assertNotIn("prompt_cache_key", gemini)
        for key in ("tools", "input", "reasoning", "text"):
            self.assertEqual(gemini[key], data[key])
        result = prepare_request(PRESETS["openai"], data.copy(), {}, [])
        self.assertEqual(result["prompt_cache_key"], "cache-hint")

    def test_gemini_explicit_gateway_bases_include_api_version(self):
        for preset in ("gemini", "opencode_go_gemini"):
            parsed = ModelSettings.parse(settings(providers={"test": {"preset": preset, "enabled": True}}))
            route = configuration(parsed)["model_list"][0]["litellm_params"]
            self.assertTrue(route["api_base"].endswith("/v1beta"))

    def test_go_routes_authentication_and_conversation_isolation(self):
        for suffix in ("responses", "chat", "anthropic", "gemini"):
            provider = PRESETS["opencode_go_" + suffix]
            self.assertIn("/inference/go/", provider["base_url"])
            incoming = {
                "Authorization": "Bearer local-only",
                "x-api-key": "do-not-forward",
                "x-assistant-session": "conversation-one",
            }
            headers = upstream_headers(provider, incoming, {"OPENCODE_API_KEY": "upstream-only"})
            self.assertEqual(headers["Authorization"], "Bearer upstream-only")
            self.assertNotIn("x-api-key", headers)
            self.assertNotIn("local-only", str(headers))
            again = upstream_headers(
                provider,
                {MODEL_SESSION_HEADER: "conversation-one"},
                {"OPENCODE_API_KEY": "upstream-only"},
                (MODEL_SESSION_HEADER,),
            )
            self.assertEqual(headers, again)
            other = upstream_headers(
                provider,
                {MODEL_SESSION_HEADER: "conversation-two"},
                {"OPENCODE_API_KEY": "upstream-only"},
                (MODEL_SESSION_HEADER,),
            )
            self.assertNotEqual(headers["x-opencode-session"], other["x-opencode-session"])
            with self.assertRaisesRegex(ValueError, "conversation identity"):
                upstream_headers(provider, {}, {"OPENCODE_API_KEY": "upstream-only"})
        official = upstream_headers(PRESETS["claude"], incoming, {})
        self.assertEqual(set(official), {"User-Agent"})

    def test_official_presets_and_custom_protocol(self):
        for preset in PRESETS:
            raw = settings(providers={"test": {"preset": preset}})
            parsed = ModelSettings.parse(raw)
            self.assertEqual(parsed.providers["test"], PRESETS[preset])
            self.assertIn("preset", raw["providers"]["test"])
            self.assertFalse(parsed.catalog()[0]["available"])
            with self.assertRaisesRegex(ValueError, "provider is disabled"):
                parsed.entry("test-model")
        raw = settings(
            providers={
                "test": {
                    "protocol": "openai_chat",
                    "base_url": "https://compatible.example/v1",
                    "key_env": "CUSTOM_KEY",
                }
            }
        )
        self.assertEqual(
            ModelSettings.parse(raw).providers["test"]["base_url"], "https://compatible.example/v1"
        )

    def test_invalid_config_rejected_before_use(self):
        for changes in (
            {"gateway_url": "http://192.168.1.2:4010"},
            {"queue_size": 0},
            {"timeout_seconds": 81},
            {"memory": "missing"},
            {"enabled": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ModelSettings.parse(settings(**changes))
        for provider in (
            {"preset": "deepseek", "base_url": "https://evil.example"},
            {"protocol": "openai_chat", "base_url": "https://user:secret@example.com", "key_env": "KEY"},
        ):
            with self.assertRaises(ValueError):
                ModelSettings.parse(settings(providers={"test": provider}))

    def test_memory_binding_cannot_select_a_disabled_provider(self):
        with self.assertRaisesRegex(ValueError, "disabled"):
            ModelSettings.parse(
                settings(memory="test-model", providers={"test": {"preset": "deepseek", "enabled": False}})
            )

    def test_four_protocol_routes_have_no_retry_or_fallback(self):
        protocols = {p["protocol"] for p in PRESETS.values()}
        self.assertEqual(len(protocols), 4)
        for preset in PRESETS:
            parsed = ModelSettings.parse(settings(providers={"test": {"preset": preset, "enabled": True}}))
            config = configuration(parsed)
            params = config["model_list"][0]["litellm_params"]
            self.assertEqual(params["num_retries"], 0)
            self.assertTrue(params["api_key"].startswith("os.environ/"))
            self.assertEqual(config["router_settings"]["fallbacks"], [])
            self.assertFalse(config["litellm_settings"]["drop_params"])
            self.assertEqual(
                params.get("use_chat_completions_api", False),
                parsed.providers["test"]["protocol"] == "openai_chat",
            )


class ModelsTest(unittest.IsolatedAsyncioTestCase):
    async def test_auxiliary_reasoning_default_explicit_override_and_validation(self):
        self.plugin.settings.entries["test-model"].update(
            reasoning_efforts=["none", "low", "high"], default_reasoning_effort="low"
        )
        for requested, expected in (
            ({}, "low"),
            ({"reasoning_effort": "none"}, "none"),
            ({"reasoning_effort": "high"}, "high"),
        ):
            result = await self.runtime.invoke(self.request(**requested))
            self.assertEqual(result["reasoning_effort"], expected)
            self.assertEqual(self.client.complete.call_args.args[0]["reasoning"], {"effort": expected})
            self.assertEqual(result["attempts"][0]["reasoning_effort"], expected)
        for invalid in ("medium", "auto", [], 1):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await self.runtime.invoke(self.request(reasoning_effort=invalid))
        self.assertEqual(self.client.complete.await_count, 3)

    async def test_composed_memory_uses_explicit_binding_without_error_fallback(self):
        from assistant_runtime.bootstrap import memory_complete

        self.host.backend = SimpleNamespace(memory_complete=AsyncMock(return_value={"native": True}))
        self.host.config.model_api["memory"] = "test-model"
        schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
        self.assertEqual(
            await memory_complete(self.host, [{"role": "user", "content": "synthetic"}], schema), {"value": 7}
        )
        self.host.backend.memory_complete.assert_not_awaited()
        self.client.complete.side_effect = RuntimeError("model_http_401")
        with self.assertRaisesRegex(RuntimeError, "model_http_401"):
            await memory_complete(self.host, [], schema)
        self.host.backend.memory_complete.assert_not_awaited()
        self.host.config.model_api["memory"] = ""
        with self.assertRaisesRegex(RuntimeError, "explicit model_api.memory"):
            await memory_complete(self.host, [], schema)

    async def test_memory_batch_budget_covers_both_scopes_and_survives_retry_and_reopen(self):
        from dataclasses import replace
        from assistant_runtime.bootstrap import memory_complete
        from assistant_runtime.kernel.ai_usage import UsageError, UsageScope, current_usage
        from assistant_runtime.memory.service import MemoryPlugin
        from assistant_runtime.plugins.ai_usage.service import UsageService

        self.host.config.model_api["memory"] = "test-model"
        self.host.config.memory_auto_capture = True
        usage = self.host.ai_usage.service
        policy = replace(usage.settings, max_requests=2)
        usage.settings = usage.ledger.settings = policy
        schema = {"type": "object", "properties": {"value": {"type": "integer"}}}
        phases = []

        async def capture(sources, scope, project, job_id):
            phases.append(scope)
            for _ in range(2):
                await memory_complete(self.host, sources, schema)
            return {"records": [], "deleted": []}

        provider = SimpleNamespace(capture=capture, export=AsyncMock(return_value=[]))
        memory = MemoryPlugin(self.host.config, self.store, provider, lambda: False)
        self.store.transcript("user", "Synthetic tea preference")
        memory.enqueue()
        job = self.store.db.execute(
            "SELECT id,start_id,end_id,epoch FROM memory_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        foreground = UsageScope("foreground", "main")
        token = current_usage.set(foreground)
        reopened = None
        try:
            with self.assertRaisesRegex(UsageError, "max_requests"):
                await memory.process_job(job)
            self.assertEqual(phases, ["user", "project"])
            self.assertEqual(current_usage.get(), foreground)
            self.assertEqual(self.client.complete.await_count, 2)
            calls = (await usage.call("query", {}))["items"]
            works = {row["work_id"] for row in calls}
            self.assertEqual(len(works), 1)
            self.assertEqual({row["service"] for row in calls}, {"memory"})
            work = works.pop()

            await usage.close()
            reopened = UsageService(self.host.config.state_dir / "ai-usage", policy)
            await reopened.start()
            self.host.ai_usage.service = reopened
            memory = MemoryPlugin(self.host.config, self.store, provider, lambda: False)
            with self.assertRaisesRegex(UsageError, "max_requests"):
                await memory.process_job(job)
            self.assertEqual(self.client.complete.await_count, 2)
            self.assertEqual((await reopened.snapshot(work))["requests"], 2)
            self.assertEqual(current_usage.get(), foreground)

            self.store.transcript("user", "A new synthetic preference")
            memory.enqueue()
            next_job = self.store.db.execute(
                "SELECT id,start_id,end_id,epoch FROM memory_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            with self.assertRaisesRegex(UsageError, "max_requests"):
                await memory.process_job(next_job)
            self.assertEqual(self.client.complete.await_count, 4)
            calls = (await reopened.call("query", {}))["items"]
            self.assertEqual(len({row["work_id"] for row in calls}), 2)
        finally:
            current_usage.reset(token)
            if reopened:
                await reopened.close()

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = Store(root / "db.sqlite")
        self.runtime = Runtime(self.store)
        self.client = SimpleNamespace(
            start=AsyncMock(),
            close=AsyncMock(),
            complete=AsyncMock(return_value={"text": '{"value": 7}', "usage": {"output_tokens": 5}}),
        )
        self.host = SimpleNamespace(
            config=SimpleNamespace(root=root, model_api=settings()), store=self.store, runtime=self.runtime
        )
        attach_usage(self.host, self.runtime)
        self.plugin = ModelsPlugin(self.host, self.client)
        self.runtime.add(self.plugin)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        self.store.db.close()
        self.temp.cleanup()

    def request(self, **changes):
        return ModelRequest(
            "infer",
            {"request_id": str(uuid.uuid4()), "model": "test-model", "text": "Synthetic question", **changes},
        )

    async def test_auxiliary_session_stable_and_owned_by_server(self):
        self.host.conversation = SimpleNamespace(session="one")
        await self.plugin.request(self.request())
        first = self.client.complete.call_args.kwargs["session_id"]
        await self.plugin.request(self.request())
        self.assertEqual(first, self.client.complete.call_args.kwargs["session_id"])
        self.host.conversation.session = "two"
        await self.plugin.request(self.request())
        self.assertNotEqual(first, self.client.complete.call_args.kwargs["session_id"])
        self.assertEqual(len(first), 64)
        with self.assertRaisesRegex(ValueError, "Unknown inference arguments"):
            await self.plugin.request(self.request(session_id="spoof"))

    async def test_completed_duplicate_returns_receipt_and_conflict_rejected(self):
        request = self.request()
        first = await self.plugin.request(request)
        self.assertEqual(first["state"], "completed")
        self.assertEqual(await self.plugin.request(request), first)
        self.client.complete.assert_awaited_once()
        with self.assertRaisesRegex(ValueError, "different content"):
            await self.plugin.request(
                self.request(request_id=request.arguments["request_id"], text="different")
            )
        row = self.store.db.execute("SELECT fingerprint,config_revision FROM model_calls").fetchone()
        self.assertEqual(len(row[0]), 64)
        self.assertEqual(row[1], self.plugin.settings.fingerprint())

    async def test_unknown_does_not_retry_even_after_restart(self):
        self.client.complete.side_effect = TimeoutError("disconnected after submit")
        request = self.request()
        with self.assertRaises(TimeoutError):
            await self.plugin.request(request)
        restarted = ModelsPlugin(self.host, self.client)
        self.assertEqual((await restarted.request(request))["state"], "unknown")
        self.client.complete.assert_awaited_once()

    async def test_committed_submission_at_restart_remains_unknown(self):
        request = self.request()
        self.client.complete.side_effect = RuntimeError("network lost")
        with self.assertRaises(RuntimeError):
            await self.plugin.request(request)
        self.store.db.execute("UPDATE model_calls SET state='submitted'")
        restarted = ModelsPlugin(self.host, self.client)
        self.assertEqual((await restarted.request(request))["state"], "unknown")

    async def test_bounded_queue_shutdown_cancels_without_retry(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def complete(payload, **kwargs):
            entered.set()
            await release.wait()
            return {"text": "done"}

        self.client.complete.side_effect = complete
        first = asyncio.create_task(self.plugin.request(self.request()))
        await entered.wait()
        second = asyncio.create_task(self.plugin.request(self.request()))
        await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError, "queue full"):
            await self.plugin.request(self.request())
        await self.runtime.close()
        self.assertTrue(first.cancelled())
        self.assertTrue(second.cancelled())
        self.assertEqual(self.plugin.pending, 0)
        self.assertEqual(self.store.db.execute("SELECT state FROM model_calls").fetchone()[0], "unknown")
        self.client.close.assert_awaited_once()

    async def test_same_id_queued_with_different_content_conflicts(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def complete(payload, **kwargs):
            entered.set()
            await release.wait()
            return {"text": "done"}

        self.client.complete.side_effect = complete
        request = self.request()
        first = asyncio.create_task(self.plugin.request(request))
        await entered.wait()
        with self.assertRaisesRegex(ValueError, "different content"):
            await self.plugin.request(self.request(request_id=request.arguments["request_id"], text="other"))
        release.set()
        await first

    async def test_schema_validated_and_no_tool_or_arbitrary_file_inputs(self):
        schema = {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        }
        self.assertEqual((await self.plugin.request(self.request(schema=schema)))["data"], {"value": 7})
        self.client.complete.return_value = {"text": '{"value": "wrong"}'}
        with self.assertRaises(Exception):
            await self.plugin.request(self.request(schema=schema))
        for arguments in (
            {"images": ["/etc/passwd"]},
            {"tools": []},
            {"caller_run": "other"},
            {"schema": {"$ref": "https://example.com"}},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                await self.plugin.request(self.request(**arguments))

    async def test_task_identity_and_images_require_active_owner(self):
        self.runtime.invoke = AsyncMock(return_value={"run": {"state": "cancelled"}})
        request = self.request()
        with self.assertRaisesRegex(ValueError, "no longer active"):
            await self.plugin.request(ModelRequest("infer", request.arguments, "run"))
        self.client.complete.assert_not_awaited()
        self.host.runtime.plugins["tasks"] = SimpleNamespace(
            repo=SimpleNamespace(run=lambda _: {"data": {"attachments": []}})
        )
        with self.assertRaisesRegex(ValueError, "belong"):
            await self.plugin.images(["a" * 32], "run")

    async def test_result_from_old_execution_version_is_rejected(self):
        version = {"execution_session_id": "one", "turn_id": "one", "model_segments": 0}

        async def info(_):
            return {"run": {"state": "running", "execution_version": version.copy()}}

        self.runtime.invoke = info

        async def complete(_, **kwargs):
            version["turn_id"] = "two"
            return {"text": "late"}

        self.client.complete.side_effect = complete
        request = self.request()
        with self.assertRaisesRegex(ValueError, "version changed"):
            await self.plugin.request(ModelRequest("infer", request.arguments, "run"))
        self.assertEqual(
            (await self.plugin.request(ModelRequest("infer", request.arguments, "run")))["state"], "unknown"
        )
        self.client.complete.assert_awaited_once()

    async def test_mcp_binds_caller_and_does_not_expose_endpoint_or_tools(self):
        target = SimpleNamespace(invoke=AsyncMock(return_value={"state": "completed"}))
        async with Client(create_server(target, "bound-run")) as client:
            tools = await client.list_tools()
            tool = next(t for t in tools if t.name == "infer_with_model")
            self.assertEqual(
                set(tool.inputSchema["properties"]),
                {"request_id", "model", "text", "images", "schema", "reasoning_effort"},
            )
            await client.call_tool("infer_with_model", self.request().arguments)
        self.assertEqual(target.invoke.call_args.args[0].caller_run, "bound-run")


class DeploymentTest(unittest.TestCase):
    def test_only_referenced_secrets_are_private_and_inputs_are_derived(self):
        import os
        from unittest.mock import patch
        from assistant_runtime.config import Config
        from scripts.prepare_model_gateway import prepare, IMAGE

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env").write_text(
                "DEEPSEEK_API_KEY=synthetic-provider-secret\nUNRELATED_KEY=never-export\n"
            )
            config = Config(root=root, plugins=["models"], model_api=settings())
            with patch.dict(os.environ, {}, clear=True):
                path = prepare(config)
            manifest = json.loads(path.read_text())
            gateway = manifest["services"]["gateway"]
            self.assertEqual(gateway["image"], IMAGE)
            self.assertIn("@sha256:", IMAGE)
            self.assertEqual(gateway["ports"], ["127.0.0.1:4010:4000"])
            self.assertNotIn("UNRELATED_KEY", gateway["environment"])
            self.assertNotIn("never-export", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("synthetic-provider-secret", (path.parent / "gateway/config.yaml").read_text())
            self.assertEqual(
                (path.parent / "revision").read_text().strip(),
                ModelSettings.parse(config.model_api).fingerprint(),
            )
