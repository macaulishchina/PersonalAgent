"""Public route constraints, explicit defaults and read-only lost-ack recovery."""

import copy
import json
import sqlite3
import time
import unittest
import uuid

from assistant_runtime.api import CapabilityRequestError
from assistant_runtime.api.tool_calls import ToolFailure
from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.core.models.settings import ModelSettings
from assistant_runtime.core.tasks.selection import ModelPolicy
from assistant_runtime.core.tasks.repository import TaskRepository
import test_models as model_fixtures
import test_tasks as task_fixtures
import test_conversation_sessions as conversation_fixtures


def registry():
    raw = model_fixtures.settings()
    raw["entries"]["test-model"]["reasoning_efforts"] = ["low", "high"]
    raw["entries"]["test-model"]["default_reasoning_effort"] = "auto"
    return ModelSettings.parse(raw)


class PublicRouteTest(unittest.TestCase):
    def test_public_route_excludes_secrets_and_ignores_presentation_credentials_defaults(self):
        settings = registry()
        route = settings.route("test-model")
        settings.providers["test"]["key_env"] = "ROTATED_KEY"
        settings.providers["test"]["presentation"] = {"display_name": "团队", "avatar": "builtin:openai"}
        settings.entries["test-model"]["default_reasoning_effort"] = "high"
        self.assertEqual(route, settings.route("test-model"))
        self.assertNotIn("api.deepseek.com", json.dumps(settings.public_catalog()))
        self.assertNotIn("ROTATED_KEY", json.dumps(settings.public_catalog()))
        condition = {k: route[k] for k in ("model", "revision")}
        settings.require_route(condition)
        settings.providers["test"]["base_url"] = "https://another.example/v1"
        with self.assertRaises(CapabilityRequestError) as caught:
            settings.require_route(condition)
        self.assertEqual(caught.exception.code, "model_binding_changed")
        self.assertEqual(caught.exception.dispatch_state, "not_dispatched")
        self.assertIsInstance(caught.exception, ToolFailure)
        self.assertEqual(caught.exception.outcome.public()["dispatch"], "not_dispatched")

    def test_multiple_protocols_require_a_selected_safe_route(self):
        provider = {
            "base_url": "https://gateway.example/api",
            "paths": {"openai_chat": "", "gemini": "google/v1beta"},
            "key_env": "TEST_KEY",
            "enabled": True,
        }
        raw = {"providers": {"gateway": provider}, "entries": {"m": {"provider": "gateway", "model": "m"}}}
        with self.assertRaisesRegex(ValueError, "支持的接口协议"):
            ModelSettings.parse(raw)
        raw["entries"]["m"]["protocol"] = "gemini"
        settings = ModelSettings.parse(raw)
        self.assertEqual(
            settings.provider_for(settings.entries["m"])["base_url"],
            "https://gateway.example/api/google/v1beta",
        )
        for invalid in ("../another", "//elsewhere.example", "https://elsewhere.example/v1", "%2e%2e"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "relative protocol path"):
                ModelSettings.parse(
                    {**raw, "providers": {"gateway": {**provider, "paths": {"gemini": invalid}}}}
                )

    def test_disabled_and_distinct_connections_are_not_interchangeable(self):
        settings = registry()
        settings.providers["other"] = copy.deepcopy(settings.providers["test"])
        settings.entries["other-model"] = {**settings.entries["test-model"], "provider": "other"}
        self.assertNotEqual(
            settings.route("test-model")["revision"], settings.route("other-model")["revision"]
        )
        route = settings.route("test-model")
        settings.providers["test"]["enabled"] = False
        with self.assertRaises(CapabilityRequestError):
            settings.require_route({k: route[k] for k in ("model", "revision")})


class TaskRouteTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = task_fixtures.TasksTest.asyncSetUp
    asyncTearDown = task_fixtures.TasksTest.asyncTearDown

    async def prepare(self):
        self.plugin.scheduler.pause()
        self.settings = registry()
        self.executor.models.return_value = self.settings.public_catalog()
        self.config.main_model = "test-model"
        self.config.main_reasoning_effort = "auto"
        self.plugin.model_policy = ModelPolicy.parse({})
        route = self.settings.route("test-model")
        return {k: route[k] for k in ("model", "revision")}

    async def create_bound(self, condition, **execution):
        args = {
            "request_id": str(uuid.uuid4()),
            "spec": {
                "title": "route test",
                "prompt": "synthetic",
                "auto_archive": False,
                "execution": {
                    "model": "test-model",
                    "expected_model": condition,
                    "effort_mode": "model_default",
                    **execution,
                },
            },
        }
        result = await self.plugin.request(TaskRequest("create", args))
        return result, args

    async def test_new_default_auto_is_frozen_and_old_auto_rule_is_preserved(self):
        condition = await self.prepare()
        with self.assertRaisesRegex(ValueError, "initiating model"):
            await self.plugin.model_policy.resolve(
                self.executor, {}, self.plugin.settings.execution, self.plugin.model_defaults()
            )
        definition, args = await self.create_bound(condition)
        saved = definition["spec"]["execution"]
        self.assertEqual(saved["reasoning_effort"], "auto")
        self.settings.entries["test-model"]["default_reasoning_effort"] = "high"
        self.executor.models.return_value = self.settings.public_catalog()
        restored = await self.plugin.model_policy.refresh_missing(
            self.executor, saved, self.plugin.settings.execution, self.plugin.model_defaults()
        )
        self.assertEqual(restored["reasoning_effort"], "auto")
        same = await self.plugin.request(TaskRequest("create", args))
        self.assertEqual(same["id"], definition["id"])
        updated = await self.plugin.request(
            TaskRequest(
                "update",
                {
                    "task_id": definition["id"],
                    "revision": definition["revision"],
                    "changes": {"execution": {"reason": "Updated explanation only"}},
                },
            )
        )
        self.assertEqual(updated["spec"]["execution"]["reasoning_effort"], "auto")
        self.assertEqual(updated["spec"]["execution"]["reason"], "Updated explanation only")

    async def test_changed_route_fails_before_launch_and_strict_upgrade_is_rejected(self):
        condition = await self.prepare()
        definition, _ = await self.create_bound(condition)
        run = self.plugin.repo.enqueue(definition, "synthetic")
        self.settings.entries["test-model"]["model"] = "different-upstream"
        self.executor.models.return_value = self.settings.public_catalog()
        await self.plugin._launch(run)
        self.executor.launch.assert_not_awaited()
        current = self.plugin.repo.run(run["id"])
        self.assertEqual(current["state"], "failed")
        self.assertEqual(current["data"]["dispatch"]["state"], "not_dispatched")
        self.assertEqual(current["data"]["initial_error"]["code"], "model_binding_changed")
        with self.assertRaises(CapabilityRequestError):
            await self.plugin.upgrades.request(
                {
                    "run_id": run["id"],
                    "request_id": str(uuid.uuid4()),
                    "recommendation": "missing",
                    "cause": "reasoning",
                    "reason": "synthetic reason",
                    "evidence": "synthetic",
                    "checkpoint": {},
                },
                None,
            )

    async def test_instance_policy_conflict_is_rejected_and_no_default_value_is_guessed(self):
        condition = await self.prepare()
        self.plugin.settings.execution["model"] = "another-model"
        with self.assertRaises(CapabilityRequestError):
            await self.create_bound(condition)
        self.plugin.settings.execution["model"] = "auto"
        with self.assertRaises(CapabilityRequestError):
            await self.create_bound(condition, reasoning_effort="high")
        self.plugin.model_policy.recommendations["other"] = {"model": "another-model"}
        with self.assertRaises(CapabilityRequestError):
            await self.plugin.model_policy.resolve(
                self.executor,
                {"expected_model": condition, "effort_mode": "model_default", "recommendation": "other"},
                self.plugin.settings.execution,
                self.plugin.model_defaults(),
            )

    async def test_read_only_creation_receipt_matches_original_and_survives_repository_reopen(self):
        condition = await self.prepare()
        definition, args = await self.create_bound(condition)
        self.plugin.repo = TaskRepository(self.store.db)
        receipt = await self.plugin.request(TaskRequest("receipt", args))
        self.assertEqual(
            receipt,
            {"found": True, "request_id": args["request_id"], "task_id": definition["id"], "matches": True},
        )
        changed = copy.deepcopy(args)
        changed["spec"]["prompt"] = "different"
        self.assertFalse((await self.plugin.request(TaskRequest("receipt", changed)))["matches"])
        self.assertFalse(
            (await self.plugin.request(TaskRequest("receipt", {"request_id": str(uuid.uuid4())})))["found"]
        )
        self.executor.launch.assert_not_awaited()
        self.assertEqual(len(self.plugin.repo.definitions()), 1)


