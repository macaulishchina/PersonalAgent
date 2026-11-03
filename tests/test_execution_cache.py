"""Real Pi requests: independent execution directories with stable prompt prefixes."""

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from assistant_runtime.api import ExecutionRequest
from assistant_runtime.app.config import Config
from assistant_runtime.app.store import Store
from assistant_runtime.core.ai_usage.service import UsageService
from assistant_runtime.core.execution.service import ExecutionService
from assistant_runtime.core.pi.plugin import PiPlugin
from assistant_runtime.core.pi.native_telemetry import telemetry
from assistant_runtime.core.workfiles import WorkFiles
from assistant_runtime.kernel.runtime import Manifest, Runtime
from pi_gateway import FakeResponsesGateway, sse_text_reply, sse_tool_call
from test_pi_skeleton import configuration


class PrefixTest(unittest.IsolatedAsyncioTestCase):
    async def test_two_public_executions_keep_prompt_stable_and_artifacts_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def reply(step, body):
                if step % 2:
                    return sse_tool_call(
                        "write", "write", {"path": "../artifacts/proof.txt", "content": "CACHE_ARTIFACT"}
                    )
                return sse_text_reply("done", "done")

            gateway = FakeResponsesGateway(reply)
            (root / ".env").write_text("TEST_KEY=synthetic-key\n")
            config = Config(
                root=root,
                workspace=str(root),
                plugins=["ai_usage", "models", "pi"],
                model_api=configuration(gateway.base_url),
            )
            store = Store(root / "execution.sqlite3")
            usage = UsageService(root / "usage")
            pi = PiPlugin(config, store, usage=usage, foreground=False)
            files = WorkFiles(root, store.db)
            service = ExecutionService(
                pi.backend.client,
                store.db,
                files,
                usage,
                default_model="model-a",
                default_effort="low",
                servers=lambda: {},
            )
            runtime = Runtime(store)

            async def start_usage(context):
                context.own(usage.close)
                await usage.start()

            runtime.add(SimpleNamespace(manifest=Manifest("ai_usage"), start=start_usage))
            runtime.add(pi)
            runtime.add(SimpleNamespace(manifest=Manifest("execution", ("pi",)), start=service.start))
            pi.backend.executions = service
            try:
                await runtime.start()
                text = "Frozen synthetic material. " * 600 + "\nQuestion: write the proof artifact."
                rows = []
                for _ in range(2):
                    args = {"request_id": str(uuid.uuid4()), "text": text, "tools": ["write"], "skills": []}
                    row = await runtime.invoke(ExecutionRequest("submit", args))
                    async with asyncio.timeout(25):
                        while row["state"] not in {"completed", "failed", "unknown"}:
                            await asyncio.sleep(0.02)
                            row = await runtime.invoke(ExecutionRequest("get", {"id": row["id"]}))
                    self.assertEqual(row["state"], "completed", row)
                    self.assertEqual(
                        (await runtime.invoke(ExecutionRequest("submit", args)))["id"], row["id"]
                    )
                    rows.append(row)
                self.assertEqual(len(gateway.requests), 4)  # No warming/replay requests.
                first, second = gateway.requests[0], gateway.requests[2]
                self.assertEqual(first["tools"], second["tools"])
                self.assertEqual(first["input"], second["input"])
                for row in rows:
                    self.assertNotIn(row["id"], json.dumps(first["input"]))
                    area = files.area("executions/" + row["id"])
                    self.assertEqual((area / "artifacts/proof.txt").read_text(), "CACHE_ARTIFACT")
                self.assertNotEqual(rows[0]["id"], rows[1]["id"])
                # Native affinity remains per session; caching never joins identities.
                self.assertNotEqual(first.get("prompt_cache_key"), second.get("prompt_cache_key"))
            finally:
                await runtime.close()
                store.db.close()
                await gateway.aclose()


class CacheTelemetryTest(unittest.TestCase):
    def test_absent_and_sdk_normalized_zero_cache_counts_remain_unknown(self):
        for value in (
            {"input": 100, "output": 2},
            {"input": 100, "output": 2, "cacheRead": 0, "cacheWrite": 0},
        ):
            actual = telemetry({"usage": value})
            self.assertIsNone(actual["cached_input_tokens"])
            self.assertIsNone(actual["cache_write_input_tokens"])
            self.assertEqual(actual["input_tokens"], 100)
        actual = telemetry({"usage": {"input": 10, "output": 2, "cacheRead": 90, "cacheWrite": 7}})
        self.assertEqual(actual["cached_input_tokens"], 90)
        self.assertEqual(actual["cache_write_input_tokens"], 7)
        self.assertEqual(actual["input_tokens"], 107)
