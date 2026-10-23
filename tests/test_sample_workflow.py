"""Installed external package plus real Pi: input, tools, report, shared budget."""

import asyncio
from dataclasses import replace
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import uuid
import zipfile

from assistant_runtime.api import CallIdentity
from assistant_runtime.api.ai_usage import UsageScope, current_usage
from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.api import ExecutionRequest
from assistant_runtime.app.config import Config
from assistant_runtime.app.host import AssistantService
from assistant_runtime.app.initialize import initialize
from pi_gateway import FakeResponsesGateway, sse_text_reply, sse_tool_call
from test_pi_skeleton import configuration


class InstalledWorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def test_slim_installed_report_and_optional_tasks_without_conversation(self):
        source = Path(__file__).resolve().parents[1] / "examples/sample-plugin"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copytree(source, root / "package", ignore=shutil.ignore_patterns("build", "*.egg-info"))
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "from setuptools.build_meta import build_wheel; build_wheel('../dist')",
                cwd=root / "package",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            output, errors = await asyncio.wait_for(process.communicate(), 30)
            self.assertEqual(process.returncode, 0, (output + errors).decode())
            installed = root / "installed"
            with zipfile.ZipFile(next((root / "dist").glob("*.whl"))) as wheel:
                wheel.extractall(installed)
            request_id = str(uuid.uuid4())
            instance = root / "instance"

            def script(step, body):
                if step == 1:
                    path = next(
                        (instance / "state/work/executions" / request_id / "scratch").glob("input-*.txt")
                    )
                    return sse_tool_call("input", "read", {"path": str(path)})
                if step == 2:
                    return sse_tool_call(
                        "report",
                        "write",
                        {"path": "../artifacts/report.md", "content": "Verified synthetic report"},
                    )
                return sse_text_reply("report-result", "REPORT_OK")

            gateway = FakeResponsesGateway(script)
            config = Config.load(initialize(instance, preset="slim"))
            config = replace(
                config,
                workspace=str(root),
                plugins=[*config.plugins, "sample", "tasks", "notifications"],
                notifications={"channels": ["inbox"], "voice_enabled": False},
                model_api=configuration(gateway.base_url),
                main_model="model-a",
                main_reasoning_effort="low",
                tasks={
                    "model_policy": {
                        "profiles": {
                            p: {"model": "model-a", "reasoning_effort": "low"}
                            for p in ("fast", "balanced", "deep", "ultra")
                        }
                    }
                },
            )
            (config.root / ".env").write_text("ASSISTANT_MODEL_GATEWAY_KEY=synthetic-key\n")
            old_path = sys.path[:]
            sys.path.insert(0, str(installed))
            host, running = None, None
            try:
                host = AssistantService(config)
                running = asyncio.create_task(host.run())
                await asyncio.wait_for(host.runtime.ready.wait(), 20)
                self.assertIsNone(host.assistant)
                self.assertFalse({"conversation", "devices", "voice_disabled"} & host.runtime.plugins.keys())
                identity = CallIdentity("operator", "sample-work")
                token = current_usage.set(UsageScope("shared-example", "operator"))
                try:
                    result = await host.extension_tools.invoke(
                        "sample_report",
                        {"request_id": request_id, "text": "Example input"},
                        identity=identity,
                    )
                finally:
                    current_usage.reset(token)
                async with asyncio.timeout(25):
                    while result["state"] not in {"completed", "failed", "unknown"}:
                        await asyncio.sleep(0.05)
                        result = await host.extension_tools.invoke(
                            "sample_report_status", {"id": request_id}, identity=identity
                        )
                self.assertEqual(result["state"], "completed", result)
                self.assertEqual(result["artifacts"], [{"name": "report.md", "bytes": 25}])
                count = len(gateway.requests)
                duplicate = await host.extension_tools.invoke(
                    "sample_report", {"request_id": request_id, "text": "Example input"}, identity=identity
                )
                self.assertEqual(duplicate["id"], result["id"])
                self.assertEqual(len(gateway.requests), count)
                scope = await host.ai_usage.service.binding(request_id)
                self.assertEqual(scope.work_id, "shared-example")
                self.assertEqual(host.backend.executions.records.get(request_id)["owner"], "sample-work")
                task = await host.runtime.invoke(
                    TaskRequest(
                        "create",
                        {
                            "request_id": str(uuid.uuid4()),
                            "spec": {
                                "title": "Headless task",
                                "prompt": "Synthetic task",
                                "execution": {
                                    "model": "model-a",
                                    "reasoning_effort": "low",
                                    "reason": "Synthetic known workflow, verify the returned report",
                                },
                            },
                        },
                    )
                )
                async with asyncio.timeout(25):
                    while True:
                        info = await host.runtime.invoke(TaskRequest("get", {"task_id": task["id"]}))
                        runs = info["runs"]
                        if runs and runs[0]["state"] in {"completed", "failed", "unknown"}:
                            break
                        await asyncio.sleep(0.05)
                self.assertEqual(runs[0]["state"], "completed", runs)
                self.assertEqual(runs[0]["execution"]["state"], "completed")
                self.assertEqual(runs[0]["execution"]["source"], "task")
                # The same slim host discovers an installed plugin through public execution.
                await host.extension_tools.invoke(
                    "sample_write", {"text": "HEADLESS_NOTE"}, identity=identity
                )
                start = len(gateway.requests)

                def discover(step, body):
                    index = step - start
                    if index == 1:
                        self.assertEqual([t["name"] for t in body["tools"]], ["tool_search"])
                        return sse_tool_call("discover", "tool_search", {"query": "sample_read", "limit": 1})
                    if index == 2:
                        self.assertIn("mcp__extensions__sample_read", str(body["tools"]))
                        return sse_tool_call("read-note", "mcp__extensions__sample_read", {})
                    self.assertIn("HEADLESS_NOTE", str(body))
                    return sse_text_reply("headless-discovery", "DISCOVERY_OK")

                gateway.script_fn = discover
                execution = await host.runtime.invoke(
                    ExecutionRequest(
                        "submit",
                        {
                            "request_id": str(uuid.uuid4()),
                            "text": "Read the synthetic plugin note",
                            "tools": ["mcp__extensions__sample_read"],
                            "skills": [],
                        },
                    )
                )
                async with asyncio.timeout(25):
                    while execution["state"] not in {"completed", "failed", "unknown"}:
                        await asyncio.sleep(0.05)
                        execution = await host.runtime.invoke(
                            ExecutionRequest("get", {"id": execution["id"]})
                        )
                self.assertEqual(execution["state"], "completed", execution)
                self.assertIn("DISCOVERY_OK", execution["result"])

            finally:
                if host and running:
                    host.stop_event.set()
                    await asyncio.wait_for(running, 20)
                sys.path[:] = old_path
                sys.modules.pop("personal_agent_sample", None)
                await gateway.aclose()
