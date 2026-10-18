"""Isolated real task/model runtime for explicit synthetic API acceptance.

Uses production plugins and IPC, with inert voice/device boundaries. Never loads
production state, registry entries, devices or starts a physical audio stream.
"""

import asyncio
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from assistant_runtime.assistant.service import AssistantService
from assistant_runtime.conversation.store import ConversationStore
from assistant_runtime.conversation.media import MediaAttachments
from assistant_runtime.kernel.runtime import Manifest, Runtime
from assistant_runtime.plugins.codex.api_jobs import RoutedJobs
from assistant_runtime.plugins.codex.integrations import CodexIntegrations
from assistant_runtime.plugins.codex.rpc import CodexClient
from assistant_runtime.plugins.integrations.registry import McpRegistry
from assistant_runtime.plugins.models.plugin import ModelsPlugin
from assistant_runtime.plugins.models.gateway import gateway_credential
from assistant_runtime.plugins.models.settings import ModelSettings
from assistant_runtime.plugins.notifications.plugin import NotificationsPlugin
from assistant_runtime.plugins.tasks.plugin import TasksPlugin
from assistant_runtime.store import Store
from assistant_runtime.workfiles import WorkFiles
from scripts.prepare_model_gateway import private_write


class Boundary:
    def __init__(self, name):
        self.manifest = Manifest(name)

    async def start(self, context):
        pass


