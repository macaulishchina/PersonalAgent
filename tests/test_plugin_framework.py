"""Public plugin boundaries and lifecycle behavior, independent of model services."""

from platform_support import private_mode

import asyncio
from pathlib import Path
from assistant_runtime.api.system import system
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from assistant_runtime.api import CallIdentity, Manifest, PluginDefinition, Tool
from assistant_runtime.app.extensions import InstalledPlugin, ToolRegistry, discover
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.app.store import Store


class PluginFrameworkTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_dependency_blocks_dependents_and_cleans_partial_handlers(self):
        called = []

        class Broken:
            manifest = Manifest("broken")

            async def start(self, context):
                context.register(str, lambda r: None)
                raise RuntimeError("offline")

        class Dependent:
            manifest = Manifest("dependent", ("broken",))

            async def start(self, context):
                called.append(True)

        runtime = Runtime(None)
        runtime.add(Broken())
        runtime.add(Dependent())
        await runtime.start()
        self.assertFalse(called)
        self.assertEqual(runtime.health()["dependent"]["state"], "blocked")
        self.assertNotIn(str, runtime.handlers)
        await runtime.close()

    async def test_optional_missing_and_major_version_contract(self):
        class Optional:
            manifest = Manifest("optional", optional=("missing",))

            async def start(self, context):
                self.unavailable = context.optional_unavailable

        runtime = Runtime(None)
        plugin = Optional()
        runtime.add(plugin)
        await runtime.start()
        self.assertEqual(plugin.unavailable, ("missing",))
        await runtime.close()
        runtime = Runtime(None)
        runtime.add(SimpleNamespace(manifest=Manifest("one", version="2.0.0")))
        runtime.add(SimpleNamespace(manifest=Manifest("two", ("one",), dependency_versions=(("one", "1"),))))
        with self.assertRaisesRegex(ValueError, "Incompatible dependency"):
            runtime.order()

    async def test_installed_extension_context_tools_state_and_cleanup(self):
        class Extension:
            def __init__(self, ctx):
                self.ctx = ctx

            async def start(self):
                self.queue = self.ctx.subscribe()

            async def write(self, args):
                self.ctx.set("value", args["value"])
                self.ctx.publish("sample.changed", value=args["value"])
                return {"value": args["value"], "request": self.ctx.identity.request_id}

            def tools(self):
                return (
                    Tool(
                        "sample_write",
                        "Save a sample value",
                        {
                            "type": "object",
                            "properties": {"value": {"type": "integer"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                        {"type": "object"},
                        self.write,
                        read_only=False,
                        idempotent=True,
                    ),
                )

        definition = PluginDefinition(Manifest("sample"), Extension, lambda c: c)
        tools = ToolRegistry()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / "state.db")
            runtime = Runtime(store)
            adapter = InstalledPlugin(definition, {}, root, lambda k: None, (), tools)
            runtime.add(adapter)
            await runtime.start()
            ctx = adapter.instance.ctx
            self.assertFalse(hasattr(ctx, "runtime"))
            self.assertFalse(hasattr(ctx, "host"))
            result = await tools.invoke("sample_write", {"value": 8}, identity=CallIdentity("r", "w"))
            self.assertEqual(result, {"value": 8, "request": "r"})
            from fastmcp import Client
            from assistant_runtime.mcp.extensions import create_server

            server = create_server(tools, lambda: CallIdentity("mcp-r", "w"))
            async with Client(server) as client:
                listed = await client.list_tools()
                self.assertEqual(listed[0].input_schema, dict(adapter.instance.tools()[0].input_schema))
                mcp_result = await client.call_tool("sample_write", {"value": 9})
                self.assertEqual(mcp_result.data, {"value": 9, "request": "mcp-r"})
            self.assertEqual(store.get("plugin.sample.value"), 9)
            self.assertEqual((await adapter.instance.queue.get()).kind, "sample.changed")
            from jsonschema import ValidationError

            with self.assertRaises(ValidationError):
                await tools.invoke("sample_write", {"value": "wrong"}, identity=CallIdentity("r", "w"))
            await runtime.close()
            self.assertFalse(tools.tools)
            self.assertFalse(runtime.subscribers)
            self.assertTrue(ctx.cancelled.is_set())
            store.db.close()

    async def test_entrypoints_are_explicit_and_duplicates_rejected(self):
        loaded = []

        def load():
            loaded.append(True)
            return PluginDefinition(Manifest("sample"), lambda c: None, lambda c: c)

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        metadata = Path(temporary.name) / "platform.json"
        metadata.write_text('{"supported_platforms": ["linux", "windows"]}')
        entry = SimpleNamespace(
            name="sample",
            value="sample:definition",
            load=load,
            dist=SimpleNamespace(files=["sample/platform.json"], locate_file=lambda name: metadata),
        )
        with patch("assistant_runtime.app.extensions.entry_points", return_value=[entry]):
            self.assertEqual(discover([]), {})
            self.assertFalse(loaded)
            self.assertEqual(discover(["sample"])["sample"].manifest.name, "sample")
        with patch("assistant_runtime.app.extensions.entry_points", return_value=[entry, entry]):
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                discover(["sample"])

    async def test_minimal_host_needs_no_audio_or_model_credentials(self):
        import os
        import sys

        source = """
import asyncio, tempfile, sys
from pathlib import Path
from assistant_runtime.app.config import Config
from assistant_runtime.app.host import AssistantService
async def main():
    with tempfile.TemporaryDirectory() as folder:
        host = AssistantService(Config(root=Path(folder), web={"enable": False}))
        try:
            await host.runtime.start()
            await asyncio.sleep(.05)
            assert all(s["state"] == "ready" for s in host.runtime.health().values()), host.runtime.health()
            assert not any(m in sys.modules for m in ("numpy", "miniaudio", "sherpa_onnx", "fastembed"))
            if sys.platform == "win32":
                assert host.assistant is None
                assert "web" not in host.runtime.plugins
            else:
                assert host.conversation.state()["configuration_required"]
            try:
                await host.backend.submit("no request should be sent")
            except RuntimeError as error:
                expected = "configuration required"
                assert expected in str(error), error
            else:
                raise AssertionError("Unconfigured inference accepted")
        finally:
            await host.runtime.close()
            host.store.db.close()
            host.lock_file.close()
asyncio.run(main())
"""
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            source,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), 20)
        self.assertEqual(proc.returncode, 0, (out + err).decode())

    async def test_real_sample_wheel_entrypoint_and_packaged_skill(self):
        import shutil
        import sys
        import zipfile

        source = Path(__file__).resolve().parents[1] / "examples/sample-plugin"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copytree(source, root / "source", ignore=shutil.ignore_patterns("build", "*.egg-info"))
            # Certify this isolated synthetic fixture, not the shipped Linux-only example.
            (root / "source/src/personal_agent_sample/platform.json").write_text(
                '{"supported_platforms": ["linux", "windows"]}'
            )
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "from setuptools.build_meta import build_wheel; build_wheel('../dist')",
                cwd=root / "source",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), 30)
            self.assertEqual(proc.returncode, 0, (out + err).decode())
            target = root / "installed"
            with zipfile.ZipFile(next((root / "dist").glob("*.whl"))) as wheel:
                wheel.extractall(target)
            # Metadata discovery from an actual installed wheel, not mocked entrypoints.
            previous = list(sys.path)
            sys.path.insert(0, str(target))
            try:
                from assistant_runtime.core.configuration.catalog import installed_descriptions

                with patch(
                    "importlib.metadata.EntryPoint.load", side_effect=AssertionError("runtime import")
                ):
                    described = next(item for item in installed_descriptions() if item["id"] == "sample")
                self.assertEqual(
                    described["configuration"]["schema"]["properties"]["max_length"]["default"], 2000
                )
                declaration = discover(["sample"])["sample"]
                self.assertTrue((declaration.skills[0] / "SKILL.md").is_file())
                store = Store(root / "state.db")
                runtime = Runtime(store)
                tools = ToolRegistry()
                for dependency in declaration.manifest.requires:

                    async def start(context):
                        pass

                    runtime.add(SimpleNamespace(manifest=Manifest(dependency), start=start))
                runtime.add(InstalledPlugin(declaration, {}, root, lambda k: None, (), tools))
                try:
                    await runtime.start()
                    identity = CallIdentity("package", "work")
                    await tools.invoke("sample_write", {"text": "persisted"}, identity=identity)
                    self.assertEqual(
                        await tools.invoke("sample_read", {}, identity=identity), {"text": "persisted"}
                    )
                finally:
                    await runtime.close()
                    store.db.close()
            finally:
                sys.path[:] = previous
                sys.modules.pop("personal_agent_sample", None)

    async def test_kernel_and_public_api_import_boundaries(self):
        import ast
        import sys

        root = Path(__file__).resolve().parents[1] / "src/assistant_runtime"
        for layer in ("kernel", "api"):
            for path in (root / layer).glob("*.py"):
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    names = (
                        [n.name for n in node.names]
                        if isinstance(node, ast.Import)
                        else (
                            [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level else []
                        )
                    )
                    for name in names:
                        if name.startswith("assistant_runtime."):
                            self.assertIn(
                                name.split(".")[1],
                                {"kernel", "api"} if layer == "api" else {"kernel"},
                                str(path),
                            )
                        elif name:
                            self.assertIn(name.split(".")[0], sys.stdlib_module_names, str(path))

    async def test_initializer_private_files_and_no_overwrite(self):
        from assistant_runtime.app.initialize import initialize
        from assistant_runtime.app.config import Config

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "instance"
            config = Config.load(initialize(root))
            self.assertEqual(config.root, root)
            self.assertFalse(config.model_api["enabled"])
            self.assertNotIn("pipewire", config.plugins)
            self.assertFalse(config.memory_enabled)
            if sys.platform == "win32":
                self.assertNotIn("web", config.plugins)
            else:
                self.assertTrue(config.web["enable"])
            self.assertEqual(private_mode(root / ".env"), 0o600)
            with self.assertRaisesRegex(ValueError, "already exists"):
                initialize(root)

    async def test_configured_camera_uses_declared_state_path_through_composition(self):
        from assistant_runtime.app.config import Config
        from assistant_runtime.app.host import AssistantService

        with tempfile.TemporaryDirectory() as folder:
            config = Config(
                root=Path(folder),
                plugins=["devices", "go2rtc"],
                devices={
                    "camera": {
                        "adapter": "go2rtc",
                        "kind": "camera",
                        "label": "Synthetic camera",
                        "location": "test",
                        "connection": {"base_url": "http://127.0.0.1:8553", "stream": "camera"},
                    }
                },
            )
            host = AssistantService(config)
            try:
                plugin = host.runtime.plugins["go2rtc"]
                self.assertEqual(
                    plugin.providers["camera"].recording_folder, config.state_dir / "video-recordings"
                )
                self.assertFalse(hasattr(plugin.public.config, "root"))
                self.assertFalse(hasattr(plugin.public.config, "model_api"))
            finally:
                host.store.db.close()
                host.lock_file.close()

    async def test_composition_failure_closes_database_and_instance_lock(self):
        import sqlite3
        from assistant_runtime.app.config import Config
        from assistant_runtime.app.host import AssistantService

        with tempfile.TemporaryDirectory() as folder:
            config = Config(root=Path(folder), web={"enable": False})
            store = Store(config.state_dir / "assistant.sqlite3")
            with (
                patch("assistant_runtime.app.host.Store", return_value=store),
                patch("assistant_runtime.app.bootstrap.compose", side_effect=ValueError("bad plugin")),
                self.assertRaisesRegex(ValueError, "bad plugin"),
            ):
                AssistantService(config)
            with self.assertRaises(sqlite3.ProgrammingError):
                store.db.execute("SELECT 1")
            with (config.state_dir / "service.lock").open("w") as lock:
                system.lock(lock, blocking=False)

    async def test_all_bundled_extensions_use_public_boundaries(self):
        import ast

        root = Path(__file__).resolve().parents[1] / "src/assistant_runtime/plugins"
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    if (
                        isinstance(node.value, ast.Attribute)
                        and isinstance(node.value.value, ast.Name)
                        and node.value.value.id == "self"
                    ):
                        self.assertNotIn(
                            (node.value.attr, node.attr),
                            {
                                ("context", "runtime"),
                                ("host", "config"),
                                ("host", "runtime"),
                                ("host", "store"),
                            },
                            str(path),
                        )
                if isinstance(node, ast.ImportFrom) and node.module:
                    self.assertFalse(
                        node.module.startswith(
                            ("assistant_runtime.app", "assistant_runtime.core", "assistant_runtime.kernel")
                        ),
                        str(path),
                    )
        from assistant_runtime.core.plugin_catalog import OWNERS, declaration

        for name in OWNERS:
            spec = declaration(name)
            self.assertEqual(spec.manifest.name, name)
            if OWNERS[name].startswith("assistant_runtime.plugins"):
                self.assertIsNone(spec.internal_factory)

    async def test_native_accounts_resolve_configured_local_pi_without_global_npm(self):
        import json
        from assistant_runtime.core.pi.accounts import installation

        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / "runtime/node_modules/@earendil-works/pi-coding-agent"
            (package / "dist").mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@earendil-works/pi-coding-agent",
                        "version": "1.4.0",
                        "bin": {"pi": "dist/cli.js"},
                    }
                )
            )
            script = package / "dist/cli.js"
            script.touch(mode=0o700)
            if sys.platform == "win32":
                binary = package.parent.parent / ".bin/pi.cmd"
                binary.parent.mkdir()
                binary.touch()
            else:
                binary = Path(folder) / "pi"
                binary.symlink_to(script)
            with patch(
                "assistant_runtime.core.pi.accounts.shutil.which",
                side_effect=lambda command: str(binary)
                if command == str(binary)
                else "node.exe"
                if command == "node"
                else None,
            ):
                self.assertEqual(await installation(str(binary)), package)
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@earendil-works/pi-coding-agent",
                        "version": "1.5.0",
                        "bin": {"pi": "dist/cli.js"},
                    }
                )
            )
            with self.assertRaisesRegex(RuntimeError, "accepted Pi version"):
                await installation(str(binary))

    async def test_installed_capabilities_have_dependencies_and_bound_caller(self):
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Query:
            value: str
            caller_run: str | None = None

        class Provider:
            def __init__(self, context):
                self.context = context

            async def start(self):
                self.context.register(Query, self.handle)

            async def handle(self, request):
                return {"value": request.value, "caller": request.caller_run}

        class Consumer:
            def __init__(self, context):
                self.context = context

            async def start(self):
                pass

            async def ask(self, arguments):
                return await self.context.invoke(Query("safe", "forged"))

            def tools(self):
                return (
                    Tool(
                        "ask_provider",
                        "Read the declared provider",
                        {"type": "object"},
                        {"type": "object"},
                        self.ask,
                        read_only=True,
                    ),
                )

        provider = PluginDefinition(Manifest("provider"), Provider, lambda c: c, provides=(Query,))
        consumer = PluginDefinition(Manifest("consumer", ("provider",)), Consumer, lambda c: c, uses=(Query,))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, tools = Store(root / "db"), ToolRegistry()
            runtime = Runtime(store)
            runtime.add(InstalledPlugin(consumer, {}, root, lambda k: None, consumer.uses, tools))
            runtime.add(InstalledPlugin(provider, {}, root, lambda k: None, (), tools))
            try:
                await runtime.start()
                result = await tools.invoke("ask_provider", {}, identity=CallIdentity("r", "w", "bound-run"))
                self.assertEqual(result, {"value": "safe", "caller": "bound-run"})
            finally:
                await runtime.close()
                store.db.close()
            self.assertNotIn(Query, runtime.handlers)

    async def test_closing_owner_cancels_admitted_calls_before_resource_cleanup(self):
        started, canceled, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Owner:
            manifest = Manifest("owner")

            async def start(self, context):
                context.register(str, self.handle)

                async def close():
                    self_test.assertTrue(canceled.is_set())
                    cleaned.set()

                context.own(close)

            async def handle(self, value):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    canceled.set()

        self_test = self
        runtime = Runtime(None)
        runtime.add(Owner())
        await runtime.start()
        pending = asyncio.create_task(runtime.invoke("synthetic"))
        await started.wait()
        await runtime.close()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertTrue(cleaned.is_set())
        self.assertFalse(runtime.handlers)
