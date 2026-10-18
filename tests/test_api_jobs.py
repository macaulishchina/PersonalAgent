"""API routing never inherits subscription identity or replays uncertain work."""

import asyncio
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_models
import test_job_lifecycle
from assistant_runtime.plugins.codex.api_jobs import RoutedJobs, ApiJobs
from assistant_runtime.plugins.codex.api_catalog import catalog


class ApiJobsTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_job_lifecycle.JobLifecycleTest.asyncSetUp
    asyncTearDown = test_job_lifecycle.JobLifecycleTest.asyncTearDown
    rpc = test_job_lifecycle.JobLifecycleTest.rpc

    def router(self):
        self.config.model_api = test_models.settings()
        self.config.model_api["entries"]["test-model"]["execution_validated"] = True
        host = SimpleNamespace(
            config=self.config,
            backend=self.backend,
            mcp_registry=None,
            runtime=SimpleNamespace(
                contexts={
                    "tasks": SimpleNamespace(
                        own=Mock(), spawn=Mock(side_effect=lambda coroutine, _: coroutine.close())
                    )
                }
            ),
        )
        routed = RoutedJobs(host, Mock(return_value=True))
        routed.subscription = SimpleNamespace(
            models=AsyncMock(return_value=[{"model": "subscription"}]),
            select=AsyncMock(return_value={"model": "subscription"}),
            finalize=AsyncMock(),
            upgrade_ready=AsyncMock(),
        )
        return routed

    async def test_derived_catalog_exposes_patch_without_subscription_wire_features(self):
        routed = self.router()
        metadata = catalog(routed.settings)["models"][0]
        self.assertEqual(metadata["slug"], "test-model")
        self.assertEqual(metadata["apply_patch_tool_type"], "freeform")
        self.assertFalse(metadata["use_responses_lite"])
        self.assertFalse(metadata["supports_reasoning_effort_updates"])
        self.assertEqual(metadata["input_modalities"], ["text"])
        self.assertEqual(metadata["context_window"], 32768)

    async def test_catalog_and_selection_do_not_start_api_or_inherit_effort(self):
        routed = self.router()
        routed.api_jobs = AsyncMock(side_effect=AssertionError("must stay lazy"))
        self.assertEqual(len(await routed.models()), 2)
        self.assertEqual(
            await routed.select("test-model", "none"), {"model": "test-model", "reasoning_effort": "none"}
        )
        with self.assertRaisesRegex(ValueError, "validated"):
            await routed.select("test-model", "high")
        routed.subscription.select.assert_not_awaited()
        routed.settings.entries["test-model"]["execution_validated"] = False
        self.assertEqual(len(await routed.models()), 1)

    async def test_every_configured_effort_is_available_to_task_advice_and_execution(self):
        from assistant_runtime.plugins.tasks.selection import ModelPolicy

        routed = self.router()
        levels = ["none", "low", "medium", "high", "xhigh", "max"]
        routed.settings.entries["test-model"].update(
            reasoning_efforts=levels, default_reasoning_effort="medium"
        )
        options = ModelPolicy().options(
            await routed.models(),
            {"model": "test-model", "reason": "Explicit model, choose appropriate supported effort"},
            {"model": "auto", "reasoning_effort": "auto"},
        )
        self.assertEqual({o["reasoning_effort"] for o in options.values()}, set(levels))
        for effort in levels:
            self.assertEqual(
                await routed.select("test-model", effort), {"model": "test-model", "reasoning_effort": effort}
            )
        with self.assertRaisesRegex(ValueError, "validated"):
            await routed.select("test-model", "ultra")

    async def test_process_credentials_and_lifecycle_are_isolated(self):
        routed = self.router()
        client = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        with (
            patch(
                "assistant_runtime.plugins.codex.api_jobs.gateway_credential",
                return_value="private-gateway-key",
            ),
            patch("assistant_runtime.plugins.codex.api_jobs.CodexClient", return_value=client) as constructor,
        ):
            api = await routed.api_jobs()
            self.assertIs(api, await routed.api_jobs())
        kwargs = constructor.call_args.kwargs
        self.assertEqual(kwargs["environment"], {"ASSISTANT_CODEX_GATEWAY_KEY": "private-gateway-key"})
        self.assertEqual(kwargs["overrides"]["mcp_optional_startup_grace_ms"], 0)
        provider = kwargs["overrides"]["model_providers.assistant_api"]
        self.assertFalse(provider["requires_openai_auth"])
        self.assertEqual(provider["request_max_retries"], 0)
        self.assertIsNot(api.client, self.backend.client)
        self.assertEqual(routed.context.own.call_count, 2)

    async def test_api_start_failure_does_not_fall_back(self):
        routed = self.router()
        client = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError("offline")), close=AsyncMock())
        with (
            patch("assistant_runtime.plugins.codex.api_jobs.gateway_credential", return_value="secret"),
            patch("assistant_runtime.plugins.codex.api_jobs.CodexClient", return_value=client),
        ):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                await routed.api_jobs()
            with self.assertRaisesRegex(RuntimeError, "restart required"):
                await routed.api_jobs()
        client.start.assert_awaited_once()
        routed.subscription.select.assert_not_awaited()

    async def test_event_handler_failure_closes_executor_and_preserves_unknown(self):
        routed = self.router()
        events = asyncio.Queue()
        events.put_nowait({"method": "synthetic/event"})
        client = SimpleNamespace(events=events, close=AsyncMock())
        routed.api = SimpleNamespace(
            client=client,
            backend=SimpleNamespace(integrations=None),
            handles={"active-run": {}},
            event=AsyncMock(side_effect=ValueError("invalid native event")),
        )
        await routed.events()
        self.assertTrue(routed.api_failed)
        self.assertEqual(routed.notify.call_args.args, ("active-run", "unknown"))
        client.close.assert_awaited_once()
        with self.assertRaisesRegex(RuntimeError, "restart required"):
            await routed.api_jobs()

    async def test_cross_provider_upgrade_creates_new_thread_from_checkpoint(self):
        routed = self.router()
        api = SimpleNamespace(launch=AsyncMock())
        routed.api_jobs = AsyncMock(return_value=api)
        run = copy.deepcopy(self.run)
        run["data"].update(
            execution_backend="subscription",
            model_upgrades=[
                {
                    "phase": "dispatching",
                    "request": {"checkpoint": {"completed": ["operation-id"], "next_step": "read back"}},
                }
            ],
        )
        run["data"]["spec"]["execution"].update(model="test-model", reasoning_effort="none")
        await routed.launch(run, self.area)
        routed.subscription.upgrade_ready.assert_awaited_once()
        routed.subscription.finalize.assert_awaited_once()
        launched = api.launch.call_args.args[0]
        self.assertIsNone(launched["data"]["thread_id"])
        self.assertIn("operation-id", launched["data"]["continuation"])
        self.assertEqual(run["data"]["thread_id"], "background")

    async def test_existing_thread_cannot_change_provider_without_checkpoint(self):
        routed = self.router()
        self.run["data"]["spec"]["execution"]["model"] = "test-model"
        with self.assertRaisesRegex(ValueError, "verified checkpoint"):
            await routed.launch(self.run, self.area)

    async def test_provider_identity_sent_and_no_model_fallback(self):
        routed = self.router()
        self.backend.model_provider = "assistant_api"
        original = self.client.request.side_effect

        async def rpc(method, params, **kwargs):
            result = await original(method, params, **kwargs)
            if method == "thread/start":
                result["modelProvider"] = "assistant_api"
            return result

        self.client.request.side_effect = rpc
        runner = ApiJobs(self.backend, self.notify, routed.settings)
        self.run["data"]["thread_id"] = None
        self.run["data"]["spec"]["execution"].update(model="test-model", reasoning_effort="none")
        await runner.launch(self.run, self.area)
        request = next(c for c in self.client.request.call_args_list if c.args[0] == "thread/start")
        self.assertEqual(request.args[1]["modelProvider"], "assistant_api")
        self.assertFalse(request.args[1]["allowProviderModelFallback"])
        self.assertTrue(request.args[1]["config"]["features.default_mode_request_user_input"])
