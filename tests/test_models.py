"""Model configuration, bounded admission and durable invocation receipts."""

from platform_support import private_mode

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
from assistant_runtime.api.models import ModelRequest
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.app.store import Store
from assistant_runtime.core.models.settings import ModelSettings, PRESETS
from assistant_runtime.core.pi.catalog import pi_models_config
from assistant_runtime.core.models.plugin import ModelsPlugin
from assistant_runtime.core.models.tools import create_server


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
    def test_context_maintenance_configuration(self):
        settings = ModelSettings.parse({})
        self.assertEqual(settings.compaction_timeout_seconds, 600)
        self.assertEqual(settings.timeout_seconds, 600)
        self.assertEqual(settings.tool_result_max_chars, 6000)
        self.assertEqual(settings.max_loaded_tools, 12)
        for value in [0, True, 65]:
            with self.assertRaises(ValueError):
                ModelSettings.parse({"max_loaded_tools": value})
        for value in [0, -1, True, 3601]:
            with self.assertRaises(ValueError):
                ModelSettings.parse({"compaction_timeout_seconds": value})

    def test_long_deadlines_keep_outer_envelopes_above_inner_limits(self):
        for normal, compaction in [(600, 600), (3600, 600), (600, 3600)]:
            parsed = ModelSettings.parse(
                settings(timeout_seconds=normal, compaction_timeout_seconds=compaction)
            )
            self.assertEqual(
                parsed.outer_timeout_seconds,
                max(normal, compaction) + 60,
            )

    def test_registry_names_preserve_case_and_reject_case_only_collisions(self):
        from assistant_runtime.api.configuration import ConfigurationError

        provider = {"preset": "openai", "enabled": True}
        parsed = ModelSettings.parse(
            {
                "providers": {"LeiHuo-OpenAI": provider},
                "entries": {"Main-Model": {"provider": "LeiHuo-OpenAI", "model": "synthetic-model"}},
            }
        )
        self.assertEqual(parsed.entries["Main-Model"]["provider"], "LeiHuo-OpenAI")
        with self.assertRaises(ConfigurationError) as caught:
            ModelSettings.parse({"providers": {"Team": provider, "team": provider}})
        self.assertEqual(caught.exception.code, "duplicate_registry_name")
        with self.assertRaises(ConfigurationError) as caught:
            ModelSettings.parse({"providers": {"not valid!": provider}})
        self.assertEqual(caught.exception.path, ["model_api", "providers"])

    def test_execution_limits_belong_only_to_ai_usage(self):
        with self.assertRaises(ValueError):
            ModelSettings.parse(settings(execution_limits={"max_requests": 3}))

    def test_missing_effort_declaration_does_not_silently_advertise_none(self):
        config = settings()
        del config["entries"]["test-model"]["reasoning_efforts"]
        parsed = ModelSettings.parse(config)
        self.assertEqual(parsed.entries["test-model"]["reasoning_efforts"], [])
        self.assertEqual(parsed.entries["test-model"]["default_reasoning_effort"], "auto")
        self.assertEqual(parsed.catalog()[0]["supported_reasoning_efforts"], [{"reasoning_effort": "auto"}])

    def test_reasoning_default_is_explicit_and_not_the_first_supported_level(self):
        raw = settings()
        raw["entries"]["test-model"].update(
            reasoning_efforts=["none", "low", "high"], default_reasoning_effort="low"
        )
        parsed = ModelSettings.parse(raw)
        self.assertEqual(parsed.catalog()[0]["default_reasoning_effort"], "low")

        self.assertTrue(pi_models_config(parsed)["providers"]["assistant_api"]["models"][0]["reasoning"])
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

    def test_execution_catalog_preserves_explicit_effort_choices(self):
        raw = settings()
        raw["entries"]["test-model"].update(
            reasoning_efforts=["none", "high"], default_reasoning_effort="high"
        )
        model = pi_models_config(ModelSettings.parse(raw))["providers"]["assistant_api"]["models"][0]
        self.assertEqual(model["thinkingLevelMap"]["off"], "none")
        self.assertEqual(model["thinkingLevelMap"]["high"], "high")
        self.assertIsNone(model["thinkingLevelMap"]["medium"])

    def test_disabled_provider_cannot_be_called_or_routed(self):
        for enabled in (False, True):
            parsed = ModelSettings.parse(
                settings(providers={"test": {"preset": "deepseek", "enabled": enabled}})
            )
            self.assertEqual(parsed.catalog()[0]["available"], enabled)
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

    def test_protocol_selection_does_not_change_model_identity(self):
        for preset in PRESETS:
            parsed = ModelSettings.parse(settings(providers={"test": {"preset": preset, "enabled": True}}))
            model = pi_models_config(parsed)["providers"]["assistant_api"]["models"][0]
            self.assertEqual(model["id"], "test-model")
            self.assertEqual(model["api"], "assistant-native")

    def test_gemini_config_retains_supplier_endpoint(self):
        parsed = ModelSettings.parse(settings(providers={"test": {"preset": "gemini", "enabled": True}}))
        self.assertIn("v1beta", parsed.providers["test"]["base_url"])

    def test_custom_session_header_and_authentication_are_declared(self):
        for preset in (
            "opencode_go_responses",
            "opencode_go_chat",
            "opencode_go_anthropic",
            "opencode_go_gemini",
        ):
            parsed = ModelSettings.parse(settings(providers={"test": {"preset": preset, "enabled": True}}))
            self.assertEqual(parsed.providers["test"]["session_header"], "x-opencode-session")
            self.assertEqual(parsed.providers["test"]["auth_scheme"], "bearer")

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
            {"unknown_option": "invalid"},
            {"queue_size": 0},
            {"timeout_seconds": 3601},
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

    def test_four_protocol_routes_share_metered_pi_transport(self):
        self.assertEqual(len({p["protocol"] for p in PRESETS.values()}), 4)
        for preset in PRESETS:
            parsed = ModelSettings.parse(settings(providers={"test": {"preset": preset, "enabled": True}}))
            config = pi_models_config(parsed, transport_url="http://127.0.0.1:1234/v1")
            self.assertEqual(config["providers"]["assistant_api"]["baseUrl"], "http://127.0.0.1:1234/v1")
            self.assertNotIn(parsed.providers["test"]["key_env"], json.dumps(config))