class AcceptanceHost:
    def __init__(self, source, root, *, search=False):
        self.source = source
        self.search = search
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Native project/Skill discovery must terminate at this synthetic repo.
        if not (self.root / ".git").exists():
            subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        workspace = self.root / "workspace"
        workspace.mkdir(exist_ok=True)
        self.config = replace(
            source,
            root=self.root,
            workspace=str(workspace),
            plugins=["codex", "models", "tasks", "notifications"],
            notifications={"channels": ["chat"], "voice_enabled": False},
            external_mcp={"inherit_user_servers": False},
            tasks={"concurrency": 4},
        )
        # Native process flags isolate account apps from synthetic acceptance. This
        # wrapper starts the real binary; it does not replace any execution logic.
        binary = shutil.which(source.codex_binary)
        if not binary:
            raise ValueError("Codex binary not found")
        wrapper = self.root / "codex-acceptance"
        flags = ["-c", "features.apps=" + str(search).lower(), "-c", "features.multi_agent=false"]
        if search:
            flags += [
                "-c",
                "apps._default.enabled=false",
                "-c",
                "apps.connector_openai_search_service.enabled=true",
            ]
        private_write(
            wrapper,
            f"#!{sys.executable}\nimport os, sys\nos.execv({binary!r}, [{binary!r}, *sys.argv[1:-3], *{flags!r}, *sys.argv[-3:]])\n",
        )
        wrapper.chmod(0o700)
        self.config.codex_binary = str(wrapper)
        self.config.state_dir.mkdir(mode=0o700, exist_ok=True)
        folder = self.config.state_dir / "model-api"
        folder.mkdir(mode=0o700, exist_ok=True)
        private_write(
            folder / "gateway.key", gateway_credential(source.root, ModelSettings.parse(source.model_api))
        )
        self.store = Store(self.config.state_dir / "acceptance.sqlite3")
        chat = ConversationStore(self.store)
        self.conversation = SimpleNamespace(
            session="synthetic-main", store=chat, media=MediaAttachments(self.config.state_dir, self.store.db)
        )
        self.workfiles = WorkFiles(self.config.state_dir, self.store.db)
        self.mcp_registry = McpRegistry(self.root, self.config.external_mcp)
        self.runtime = Runtime(self.store)
        self.client = CodexClient(
            self.config.codex_binary,
            self.config.workspace,
            overrides={
                "features.remote_control": False,
                "features.multi_agent": False,
                "project_doc_max_bytes": 0,
                "web_search": "disabled",
            },
        )
        self.backend = SimpleNamespace(
            config=self.config,
            client=self.client,
            thread_id=None,
            integrations=None,
            server_tasks=set(),
            busy=False,
        )
        self.backend.integrations = CodexIntegrations(self.backend, self.mcp_registry)
        self.api_events = []
        self.task_executor = self.executor
        self.tasks = TasksPlugin(self)
        self.runtime.add(Boundary("codex"))
        self.runtime.add(Boundary("conversation"))
        self.runtime.add(NotificationsPlugin(self))
        self.runtime.add(self.tasks)
        self.runtime.add(ModelsPlugin(self))
        self.foreground_events = asyncio.Queue()
        self.pump = self.server = None
        self.foreground_threads = []

    def executor(self, notify):
        routed = RoutedJobs(self, notify)
        start = routed.api_jobs

        async def observed():
            api = await start()
            if not getattr(api, "acceptance_observed", False):
                receive = api.client.events.get

                async def get():
                    event = await receive()
                    self.api_events.append(event)
                    return event

                api.client.events.get = get
                api.acceptance_observed = True
            return api

        routed.api_jobs = observed
        return routed

    async def start(self):
        await self.client.start()
        await self.backend.integrations.start()
        self.config.socket.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(
            lambda reader, writer: AssistantService._connection(self, reader, writer),
            path=self.config.socket,
            limit=128 * 1024,
        )
        self.config.socket.chmod(0o600)
        await self.runtime.start()
        self.pump = asyncio.create_task(self.events())
        return self

    async def events(self):
        while True:
            event = await self.client.events.get()
            if not await self.tasks.executor.event(event):
                await self.foreground_events.put(event)

    async def foreground(self, prompt, model="gpt-6-luna"):
        """Subscription thread with only this fixture's production tasks/models MCP."""
        config = await self.backend.integrations.capture("foreground", cwd=self.config.workspace)
        for name, descriptor in config.items():
            if name not in {"tasks", "models"}:
                descriptor["enabled"] = False
        reply = await self.client.request(
            "thread/start",
            {
                "model": model,
                "cwd": self.config.workspace,
                "sandbox": self.config.codex_sandbox,
                "approvalPolicy": self.config.approval_policy,
                "baseInstructions": "Synthetic coding integration acceptance. Use only the configured tasks/models MCP. Never use shell, network, personal files, devices or other tools. Do not spawn agents. Stop on any error.",
                "config": {"mcp_servers": config, "web_search": "disabled", "skills": {"config": []}},
            },
        )
        thread = reply["thread"]["id"]
        self.foreground_threads.append(thread)
        turn = await self.client.request(
            "turn/start",
            {
                "threadId": thread,
                "effort": "low",
                "input": [{"type": "text", "text": prompt}],
            },
        )
        events = []
        async with asyncio.timeout(180):
            while True:
                event = await self.foreground_events.get()
                if event.get("params", {}).get("threadId") != thread:
                    continue
                events.append(event)
                if "id" in event:
                    await self.client.send(
                        {
                            "id": event["id"],
                            "error": {"code": -32601, "message": "No additional test permissions"},
                        }
                    )
                if event.get("method") == "error":
                    raise RuntimeError("Synthetic foreground provider error")
                if (
                    event.get("method") == "turn/completed"
                    and event["params"]["turn"]["id"] == turn["turn"]["id"]
                ):
                    if event["params"]["turn"]["status"] != "completed":
                        raise RuntimeError("Synthetic foreground did not complete")
                    return {"thread_id": thread, "turn_id": turn["turn"]["id"], "events": events}

    async def restart(self):
        source, root, observed, search = self.source, self.root, self.api_events, self.search
        await self.close()
        self.__init__(source, root, search=search)
        self.api_events.extend(observed)
        await self.start()

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        # Stop explicit test work before shutting down its processes.
        for run in self.tasks.repo.pending():
            if run["state"] in {"running", "waiting_user", "dispatching"}:
                try:
                    await self.tasks.executor.stop(run)
                except Exception:
                    pass
        for thread in self.foreground_threads:
            try:
                summary = (
                    await self.client.request("thread/read", {"threadId": thread, "includeTurns": True})
                )["thread"]
                for turn in summary.get("turns", []):
                    if turn.get("status") == "inProgress":
                        await self.client.request(
                            "turn/interrupt", {"threadId": thread, "turnId": turn["id"]}
                        )
                await self.client.request("thread/archive", {"threadId": thread})
            except Exception:
                pass
        await self.runtime.close()
        if self.pump:
            self.pump.cancel()
            await asyncio.gather(self.pump, return_exceptions=True)
        await self.client.close()
        self.config.socket.unlink(missing_ok=True)
        self.store.db.close()
