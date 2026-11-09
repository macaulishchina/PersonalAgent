"""Real pinned Pi + local synthetic model + real media/task owners, no provider IO."""

import asyncio
from pathlib import Path
import tempfile
import unittest
import uuid

from assistant_runtime.api.contracts import TaskRequest, MediaRequest
from assistant_runtime.app.config import Config
from assistant_runtime.app.host import AssistantService
from pi_gateway import FakeResponsesGateway, sse_text_reply
import test_pi_skeleton as fixtures
from test_media_library import ROOT


class BusinessPiTest(unittest.IsolatedAsyncioTestCase):
    async def test_headless_task_cannot_report_unknown_media_operation_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("TEST_KEY=synthetic-key\n")
            gateway = FakeResponsesGateway(lambda step, body: sse_text_reply("answer", "已全部删除成功"))
            config = Config(
                root=root,
                workspace=directory,
                plugins=["ai_usage", "models", "workfiles", "pi", "execution", "tasks", "media"],
                main_model="model-a",
                main_reasoning_effort="auto",
                model_api=fixtures.configuration(gateway.base_url),
                tasks={"model_policy": {"review_mode": "off"}},
                plugin_settings={"media": {"targets": {"series": ROOT}}},
            )
            host = AssistantService(config)
            running = asyncio.create_task(host.run())
            try:
                await asyncio.wait_for(host.runtime.ready.wait(), 30)
                self.assertEqual(host.operation_sources, {"media": MediaRequest})
                tasks, media = host.runtime.plugins["tasks"], host.runtime.plugins["media"]
                self.assertFalse(media.repo.notifications_enabled)
                tasks.scheduler.pause()
                definition = await host.runtime.invoke(
                    TaskRequest(
                        "create",
                        {
                            "request_id": str(uuid.uuid4()),
                            "spec": {
                                "title": "Synthetic result",
                                "prompt": "Return the synthetic final answer",
                                "trigger": {"kind": "interval", "seconds": 3600},
                                "auto_archive": False,
                                "execution": {"model": "model-a", "effort_mode": "model_default"},
                            },
                        },
                    )
                )
                run = tasks.repo.enqueue(definition, "synthetic")
                operation = media.repo.create(
                    str(uuid.uuid4()),
                    {
                        "kind": "library",
                        "target": "series",
                        "destination": ROOT,
                        "plan_id": "synthetic",
                        "file_id": "folder",
                        "share_id": "library",
                    },
                    "task:synthetic",
                    run["id"],
                    initial={"steps": []},
                )
                media.repo.change(
                    operation["id"], "uncertain", submitted=True, error="provider_network_error"
                )
                async with asyncio.timeout(30):
                    while True:
                        current = tasks.repo.run(run["id"])
                        if current["data"].get("completion_delivered"):
                            break
                        await asyncio.sleep(0.05)
                self.assertEqual(current["state"], "completed")
                self.assertEqual(current["data"]["business_result"]["state"], "unknown")
                self.assertNotIn("已全部删除成功", current["data"]["progress"])
                self.assertEqual(current["data"]["completion_feedback"]["route"], "query")
                self.assertEqual(len(gateway.requests), 1)
                media.repo.change(operation["id"], "completed", submitted=False)
                await tasks.refresh_business(current)
                self.assertEqual(tasks.repo.run(run["id"])["data"]["business_result"]["state"], "completed")
                self.assertEqual(len(gateway.requests), 1)
                self.assertEqual(media.repo.db.execute("SELECT count(*) FROM media_outbox").fetchone()[0], 0)
            finally:
                host.stop_event.set()
                await asyncio.wait_for(running, 30)
                await gateway.aclose()