class ModelsTest(unittest.IsolatedAsyncioTestCase):
    async def test_direct_inference_inherits_owner_timeout_and_cancellation_never_replays(self):
        self.assertEqual(self.runtime.timeouts[ModelRequest], 630)
        entered = asyncio.Event()
        release = asyncio.Event()
        original = self.client.complete.return_value

        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return original

        self.client.complete.side_effect = slow
        operation = self.request()
        work = asyncio.create_task(self.runtime.invoke(operation))
        await asyncio.wait_for(entered.wait(), 2)
        work.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await work
        release.set()
        result = await self.runtime.invoke(operation)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(self.client.complete.await_count, 1)

    async def test_auxiliary_reasoning_default_explicit_override_and_validation(self):
        self.plugin.settings.entries["test-model"].update(
            reasoning_efforts=["none", "low", "high"], default_reasoning_effort="low"
        )
        for requested, expected in (
            ({}, "low"),
            ({"reasoning_effort": "none"}, "none"),
            ({"reasoning_effort": "high"}, "high"),
            ({"reasoning_effort": "auto"}, "auto"),
        ):
            result = await self.runtime.invoke(self.request(**requested))
            self.assertEqual(result["reasoning_effort"], expected)
            self.assertEqual(self.client.complete.call_args.args[0]["reasoning"], {"effort": expected})
            self.assertEqual(result["attempts"][0]["reasoning_effort"], expected)
        for invalid in ("medium", "invented", [], 1):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await self.runtime.invoke(self.request(reasoning_effort=invalid))
        self.assertEqual(self.client.complete.await_count, 4)

    async def test_composed_memory_uses_explicit_binding_without_error_fallback(self):
        from assistant_runtime.core.memory.inference import complete as memory_complete

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
        from assistant_runtime.core.memory.inference import complete as memory_complete
        from assistant_runtime.api.ai_usage import UsageError, UsageScope, current_usage
        from assistant_runtime.core.memory.service import MemoryPlugin
        from assistant_runtime.core.ai_usage.service import UsageService

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
        self.store.transcripts.append("user", "Synthetic tea preference")
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

            self.store.transcripts.append("user", "A new synthetic preference")
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
    def test_execution_configuration_never_contains_provider_secrets(self):
        from assistant_runtime.core.pi.catalog import write_pi_models

        with tempfile.TemporaryDirectory() as folder:
            path = Path(
                write_pi_models(
                    Path(folder), ModelSettings.parse(settings()), transport_url="http://127.0.0.1:1234/v1"
                )
            )
            self.assertEqual(private_mode(path), 0o600)
            self.assertNotIn("DEEPSEEK_API_KEY", path.read_text())
            self.assertIn("PA_EXECUTION_TOKEN", path.read_text())
