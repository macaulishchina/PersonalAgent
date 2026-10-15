"""Project settings must override global and resumed-thread model defaults."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from assistant_runtime.config import Config
from assistant_runtime.plugins.codex.backend import CodexBackend
from assistant_runtime.store import Store


class ProjectModelConfigTest(unittest.IsolatedAsyncioTestCase):
    def test_load_and_validate_project_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            path.write_text(
                'profile="headless"\ncodex_model="chosen-model"\n'
                'codex_reasoning_effort="low"\nrealtime_voice="cove"\n'
                'memory_model="memory-model"\nmemory_reasoning_effort="medium"\n'
            )
            config = Config.load(path)
            self.assertEqual(config.codex_model, "chosen-model")
            self.assertEqual(config.codex_reasoning_effort, "low")
            self.assertEqual(config.realtime_voice, "cove")
            self.assertEqual(config.memory_model, "memory-model")
            self.assertEqual(config.memory_reasoning_effort, "medium")
        for key in (
            "codex_model",
            "codex_reasoning_effort",
            "realtime_voice",
            "memory_model",
            "memory_reasoning_effort",
        ):
            for value in ("", " ", None, 1):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    Config(**{key: value})

    async def test_new_and_resumed_threads_and_turns_use_project_settings(self):
        for saved in (None, "existing-thread"):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as folder:
                store = Store(Path(folder) / "state.db")
                if saved:
                    store.set("thread_id", saved)
                config = Config(root=Path(folder), codex_model="chosen-model", codex_reasoning_effort="low")
                backend = CodexBackend(config, store, AsyncMock(), AsyncMock())
                backend.client.start = AsyncMock()
                backend._events = AsyncMock()
                backend.client.request = AsyncMock(
                    side_effect=[
                        {
                            "thread": {"id": saved or "new-thread", "turns": []},
                            "model": "resolved-model",
                            "reasoningEffort": "low",
                        },
                        {},
                        {"turn": {"id": "turn-1"}},
                    ]
                )
                await backend.start()
                await backend.events_task
                call = backend.client.request.await_args_list[0]
                self.assertEqual(call.args[0], "thread/resume" if saved else "thread/start")
                self.assertEqual(call.args[1]["model"], "chosen-model")
                self.assertEqual(call.args[1]["config"]["model_reasoning_effort"], "low")
                self.assertEqual(backend.resolved_model, "resolved-model")
                self.assertEqual(backend.resolved_reasoning_effort, "low")
                await backend.submit("test task")
                params = backend.client.request.await_args_list[2].args[1]
                self.assertEqual((params["model"], params["effort"]), ("chosen-model", "low"))
                self.assertEqual(params["threadId"], saved or "new-thread")
                store.db.close()

    async def test_memory_maintenance_uses_independent_settings(self):
        config = Config(
            codex_model="task-model",
            codex_reasoning_effort="high",
            memory_model="memory-model",
            memory_reasoning_effort="low",
        )
        backend = CodexBackend(config, None, AsyncMock(), AsyncMock())

        async def request(method, params, **kwargs):
            if method == "account/read":
                return {"account": {"type": "chatgpt"}}
            if method == "thread/start":
                self.assertEqual(params["model"], "memory-model")
                self.assertEqual(params["config"]["model_reasoning_effort"], "low")
                self.assertEqual(params["sandbox"], "read-only")
                return {
                    "thread": {"id": "maintenance"},
                    "model": "resolved-memory-model",
                    "reasoningEffort": "low",
                }
            if method == "turn/start":
                self.assertEqual((params["model"], params["effort"]), ("memory-model", "low"))
                backend.maintenance_result.set_result('{"facts": []}')
                return {"turn": {"id": "maintenance-turn"}}
            return {}

        backend.client.request = AsyncMock(side_effect=request)
        self.assertEqual(await backend.memory_complete([], {"type": "object"}), {"facts": []})
        self.assertEqual(backend.resolved_memory_model, "resolved-memory-model")
        self.assertEqual(backend.resolved_memory_reasoning_effort, "low")
        self.assertIsNone(backend.resolved_model)

    def test_memory_defaults_do_not_follow_task_settings(self):
        config = Config(codex_model="task-only-model", codex_reasoning_effort="ultra")
        self.assertEqual((config.memory_model, config.memory_reasoning_effort), ("gpt-6-luna", "low"))
