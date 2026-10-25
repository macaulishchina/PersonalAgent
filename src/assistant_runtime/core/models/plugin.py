"""Optional inference owner with durable receipts, bounded admission and caller checks."""

from assistant_runtime.api.lifecycle import admits_work

from .declaration import DECLARATION

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
import uuid

from assistant_runtime.api.models import ModelRequest, ModelCallError
from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.core.models.settings import ModelSettings, reasoning_effort
from assistant_runtime.core.models.gateway import gateway_credential
from assistant_runtime.core.models.client import GatewayClient

log = logging.getLogger(__name__)


class ModelsPlugin:
    manifest = DECLARATION.manifest

    async def configuration_action(self, action, arguments):
        if action != "connection" or arguments:
            raise ValueError("Unknown model configuration action")
        if not self.client:
            return {"reachable": False, "message": "请先配置并启用模型服务"}
        result = await self.client.health()
        return {
            "reachable": result["reachable"],
            "message": "模型网关连通" if result["reachable"] else "模型网关不可用",
        }

    def __init__(self, host, client=None):
        self.host = host
        self.settings = ModelSettings.parse(host.config.model_api)
        self.db = host.store.db
        self.client, self.context, self.error = client, None, None
        self.semaphore = asyncio.Semaphore(self.settings.concurrency)
        self.pending = 0
        self.active = set()
        self.stopping = False
        self.session_namespace = str(uuid.uuid4())
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS model_calls (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, caller TEXT NOT NULL, model TEXT NOT NULL, state TEXT NOT NULL, result TEXT, created REAL NOT NULL, config_revision TEXT NOT NULL)"
        )
        with self.db:
            self.db.execute("UPDATE model_calls SET state='unknown' WHERE state='submitted'")

    async def start(self, context):
        self.context = context
        context.register(ModelRequest, self.request, timeout=self.settings.timeout_seconds + 30)
        if not self.settings.enabled:
            return
        try:
            if self.client is None:
                from assistant_runtime.core.pi.accounts import NativeAccounts

                self.client = GatewayClient(
                    self.settings,
                    gateway_credential(self.host.config.root, self.settings),
                    accounts=NativeAccounts(
                        self.host.config.root,
                        self.settings,
                        state_dir=getattr(self.host.config, "state_dir", self.host.config.root / "state")
                        / "model-accounts",
                        binary=getattr(self.host.config, "pi_binary", "pi"),
                    ),
                )
            context.own(self.client.close)
            await self.client.start()
            context.own(self.close)
        except Exception:
            self.error = "gateway_initialization_failed"

    async def maintenance_cancel(self):
        for task in list(self.active):
            task.cancel()

    async def prepare_configuration(self, config):
        from assistant_runtime.api.configuration import ConfigurationUpdate, ConfigurationPending
        from assistant_runtime.core.pi.accounts import NativeAccounts

        if self.pending or self.active:
            raise ConfigurationPending()
        settings = ModelSettings.parse(config.model_api)
        previous = self.settings, self.client, self.semaphore, self.error
        client = None
        if settings.enabled:
            client = GatewayClient(
                settings,
                gateway_credential(config.root, settings),
                accounts=NativeAccounts(config.root, settings, binary=config.pi_binary),
            )
            try:
                await client.start()
            except BaseException:
                await client.close()
                raise

        async def commit():
            if self.pending or self.active:
                raise ConfigurationPending()
            self.settings, self.client = settings, client
            self.semaphore = asyncio.Semaphore(settings.concurrency)
            self.error = None
            if client and self.context:
                self.context.own(client.close)
            if self.context:
                self.context.runtime.timeouts[ModelRequest] = settings.timeout_seconds + 30

        async def rollback():
            self.settings, self.client, self.semaphore, self.error = previous
            if self.context:
                self.context.runtime.timeouts[ModelRequest] = previous[0].timeout_seconds + 30

        async def release(committed):
            obsolete = previous[1] if committed else client
            if obsolete:
                await obsolete.close()

        return ConfigurationUpdate(commit, rollback, release)

    def maintenance_pending(self):
        return [{"state": "model_call"} for task in self.active if not task.done()]

    async def close(self):
        self.stopping = True
        tasks = list(self.active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def receipt(self, key, fingerprint, ident):
        row = self.db.execute(
            "SELECT fingerprint,state,result FROM model_calls WHERE id=?", (key,)
        ).fetchone()
        if row:
            if row[0] != fingerprint:
                raise ValueError("Request ID reused with different content")
            return {**(json.loads(row[2]) if row[2] else {}), "state": row[1], "request_id": ident}
        return None

    def diagnostics(self):
        return {
            "enabled": self.settings.enabled,
            "error": self.error,
            "pending": self.pending,
            "configuration_revision": self.settings.fingerprint(),
        }

    async def caller(self, run):
        if run:
            info = await self.host.runtime.invoke(TaskRequest("execution_info", {"run_ids": [run]}))
            if info.get(run, {}).get("state") not in {"running", "waiting_user"}:
                raise ValueError("Model caller is no longer active")
            version = info[run].get("execution_version", {})
            return run + ":" + hashlib.sha256(json.dumps(version, sort_keys=True).encode()).hexdigest()[:16]
        return "foreground"

    async def images(self, ids, caller_run):
        if (
            not isinstance(ids, list)
            or len(ids) > 4
            or any(not isinstance(i, str) or not re.fullmatch(r"[a-f0-9]{32}", i) for i in ids)
        ):
            raise ValueError("Images must be registered attachment IDs")
        if caller_run and ids:
            tasks = self.host.runtime.plugins.get("tasks")
            run = tasks.repo.run(caller_run) if tasks else None
            allowed = {r["id"] for r in (run or {}).get("data", {}).get("attachments", [])}
            if not set(ids) <= allowed:
                raise ValueError("Image does not belong to this task execution")
        if not ids:
            return []
        if not getattr(self.host, "workfiles", None):
            raise ValueError("Registered images require the workfiles capability")
        media = self.host.workfiles.media
        references = media.resolve([{"id": i} for i in ids])
        if len(references) != len(ids):
            raise ValueError("Registered image is unavailable")
        content = []
        for r in references:
            if not r["type"].startswith("image/"):
                raise ValueError("Only image attachments are accepted")

            def read():
                with (media.folder / r["id"]).open("rb") as source:
                    return source.read(10 * 1024 * 1024 + 1)

            data = await asyncio.to_thread(read)
            if len(data) > 10 * 1024 * 1024:
                raise ValueError("Image too large")
            content.append(
                {
                    "type": "input_image",
                    "image_url": "data:" + r["type"] + ";base64," + base64.b64encode(data).decode(),
                }
            )
        return content

    @admits_work(lambda request: request.action == "infer")
    async def request(self, request):
        if self.stopping:
            raise RuntimeError("Model service stopping")
        task = asyncio.current_task()
        self.active.add(task)
        try:
            return await self.handle(request)
        finally:
            self.active.discard(task)

    async def handle(self, request):
        if request.action in {"list", "status"}:
            return {**self.diagnostics(), "models": self.settings.catalog(), "memory": self.settings.memory}
        if request.action == "health":
            if not self.client or self.error or not self.settings.enabled:
                return {**self.diagnostics(), "reachable": False}
            return await self.client.health()
        if request.action != "infer":
            raise ValueError("Unknown model action")
        a = request.arguments
        if not isinstance(a, dict) or set(a) - {
            "request_id",
            "model",
            "text",
            "images",
            "schema",
            "reasoning_effort",
        }:
            raise ValueError("Unknown inference arguments")
        ident = str(uuid.UUID(a["request_id"]))
        if ident != a["request_id"]:
            raise ValueError("Canonical request UUID required")
        m = self.settings.entry(a["model"])
        reasoning_effort(m, a.get("reasoning_effort"))
        caller = await self.caller(request.caller_run)
        if not isinstance(a.get("text"), str) or not 1 <= len(a["text"]) <= 24000:
            raise ValueError("Inference requires bounded text")
        if a.get("images") and not m.get("images"):
            raise ValueError("Model does not support images")
        if a.get("schema") is not None:
            if not m.get("structured"):
                raise ValueError("Model does not support structured output")
            from jsonschema import Draft202012Validator

            Draft202012Validator.check_schema(a["schema"])
            if any(k in json.dumps(a["schema"]) for k in ('"$ref"', '"$dynamicRef"')):
                raise ValueError("External/reference schemas are not supported")
        serialized = json.dumps(a, sort_keys=True, ensure_ascii=False)
        if len(serialized.encode()) > 60000:
            raise ValueError("Inference request too large")
        fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
        key = (request.caller_run or "foreground") + ":" + ident
        prior = self.receipt(key, fingerprint, ident)
        if prior is not None:
            return prior
        if not self.client or self.error:
            raise RuntimeError("Model gateway unavailable")
        if self.pending >= self.settings.concurrency + self.settings.queue_size:
            raise RuntimeError("Model request queue full")
        self.pending += 1
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                async with self.semaphore:
                    if await self.caller(request.caller_run) != caller:
                        raise ValueError("Model caller execution version changed")
                    # Recheck after admission wait to avoid duplicate paid requests.
                    prior = self.receipt(key, fingerprint, ident)
                    if prior is not None:
                        return prior
                    content = [{"type": "input_text", "text": a["text"]}]
                    if a.get("images"):
                        content += await self.images(a["images"], request.caller_run)
                    payload = {
                        "model": a["model"],
                        "input": [{"role": "user", "content": content}],
                        "store": False,
                        "max_output_tokens": m.get("max_output_tokens", self.settings.max_output_tokens),
                    }
                    if a.get("schema") is not None:
                        payload["text"] = {
                            "format": {
                                "type": "json_schema",
                                "name": "result",
                                "schema": a["schema"],
                                "strict": True,
                            }
                        }
                    if await self.caller(request.caller_run) != caller:
                        raise ValueError("Model caller execution version changed")
                    prior = self.receipt(key, fingerprint, ident)
                    if prior is not None:
                        return prior
                    with self.db:
                        self.db.execute(
                            "INSERT INTO model_calls VALUES (?,?,?,?,?,?,?,?)",
                            (
                                key,
                                fingerprint,
                                caller,
                                a["model"],
                                "submitted",
                                None,
                                time.time(),
                                self.settings.fingerprint(),
                            ),
                        )
                    attempts = []
                    try:
                        session = (
                            caller
                            if request.caller_run
                            else getattr(
                                getattr(self.host, "conversation", None), "session", self.session_namespace
                            )
                        )
                        session_id = hashlib.sha256(
                            (str(self.host.config.root) + ":" + str(session)).encode()
                        ).hexdigest()
                        candidates = [a["model"], *m.get("fallbacks", [])]
                        for index, candidate in enumerate(candidates):
                            try:
                                selected = self.settings.entry(candidate)
                                effort = reasoning_effort(selected, a.get("reasoning_effort"))
                                if (a.get("images") and not selected.get("images")) or (
                                    a.get("schema") is not None and not selected.get("structured")
                                ):
                                    raise ValueError("Fallback lacks required capability")
                            except ValueError:
                                attempts.append(
                                    {
                                        "model": candidate,
                                        "state": "skipped",
                                        "error": "incompatible_configuration",
                                    }
                                )
                                log.warning(
                                    "Model candidate skipped request=%s model=%s reason=incompatible_configuration",
                                    ident,
                                    candidate,
                                )
                                continue
                            if await self.caller(request.caller_run) != caller:
                                raise ValueError("Model caller execution version changed")
                            attempted = {"model": candidate, "reasoning_effort": effort, "state": "submitted"}
                            attempts.append(attempted)
                            with self.db:
                                self.db.execute(
                                    "UPDATE model_calls SET result=? WHERE id=?",
                                    (
                                        json.dumps(
                                            {"state": "submitted", "request_id": ident, "attempts": attempts}
                                        ),
                                        key,
                                    ),
                                )
                            try:
                                from assistant_runtime.api.ai_usage import metered

                                scope = await self.host.ai_usage.scope(request.caller_run)
                                selected_payload = {
                                    **payload,
                                    "model": candidate,
                                    "reasoning": {"effort": effort},
                                    "max_output_tokens": selected.get(
                                        "max_output_tokens", self.settings.max_output_tokens
                                    ),
                                }
                                result = await metered(
                                    self.host.ai_usage.service,
                                    scope,
                                    "memory" if scope.work_id.startswith("memory:") else "inference",
                                    selected["provider"],
                                    selected["model"],
                                    lambda: self.client.complete(selected_payload, session_id=session_id),
                                    bounds={
                                        "input_tokens": selected.get("context_window", 128000),
                                        "output_tokens": await self.client.output_bound(selected_payload)
                                        if hasattr(self.client, "output_bound")
                                        else selected_payload["max_output_tokens"],
                                    },
                                    body=selected_payload,
                                )
                                attempted["state"] = "completed"
                                result.update(
                                    selected_model=candidate, reasoning_effort=effort, attempts=attempts
                                )
                                break
                            except ModelCallError as exc:
                                attempted.update(state="failed", error=exc.code)
                                log.error(
                                    "Model call failed request=%s model=%s error=%s fallback=%s",
                                    ident,
                                    candidate,
                                    exc.code,
                                    exc.fallback_allowed and index + 1 < len(candidates),
                                )
                                if not exc.fallback_allowed or index + 1 == len(candidates):
                                    raise
                        else:
                            raise ModelCallError("no_compatible_fallback")
                        if a.get("schema") is not None:
                            from jsonschema import Draft202012Validator

                            result["data"] = json.loads(result["text"])
                            Draft202012Validator(a["schema"]).validate(result["data"])
                        if await self.caller(request.caller_run) != caller:
                            raise ValueError("Model caller execution version changed")
                        result.update(
                            state="completed", request_id=ident, model=a["model"], caller_version=caller
                        )
                        with self.db:
                            self.db.execute(
                                "UPDATE model_calls SET state='completed',result=? WHERE id=?",
                                (json.dumps(result, ensure_ascii=False), key),
                            )
                        return result
                    except BaseException as exc:
                        from assistant_runtime.api.ai_usage import UsageError

                        failed = isinstance(exc, (ModelCallError, UsageError))
                        if attempts and attempts[-1]["state"] == "submitted":
                            attempts[-1]["state"] = "blocked" if isinstance(exc, UsageError) else "unknown"
                        state = "failed" if failed else "unknown"
                        log.error(
                            "Model request ended request=%s state=%s error=%s",
                            ident,
                            state,
                            type(exc).__name__,
                        )
                        with self.db:
                            self.db.execute(
                                "UPDATE model_calls SET state=?,result=? WHERE id=?",
                                (
                                    state,
                                    json.dumps({"state": state, "request_id": ident, "attempts": attempts}),
                                    key,
                                ),
                            )
                        raise
        except TimeoutError:
            log.warning(
                "model_timeout layer=auxiliary request=%s timeout_seconds=%s elapsed_seconds=%.3f",
                ident,
                self.settings.timeout_seconds,
                time.monotonic() - started,
            )
            raise
        finally:
            self.pending -= 1
