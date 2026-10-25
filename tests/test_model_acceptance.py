"""Acceptance scripts enforce real answers and meter structured inference before dispatch."""

import json
from pathlib import Path
import tempfile
import unittest
import uuid

from pi_gateway import FakeResponsesGateway, release_host_target, sse_text_reply, sse_tool_call
from test_pi_skeleton import configuration
from assistant_runtime.app.config import Config
from scripts.verify_models import verify
from scripts.verify_pi_host import verify as verify_host


class AcceptanceTest(unittest.IsolatedAsyncioTestCase):
    async def test_host_acceptance_uses_real_task_mcp_and_hard_shared_request_limit(self):
        for cap in (6, 1):
            counts = {}
            target = None

            def script(step, body):
                nonlocal target
                model = body["model"]
                counts[model] = counts.get(model, 0) + 1
                if target is None:
                    target = release_host_target(body)
                if model == "model-a" and counts[model] == 1:
                    return sse_tool_call(
                        "discover",
                        "tool_search",
                        {"query": "create task request_id title prompt", "limit": 8},
                    )
                if model == "model-a" and counts[model] == 2:
                    return sse_tool_call(
                        "delegate",
                        "mcp__tasks__create_task",
                        {
                            "request_id": str(uuid.uuid4()),
                            "feedback_owner": "conversation",
                            "title": "Synthetic bounded release task",
                            "prompt": "Write SYNTHETIC_OK to " + target,
                            "execution": {
                                "model": "model-b",
                                "reasoning_effort": "low",
                                "model_source": "user",
                                "effort_source": "user",
                                "reason": "Synthetic file readback",
                            },
                        },
                    )
                if model == "model-b" and counts[model] == 1:
                    return sse_tool_call("write", "write", {"path": target, "content": "SYNTHETIC_OK"})
                return sse_text_reply("done" + str(step), "已完成")

            gateway = FakeResponsesGateway(script)
            try:
                with tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    (root / ".env").write_text("ASSISTANT_MODEL_GATEWAY_KEY=synthetic-key\n")
                    config = Config(root=root, model_api=configuration(gateway.base_url))

                    result = await verify_host(config, "model-a", "model-b", max_requests=cap)
                    self.assertFalse(result["production_changed"])
                    if cap == 6:
                        self.assertEqual(result["state"], "completed", result)
                        self.assertEqual(result["task_count"], 1)
                        self.assertEqual(result["callers"], 2)
                        self.assertEqual(result["requests"], 5)
                    else:
                        self.assertEqual(result["state"], "failed", result)
                        self.assertEqual(len(gateway.requests), 1)
                        self.assertEqual(result["stage"], "before_delegation")
                        self.assertEqual(result["task_count"], 0)
            finally:
                await gateway.aclose()

    async def test_memory_uses_structured_format_and_usage_ledger(self):
        expected = {"fact": "likes tea", "source_id": 1, "evidence": "likes tea"}
        gateway = FakeResponsesGateway(lambda step, body: sse_text_reply("memory", json.dumps(expected)))
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / ".env").write_text("ASSISTANT_MODEL_GATEWAY_KEY=synthetic-key\n")
                config = Config(root=root, model_api=configuration(gateway.base_url))
                result = await verify(config, "model-a", case="memory", max_requests=1)
                self.assertEqual(result["state"], "completed", result)
                self.assertEqual(result["ai_usage"]["requests"], 1)
                self.assertEqual(result["requests"], 1)
                self.assertEqual(gateway.requests[0]["text"]["format"]["type"], "json_schema")
        finally:
            await gateway.aclose()

    async def test_generic_success_text_cannot_pass_text_or_vision_acceptance(self):
        gateway = FakeResponsesGateway(
            lambda step, body: sse_text_reply("generic", "I received your request")
        )
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / ".env").write_text("ASSISTANT_MODEL_GATEWAY_KEY=synthetic-key\n")
                config = Config(root=root, model_api=configuration(gateway.base_url))
                for case in ("text", "image"):
                    result = await verify(config, "model-a", case=case, max_requests=1)
                    self.assertEqual(result["state"], "failed", result)
                    self.assertEqual(result["error"], "ValueError")
                    self.assertEqual(result["requests"], 1)
        finally:
            await gateway.aclose()
