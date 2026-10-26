"""Failures remain attributable to one model and one dispatch, including lost replies."""

import asyncio
import copy
import unittest

import test_models
import test_tasks
from assistant_runtime.api.models import ModelCallError
from assistant_runtime.core.models.settings import ModelSettings
from assistant_runtime.core.tasks.selection import ModelPolicy
from assistant_runtime.core.pi.failures import model_failure


class InferenceFailureTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_models.ModelsTest.asyncSetUp
    asyncTearDown = test_models.ModelsTest.asyncTearDown
    request = test_models.ModelsTest.request

    async def test_provider_rejections_fail_once_and_receipt_survives_duplicate(self):
        for status in (401, 402, 403, 429, 500):
            with self.subTest(status=status):
                self.client.complete.reset_mock()
                self.client.complete.side_effect = ModelCallError("model_http_" + str(status))
                request = self.request()
                with self.assertRaises(ModelCallError):
                    await self.runtime.invoke(request)
                result = await self.runtime.invoke(request)
                self.assertEqual(result["state"], "failed")
                self.assertEqual(len(result["attempts"]), 1)
                self.assertEqual(result["attempts"][0]["model"], "test-model")
                self.assertEqual(result["attempts"][0]["state"], "failed")
                self.client.complete.assert_awaited_once()

    async def test_timeout_disconnect_and_cancel_remain_unknown_without_replay(self):
        for error in (TimeoutError(), RuntimeError("stream disconnected"), asyncio.CancelledError()):
            with self.subTest(error=type(error)):
                self.client.complete.reset_mock()
                self.client.complete.side_effect = error
                request = self.request()
                with self.assertRaises(type(error)):
                    await self.runtime.invoke(request)
                result = await self.runtime.invoke(request)
                self.assertEqual(result["state"], "unknown")
                self.assertEqual(len(result["attempts"]), 1)
                self.client.complete.assert_awaited_once()

    def test_retired_configuration_is_rejected_instead_of_ignored(self):
        raw = copy.deepcopy(self.host.config.model_api)
        raw["entries"]["test-model"]["fallbacks"] = ["test-model"]
        with self.assertRaises(ValueError):
            ModelSettings.parse(raw)
        for data in ({"fallbacks": {}}, {"profiles": {}}, {"default_profile": "fast"}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                ModelPolicy.parse(data)


class TaskFailureTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_tasks.TasksTest.asyncSetUp
    asyncTearDown = test_tasks.TasksTest.asyncTearDown
    create = test_tasks.TasksTest.create

    async def test_failed_task_has_no_alternative_model_dispatch(self):
        definition = await self.create()
        self.plugin.due(definition["id"])
        run = self.plugin.repo.runs(definition["id"])[0]
        await self.plugin.launch(run)
        self.plugin.provider_update(
            run["id"], "failed", turn_active=False, model_failure=model_failure({"status": 402})
        )
        failed = self.plugin.repo.run(run["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["data"]["model_failure"], {"code": "model_http_402"})
        self.plugin.provider_update(run["id"], "completed", turn_active=False)
        self.assertEqual(self.plugin.repo.run(run["id"])["state"], "failed")
        self.executor.launch.assert_awaited_once()
        self.assertEqual(len(failed["data"]["model_selections"]), 1)
