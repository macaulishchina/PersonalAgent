"""Owner evidence survives IPC/MCP; unexpected handler errors remain uncertain."""

import unittest
from unittest.mock import AsyncMock
from fastmcp import FastMCP, Client
from assistant_runtime.api.tool_calls import ToolOutcome, ToolFailure, invalid_arguments
from assistant_runtime.mcp.outcomes import ToolOutcomes
from assistant_runtime.core.tasks.tool_contracts import EXECUTION, CHANGES, merge_patch
from assistant_runtime.mcp.contracts import validate


class OutcomeTest(unittest.IsolatedAsyncioTestCase):
    async def test_media_owner_failure_preserves_code_and_recovery_through_ipc_and_mcp(self):
        from assistant_runtime.plugins.media.errors import MediaError
        from assistant_runtime.plugins.media.plugin import MediaPlugin
        from assistant_runtime.plugins.media.tools import create_server
        from assistant_runtime.mcp.client import LocalRuntimeClient
        from assistant_runtime.platforms.ipc import serve
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        import json

        async def invoke(request):
            plugin = object.__new__(MediaPlugin)
            plugin._request = AsyncMock(side_effect=MediaError("provider_file_identity_mismatch"))
            return await plugin.request(request)

        async def reply(reader, writer):
            try:
                payload = json.loads(await reader.readline())
                try:
                    await invoke(SimpleNamespace(action=payload["action"]))
                except ToolFailure as error:
                    value = {"ok": False, "error": str(error), "tool_outcome": error.outcome.public()}
                writer.write((json.dumps(value) + "\n").encode())
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        with TemporaryDirectory() as directory:
            path = Path(directory) / "control.sock"
            listener = await serve(path, reply)
            try:
                server = create_server(LocalRuntimeClient(path))
                server.add_middleware(ToolOutcomes())
                async with Client(server) as client:
                    result = await client.call_tool(
                        "inspect_media_share",
                        {"share_url": "https://pan.quark.cn/s/abc123"},
                        raise_on_error=False,
                    )
                    outcome = result.structured_content["tool_outcome"]
                    self.assertEqual(outcome["code"], "provider_file_identity_mismatch")
                    self.assertEqual(outcome["outcome"], "failed")
                    self.assertFalse(outcome["retryable"])
                    self.assertEqual(outcome["next_action"], "inspect_provider_response")
                    self.assertNotEqual(outcome["next_action"], "inspect_operation")
            finally:
                listener.close()
                await listener.wait_closed()

    async def test_unexpected_read_failure_is_recoverable_but_write_remains_unknown(self):
        server = FastMCP("sample")
        server.add_middleware(ToolOutcomes())

        @server.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
        def read_value() -> str:
            raise RuntimeError("private-response-with-secret")

        @server.tool()
        def write_value() -> str:
            raise RuntimeError("private-response-with-secret")

        async with Client(server) as client:
            for name in ("read_value", "write_value"):
                result = await client.call_tool(name, {}, raise_on_error=False)
                outcome = result.structured_content["tool_outcome"]
                self.assertEqual(outcome["outcome"], "failed" if name == "read_value" else "unknown")
                self.assertEqual(
                    outcome["next_action"], "retry_read" if name == "read_value" else "inspect_operation"
                )
                self.assertNotIn("private-response", str(result))

    async def test_owner_retry_classification_never_replays_uncertain_writes(self):
        from assistant_runtime.plugins.media.errors import MediaError

        for action, uncertain, retryable in (
            ("inspect", False, True),
            ("transfer", False, False),
            ("inspect", True, False),
        ):
            outcome = MediaError("provider_network_error", uncertain=uncertain).tool_failure(action).outcome
            self.assertEqual(outcome.retryable, retryable)
            self.assertEqual(outcome.outcome, "failed" if retryable else "unknown")

    async def test_complex_first_party_inputs_publish_fields_and_reject_before_dispatch(self):
        from types import SimpleNamespace
        from assistant_runtime.core.events.tools import create_server as events
        from assistant_runtime.core.integrations.tools import create_server as integrations
        from assistant_runtime.plugins.media.tools import create_server as media

        cases = [
            (events, "record_event", {"record": {"type": "note"}}, "record"),
            (
                integrations,
                "configure_mcp_server",
                {"name": "example", "revision": 0, "changes": {"transport": {"type": "stdio"}}},
                "changes",
            ),
            (
                media,
                "preview_media_changes",
                {"target": "example", "changes": [{"action": "rename", "file_id": "example"}]},
                "changes",
            ),
        ]
        for factory, name, arguments, field in cases:
            with self.subTest(tool=name):
                caller = SimpleNamespace(invoke=AsyncMock())
                server = factory(caller)
                server.add_middleware(ToolOutcomes())
                async with Client(server) as client:
                    tools = {tool.name: tool for tool in await client.list_tools()}
                    self.assertIn(field, tools[name].input_schema["properties"])
                    invalid = await client.call_tool(name, arguments, raise_on_error=False)
                    self.assertTrue(invalid.is_error)
                    self.assertEqual(invalid.structured_content["tool_outcome"]["dispatch"], "not_dispatched")
                    self.assertTrue(invalid.structured_content["tool_outcome"]["field"].startswith(field))
                    caller.invoke.assert_not_awaited()

    async def test_all_bundled_tool_factories_have_valid_contracts_without_dispatch(self):
        from importlib import import_module
        from types import SimpleNamespace
        from jsonschema import Draft202012Validator
        from assistant_runtime.core.plugin_catalog import services

        caller = SimpleNamespace(invoke=AsyncMock())

        def check_open_schema(schema, inherited_description=False):
            if not isinstance(schema, dict):
                return
            documented = bool(schema.get("description")) or inherited_description
            if (
                schema.get("type") == "object"
                and "properties" not in schema
                and schema.get("additionalProperties", True) is True
            ):
                self.assertTrue(documented, "Unstructured object requires an explicit purpose")
            for key in ("properties", "$defs", "patternProperties"):
                for child in schema.get(key, {}).values():
                    check_open_schema(child, documented)
            for key in ("anyOf", "oneOf", "allOf"):
                for child in schema.get(key, []):
                    check_open_schema(child, documented)
            for key in ("items", "additionalProperties"):
                if isinstance(schema.get(key), dict):
                    check_open_schema(schema[key], documented)

        for service, spec in services().items():
            with self.subTest(service=service):
                module, attribute = spec.tools_factory.split(":")
                kwargs = {"caller_run": None} if spec.task_context else {}
                if service == "integrations":
                    kwargs["can_manage"] = True
                server = getattr(import_module(module), attribute)(caller, **kwargs)
                async with Client(server) as client:
                    tools = await client.list_tools()
                    self.assertTrue(tools, service)
                    self.assertEqual(len({tool.name for tool in tools}), len(tools))
                    for tool in tools:
                        self.assertTrue(tool.description and tool.description.strip(), tool.name)
                        Draft202012Validator.check_schema(tool.input_schema)
                        check_open_schema(tool.input_schema)
                        if tool.output_schema is not None:
                            Draft202012Validator.check_schema(tool.output_schema)
                        self.assertIsNotNone(tool.annotations, tool.name)
                        self.assertIsInstance(tool.annotations.read_only_hint, bool, tool.name)
                        self.assertFalse(
                            tool.annotations.read_only_hint and tool.annotations.destructive_hint, tool.name
                        )
                        validator = Draft202012Validator(tool.input_schema)
                        for example in tool.input_schema.get("examples", []):
                            validator.validate(example)
        caller.invoke.assert_not_awaited()

    async def test_registration_validates_examples_and_pins_the_declared_schema(self):
        from dataclasses import replace
        from assistant_runtime.api import Tool, CallIdentity
        from assistant_runtime.app.extensions import ToolRegistry

        handler = AsyncMock(return_value={"ok": True})
        schema = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
            "examples": [{"count": 1}],
        }
        tool = Tool("sample", "Sample operation", schema, {"type": "object"}, handler, True)
        registry = ToolRegistry()
        with self.assertRaisesRegex(ValueError, "callable"):
            registry.register("sample", replace(tool, handler=None))
        self.assertEqual(registry.describe(), {"tools": []})
        handler.assert_not_awaited()
        registry.register("sample", tool)
        schema["properties"]["count"]["type"] = "string"
        registry.describe()["tools"][0]["input_schema"]["properties"]["count"]["type"] = "number"
        await registry.invoke("sample", {"count": 2}, identity=CallIdentity("r", "w"))
        with self.assertRaises(ToolFailure):
            await registry.invoke("sample", {"count": "changed"}, identity=CallIdentity("r", "w"))
        handler.assert_awaited_once()

    async def test_optional_metadata_problems_warn_without_blocking_direct_or_mcp_calls(self):
        from assistant_runtime.api import Tool, CallIdentity
        from assistant_runtime.app.extensions import ToolRegistry
        from assistant_runtime.mcp.extensions import create_server

        handler = AsyncMock(return_value={"ok": True})
        schema = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "examples": [{"count": "do-not-log"}, {"count": 1}],
        }
        tool = Tool(
            "sample",
            None,
            schema,
            {"type": "object", "examples": "do-not-log"},
            handler,
            read_only="true",
            destructive="false",
            idempotent="true",
            external="false",
        )
        registry = ToolRegistry()
        with self.assertLogs("assistant_runtime.app.extensions", level="WARNING") as logs:
            registry.register("sample", tool)
        self.assertNotIn("do-not-log", str(logs.output))
        current = registry.describe()["tools"][0]
        self.assertEqual(current["description"], "sample")
        self.assertEqual(current["input_schema"]["examples"], [{"count": 1}])
        self.assertNotIn("examples", current["output_schema"])
        self.assertEqual(
            current["annotations"],
            {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
        )
        identity = CallIdentity("request", "work")
        await registry.invoke("sample", {"count": 1}, identity=identity)
        server = create_server(registry, lambda: identity)
        server.add_middleware(ToolOutcomes())
        async with Client(server) as client:
            result = await client.call_tool("sample", {"count": 2})
            self.assertFalse(result.is_error)
            invalid = await client.call_tool("sample", {"count": "not-an-integer"}, raise_on_error=False)
            self.assertTrue(invalid.is_error)
        self.assertEqual(handler.await_count, 2)
        self.assertEqual(schema["examples"][0]["count"], "do-not-log")

    async def test_missing_or_conflicting_behavior_hints_use_sequential_defaults(self):
        from assistant_runtime.api import Tool
        from assistant_runtime.app.extensions import ToolRegistry

        registry = ToolRegistry()
        handler = AsyncMock(return_value={})
        registry.register("sample", Tool("unspecified", "Sample", {}, {}, handler))
        self.assertFalse(registry.tools["unspecified"][1].read_only)
        with self.assertLogs("assistant_runtime.app.extensions", level="WARNING"):
            registry.register(
                "sample", Tool("conflicting", "Sample", {}, {}, handler, read_only=True, destructive=True)
            )
        self.assertFalse(registry.tools["conflicting"][1].read_only)
        self.assertTrue(registry.tools["conflicting"][1].destructive)
        handler.assert_not_awaited()

    async def test_mcp_validation_and_unconfirmed_handler_errors_are_distinct(self):
        server = FastMCP("validation")
        server.add_middleware(ToolOutcomes())
        calls = []

        @server.tool
        async def sample(count: int) -> dict:
            calls.append(count)
            raise RuntimeError("private-internal-diagnostic")

        async with Client(server) as client:
            invalid = await client.call_tool("sample", {"count": "wrong"}, raise_on_error=False)
            self.assertEqual(invalid.structured_content["tool_outcome"]["dispatch"], "not_dispatched")
            self.assertEqual(invalid.structured_content["tool_outcome"]["field"], "count")
            self.assertEqual(calls, [])
            unknown = await client.call_tool("sample", {"count": 1}, raise_on_error=False)
            self.assertEqual(unknown.structured_content["tool_outcome"]["outcome"], "unknown")
            self.assertEqual(calls, [1])
            self.assertNotIn("private-internal-diagnostic", str(unknown.structured_content))

    async def test_mcp_preserves_owner_outcome_and_received_receipt(self):
        server = FastMCP("outcomes")
        server.add_middleware(ToolOutcomes())
        calls = []

        @server.tool
        async def sample(mode: str) -> dict:
            if mode == "invalid":
                raise invalid_arguments("reason required", "execution.reason")
            calls.append(mode)
            raise ToolFailure(
                ToolOutcome("invalid_tool_result", "result", "dispatched", "unknown", "Inspect operation"),
                result={"operation_id": "op-1", "state": "submitted"},
            )

        async with Client(server) as client:
            invalid = await client.call_tool("sample", {"mode": "invalid"}, raise_on_error=False)
            self.assertTrue(invalid.is_error)
            self.assertEqual(invalid.structured_content["tool_outcome"]["dispatch"], "not_dispatched")
            self.assertEqual(calls, [])
            result = await client.call_tool("sample", {"mode": "write"}, raise_on_error=False)
            self.assertTrue(result.is_error)
            self.assertEqual(result.structured_content["received_result"]["operation_id"], "op-1")
            self.assertEqual(result.structured_content["tool_outcome"]["outcome"], "unknown")
            self.assertEqual(calls, ["write"])

    async def test_registry_retains_post_dispatch_evidence_without_reexecution(self):
        from assistant_runtime.app.extensions import ToolRegistry
        from assistant_runtime.api import Tool, CallIdentity

        registry = ToolRegistry()
        handler = AsyncMock(return_value={"operation_id": "saved", "state": "submitted"})
        registry.register(
            "example", Tool("submit", "test", {"type": "object"}, {"type": "integer"}, handler, False)
        )
        with self.assertRaises(ToolFailure) as caught:
            await registry.invoke("submit", {}, identity=CallIdentity("request", "work"))
        self.assertEqual(caught.exception.outcome.dispatch, "dispatched")
        self.assertEqual(caught.exception.result["operation_id"], "saved")
        handler.assert_awaited_once()
        with self.assertRaises(ToolFailure) as missing:
            await registry.invoke("absent", {}, identity=CallIdentity("request", "work"))
        self.assertEqual(missing.exception.outcome.dispatch, "not_dispatched")

    async def test_task_mcp_declares_nested_schema_before_handler_dispatch(self):
        from types import SimpleNamespace
        from assistant_runtime.core.tasks.tools import create_server

        handler = SimpleNamespace(invoke=AsyncMock(return_value={"ok": True}))
        async with Client(create_server(handler)) as client:
            tools = {t.name: t for t in await client.list_tools()}
            schema = tools["create_task"].input_schema["properties"]["execution"]
            self.assertIn(
                "reason", schema["anyOf"][0]["properties"] if "anyOf" in schema else schema["properties"]
            )
            result = await client.call_tool(
                "create_task",
                {"request_id": "request", "title": "test", "prompt": "test", "execution": {"invalid": 1}},
                raise_on_error=False,
            )
            self.assertTrue(result.is_error)
            handler.invoke.assert_not_awaited()


class TaskContractTest(unittest.TestCase):
    def test_patch_preserves_fields_clears_optional_and_replaces_lists(self):
        current = {
            "model": "a",
            "reason": "keep",
            "assessment": {"acceptance": "done", "uncertainties": ["old"]},
        }
        patch = {"model": "b", "assessment": {"uncertainties": ["new"]}}
        result = merge_patch(current, patch)
        self.assertEqual(result["reason"], "keep")
        self.assertEqual(result["assessment"], {"acceptance": "done", "uncertainties": ["new"]})
        result = merge_patch(result, {"assessment": {"acceptance": None}})
        self.assertNotIn("acceptance", result["assessment"])
        self.assertIn("acceptance", current["assessment"])
        validate({"execution": patch}, CHANGES, "changes")
        validate(result, EXECUTION, "execution")

    def test_variant_change_does_not_keep_incompatible_trigger_fields(self):
        self.assertEqual(
            merge_patch({"kind": "interval", "seconds": 10}, {"kind": "cron", "expression": "0 * * * *"}),
            {"kind": "cron", "expression": "0 * * * *"},
        )

    def test_invalid_field_has_path_without_argument_value(self):
        with self.assertRaises(ToolFailure) as caught:
            validate({"model": {"secret": "do-not-log"}}, EXECUTION, "execution")
        self.assertEqual(caught.exception.outcome.field, "execution.model")
        self.assertNotIn("do-not-log", str(caught.exception))