class QueryBatchTest(unittest.TestCase):
    def test_list_and_run_lookup_use_bounded_queries_for_one_twenty_and_hundred_tasks(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        repo = TaskRepository(db)
        definitions, ids = [], []
        for index in range(100):
            ident = uuid.uuid4().hex
            spec = {
                "title": str(index),
                "prompt": "synthetic",
                "trigger": {"kind": "immediate"},
                "execution": {"kind": "agent"},
            }
            db.execute(
                "INSERT INTO job_definitions(id,request_id,fingerprint,state,spec,created,updated) VALUES(?,?,?,?,?,?,?)",
                (
                    ident,
                    str(uuid.uuid4()),
                    "fingerprint",
                    "enabled",
                    json.dumps(spec),
                    time.time(),
                    time.time(),
                ),
            )
            definition = repo.definition(ident)
            definitions.append(definition)
            ids.append(repo.enqueue(definition, "one")["id"])
        for count in (1, 20, 100):
            queries = []
            db.set_trace_callback(queries.append)
            result = repo.list_runs(definitions[:count])
            runs = repo.runs_by_ids(ids[:count])
            db.set_trace_callback(None)
            self.assertEqual(len(result), count)
            self.assertEqual(len(runs), count)
            self.assertEqual(len(queries), 2)


class ConversationRouteTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = conversation_fixtures.SessionsTest.asyncSetUp
    asyncTearDown = conversation_fixtures.SessionsTest.asyncTearDown
    call = conversation_fixtures.SessionsTest.call

    async def test_queued_expected_route_is_fingerprinted_and_lost_ack_is_not_reresolved(self):
        chat = self.registry.main
        settings = registry()
        self.host.config.main_model = "test-model"
        chat.backend.client.settings = settings
        # Hold the drain at a genuine prior-turn boundary.
        chat.backend.active_turn = "prior-turn"
        route = settings.route("test-model")
        condition = {k: route[k] for k in ("model", "revision")}
        ident = str(uuid.uuid4())
        args = {
            "session_id": chat.session,
            "request_id": ident,
            "text": "queued synthetic",
            "expected_model": condition,
        }
        first = await self.call("submit", **args)
        settings.entries["test-model"]["model"] = "changed-upstream"
        repeated = await self.call("submit", **args)
        self.assertEqual(repeated["message"]["id"], first["message"]["id"])
        with self.assertRaises(ValueError):
            await self.call(
                "submit", **{**args, "expected_model": {"model": "test-model", "revision": "0" * 64}}
            )
        with self.assertRaises(CapabilityRequestError):
            await self.call("submit", **{**args, "request_id": str(uuid.uuid4())})
        receipt = await self.call(
            "receipt", session_id=chat.session, request_id=ident, include_execution=True
        )
        self.assertEqual(receipt["execution"]["requested"], condition)
        await chat.cancel_request(ident)
        receipt = await self.call(
            "receipt", session_id=chat.session, request_id=ident, include_execution=True
        )
        self.assertEqual(receipt["execution"]["dispatch_state"], "not_dispatched")
        self.assertEqual(chat.backend.calls, [])
