"""Operator-only live configuration composition; module code owns each update."""

import asyncio
from dataclasses import fields
from pathlib import Path
import os

from assistant_runtime.api.configuration import ConfigurationPending
from assistant_runtime.core.configuration.updates import UpdateCoordinator
from assistant_runtime.core.configuration.interface import FIELD_OWNERS
from assistant_runtime.core.plugin_catalog import OWNERS, declaration

RESTART = {
    "root",
    "plugins",
    "bindings",
    "profile",
    "backend",
    "pi_binary",
    "pi_shell",
    "unit_name",
    "model_dir",
    "input_device",
    "default_output",
    "echo_cancel",
    "memory_enabled",
}


class ConfigurationRuntime:
    def __init__(self, host):
        self.host = host
        self.updates = UpdateCoordinator()
        import asyncio

        self.operation = asyncio.Lock()
        self.task = None
        self.intent = None
        self.receipt = {"phase": "idle"}
        self.updates.on_stage = self._progress

    def status(self):
        return {**self.receipt, "busy": bool(self.task and not self.task.done())}

    def _progress(self, stage):
        import logging
        import json
        import time
        from assistant_runtime.api.system import system

        self.receipt.update(
            stage,
            phase="recovering" if stage["stage"] in {"rollback", "cleanup"} else "applying",
            updated=time.time(),
        )
        logging.getLogger(__name__).info(
            "configuration_update_stage version=%s owner=%s stage=%s",
            self.receipt.get("version"),
            stage["owner"],
            stage["stage"],
        )
        try:
            system.atomic_write(
                self.host.config.state_dir / "configuration/hot-receipt.json",
                json.dumps(self.receipt).encode(),
            )
        except OSError:
            # Telemetry failure must never skip owned rollback/cleanup.
            logging.getLogger(__name__).warning("configuration_stage_receipt_unavailable")

    async def submit(self, entry, revision, resources=(), *, preview=False, attempt_id=None):
        # IPC acknowledges the intent; reconnects query the same transaction rather
        # than holding a socket open or queuing duplicate updates behind its lock.
        if preview:
            return await self.apply(entry, revision, resources, preview=True)
        intent = (str(entry), revision, tuple(sorted(resources)), attempt_id)
        if self.task and not self.task.done():
            if intent != self.intent:
                from assistant_runtime.api.configuration import ConfigurationError

                raise ConfigurationError(
                    ["configuration"], "application_busy", "上一轮配置仍在处理，请稍后再保存"
                )
            return self.status()
        if intent == self.intent and self.receipt.get("phase") != "waiting":
            return self.status()
        self.intent = intent
        self.receipt = {"version": revision, "phase": "preparing", "attempt_id": attempt_id}
        self.task = asyncio.create_task(
            self._run_update(entry, revision, resources), name="configuration-update-worker"
        )
        return self.status()

    async def _run_update(self, entry, revision, resources):
        import json
        from assistant_runtime.api.configuration import ConfigurationError
        from assistant_runtime.api.system import system

        attempt_id = self.receipt.get("attempt_id")
        try:
            result = await self.apply(entry, revision, resources)
        except (Exception, asyncio.CancelledError) as exc:
            result = {"phase": "failed", "diagnostic": "configuration_update_failed"}
            if isinstance(exc, ConfigurationError):
                result["issues"] = [exc.public()]
        self.receipt = {**result, "version": revision, "attempt_id": attempt_id}
        system.atomic_write(
            self.host.config.state_dir / "configuration/hot-receipt.json", json.dumps(self.receipt).encode()
        )

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def action(self, owner, action, arguments, confirmed=False):
        async with self.operation:
            return await self._action(owner, action, arguments, confirmed)

    async def _action(self, owner, action, arguments, confirmed=False):
        from assistant_runtime.api.configuration import ConfigurationSpec, ConfigurationError
        from assistant_runtime.core.configuration.schema import resolve
        from assistant_runtime.core.configuration.fields import redact_declared

        plugin = self.host.runtime.plugins.get(owner)
        if plugin is None:
            raise ConfigurationError([owner], "disabled", "请先启用此功能")
        spec = declaration(owner) if owner in OWNERS else plugin.definition
        actions = spec.configuration.interface.get("actions", {}) if spec.configuration else {}
        operation = actions.get(action)
        if not operation:
            raise ConfigurationError([owner, action], "undeclared", "此管理操作未声明")
        if not operation.get("read_only", False) and confirmed is not True:
            raise ConfigurationError([owner, action], "confirmation", "此操作需要明确确认")
        value = resolve(
            ConfigurationSpec(
                schema=operation.get("input", {"type": "object", "additionalProperties": False})
            ),
            arguments,
        )
        result = await plugin.configuration_action(action, value)
        schema = operation["output"]
        checked = resolve(ConfigurationSpec(schema=schema), result)
        return redact_declared(checked, schema)

    def plan(self, candidate, resources=()):
        current = self.host.config
        changed = [f.name for f in fields(current) if getattr(current, f.name) != getattr(candidate, f.name)]
        mapping = {key: {owner} for key, owner in FIELD_OWNERS.items()}
        from assistant_runtime.core.configuration.catalog import catalog

        inventory = catalog()
        from assistant_runtime.core.configuration.policies import changes as changed_paths, field_policy

        for item in inventory["plugins"]:
            description = item.get("configuration") or {}
            namespace = description.get("namespace", "")
            if namespace and "." not in namespace:
                mapping.setdefault(namespace, set()).add(item["id"])
        mapping["plugin_settings"] = set()
        mapping.update(
            {
                "main_model": {"pi", "execution"},
                "main_reasoning_effort": {"pi", "execution"},
                "model_api": {"models", "pi"},
                "workspace": {"pi"},
                "instance_skills": {"pi", "execution"},
                "work_files": {"workfiles"},
            }
        )
        for name in current.enabled_plugins:
            if name in OWNERS:
                spec = declaration(name)
                if spec.config_key:
                    mapping.setdefault(spec.config_key, set()).add(name)
                for key in spec.config_fields:
                    mapping.setdefault(key, set()).add(name)
        owners, restart, changes = set(), [], []
        for key in changed:
            affected = mapping.get(key, set()) & set(current.enabled_plugins)
            if key == "plugin_backends":
                from assistant_runtime.api.backends import select_backend

                mapping.setdefault(key, set())
                affected = set()
                for name in current.enabled_plugins:
                    if name not in candidate.enabled_plugins:
                        continue  # Removed owners need no candidate implementation.
                    spec = declaration(name) if name in OWNERS else self.host.runtime.plugins[name].definition
                    before = select_backend(
                        name,
                        spec.backends,
                        self.host.runtime.platform.name,
                        current.plugin_backends.get(name, {}),
                    )
                    after = select_backend(
                        name,
                        spec.backends,
                        self.host.runtime.platform.name,
                        candidate.plugin_backends.get(name, {}),
                    )
                    if before != after:
                        affected.add(name)
            if key == "plugin_settings":
                affected = {
                    name
                    for name in current.enabled_plugins
                    if current.plugin_settings.get(name) != candidate.plugin_settings.get(name)
                }
            reason = ""
            if key == "plugin_backends" and affected:
                from .configuration_replacement import REPLACEABLE

                if (
                    affected - REPLACEABLE
                    or "pipewire" in affected
                    and "assistant" in current.enabled_plugins
                ):
                    reason = "实现切换需要重新装配模块；固定主语音实现须重启生效"
            paths = list(changed_paths(getattr(current, key), getattr(candidate, key), (key,)))
            if key in RESTART:
                reason = "实例或插件装配设置需要重启"
            elif key == "work_files" and current.work_files.get("root") != candidate.work_files.get("root"):
                reason = "工作文件根目录变更需要维护迁移"
            elif key == "devices":
                fixed = {current.input_device, current.default_output} - {""}
                if any(current.devices.get(n) != candidate.devices.get(n) for n in fixed):
                    reason = "固定主语音设备变更需要重启"
                else:
                    adapters = {
                        row["adapter"] for row in current.devices.values() if row.get("enabled", True)
                    }
                    incoming = {
                        row["adapter"] for row in candidate.devices.values() if row.get("enabled", True)
                    }
                    if adapters != incoming:
                        reason = "设备适配器装配关系变更需要重启"
                    changed_devices = {
                        ident
                        for ident in current.devices.keys() | candidate.devices.keys()
                        if current.devices.get(ident) != candidate.devices.get(ident)
                    }
                    affected.update(
                        devices[ident]["adapter"]
                        for devices in (current.devices, candidate.devices)
                        for ident in changed_devices & devices.keys()
                        if devices[ident]["adapter"] in current.enabled_plugins
                    )
            if not reason and any(
                not hasattr(self.host.runtime.plugins[name], "prepare_configuration") for name in affected
            ):
                reason = "所属模块尚未声明在线更新协议"
            elif not reason and not affected and key not in mapping:
                reason = "此设置尚未声明在线更新协议"
            if not reason and affected and any(field_policy(inventory, path) == "restart" for path in paths):
                reason = "模块声明此结构性变更需要重启或维护"
            if reason:
                restart.append(key)
            else:
                owners.update(affected)
            changes.extend(
                {
                    "path": path,
                    "owners": sorted(affected),
                    "mode": "restart" if reason else field_policy(inventory, path),
                    "reason": reason,
                }
                for path in paths
            )
        for resource in resources:
            affected = {"models", "pi"} if resource == "credentials" else set()
            if resource == "credentials":
                affected.update(
                    name
                    for name in current.enabled_plugins
                    if name in {"search", "decisions", "media", "integrations"}
                )
            if resource == "web_access":
                affected = {"web"}
            elif resource == "mcp":
                affected = {"integrations"}
            elif ":" in resource and resource.split(":")[0] in current.enabled_plugins:
                affected = {resource.split(":")[0]}
            elif resource.startswith(("skill:", "asset:")):
                affected = {"pi", "execution"}
            if resource == "management":
                continue
            affected &= set(current.enabled_plugins)
            if affected and all(
                hasattr(self.host.runtime.plugins[n], "prepare_configuration") for n in affected
            ):
                owners.update(affected)
            elif affected:
                restart.append("resource:" + resource)
        for name, context in self.host.runtime.contexts.items():
            if context.error == "configuration_rollback_failed":
                restart.append("recovery:" + name)
                changes.append(
                    {
                        "path": [name],
                        "owners": [name],
                        "mode": "restart",
                        "reason": "上次更新无法恢复此模块，需明确重启恢复服务",
                    }
                )
        return {"fields": changes, "owners": sorted(owners), "restart": restart}

    async def apply(self, entry, revision, resources=(), *, preview=False):
        async with self.operation:
            from assistant_runtime.api.system import system
            import json

            receipt = self.host.config.state_dir / "configuration/hot-receipt.json"
            if not preview:
                self.receipt = {
                    "version": revision,
                    "phase": "preparing",
                    "attempt_id": self.receipt.get("attempt_id"),
                }
                system.atomic_write(receipt, json.dumps(self.receipt).encode())
            try:
                result = await self._apply(entry, revision, resources, preview=preview)
            except BaseException:
                if not preview:
                    system.atomic_write(
                        receipt,
                        json.dumps(
                            {
                                "version": revision,
                                "phase": "failed",
                                "attempt_id": self.receipt.get("attempt_id"),
                            }
                        ).encode(),
                    )
                raise
            if not preview:
                system.atomic_write(
                    receipt,
                    json.dumps(
                        {**result, "version": revision, "attempt_id": self.receipt.get("attempt_id")}
                    ).encode(),
                )
            return result

    async def _apply(self, entry, revision, resources=(), *, preview=False):
        from .config import Config
        import re

        if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", revision):
            raise ValueError("Invalid configuration revision")
        folder = self.host.config.state_dir / "configuration/generations" / revision
        path = Path(entry).resolve()
        if not revision or path.parent != folder.resolve() or not path.is_file():
            raise ValueError("Update must reference an immutable instance generation")
        candidate = Config.load(path)
        if candidate.root != self.host.config.root:
            raise ValueError("Cannot change instance identity online")
        capacity = getattr(self.host, "execution_capacity", None)
        capacity_limit = None
        if capacity is not None and "execution" not in self.host.config.enabled_plugins:
            from assistant_runtime.core.execution.declaration import settings

            capacity_limit = settings(candidate.execution)["concurrency"]
        plan = self.plan(candidate, resources)
        if preview or plan["restart"]:
            return {**plan, "phase": "pending_restart" if plan["restart"] else "ready"}
        if "pi" in plan["owners"] and self.host.intake.pending:
            return {**plan, "phase": "waiting"}
        import asyncio

        barriers = [self.host.runtime]
        if "pi" in self.host.runtime.plugins:
            barriers.append(self.host.runtime.plugins["pi"].backend.client)
        for barrier in barriers:
            barrier.configuration_tasks = self.updates.authorized_tasks
            barrier.configuration_ready.clear()
        self.updates.authorized_tasks.add(asyncio.current_task())
        previous = self.host.config
        manifest = os.environ.get("PERSONAL_AGENT_RESOURCE_MANIFEST")
        try:
            # Only preparation and its commit run under the candidate resource view.
            # No event-loop task can inherit it accidentally (configuration_io uses contextvars).
            from assistant_runtime.api.configuration_io import resource_view

            with resource_view(folder / "resources.json"):
                from .bootstrap import assistant_skill_paths

                candidate._prepared_skill_paths = assistant_skill_paths(candidate)
                await self.updates.apply(
                    [
                        (
                            name,
                            lambda name=name: self.host.runtime.plugins[name].prepare_configuration(
                                candidate
                            ),
                        )
                        for name in self.host.runtime.order()
                        if name in plan["owners"]
                    ]
                )
            # Models/Pi can own the host pool without enabling ExecutionPlugin.
            # Validate above; resize only after all prepared module updates commit.
            if capacity_limit is not None:
                capacity.resize(capacity_limit)
            self.host.config = candidate
            os.environ["PERSONAL_AGENT_RESOURCE_MANIFEST"] = str(folder / "resources.json")
            os.environ["PERSONAL_AGENT_CONFIGURATION_VERSION"] = revision
            for name in self.updates.cleanup_failures:
                self.updates.failed_owners.add(name)
            self.block_failed()
            return {
                **plan,
                "phase": "applied",
                "version": revision,
                "cleanup_failed": sorted(self.updates.cleanup_failures),
            }
        except ConfigurationPending:
            if self.updates.failed_owners:
                self.block_failed()
                raise RuntimeError("Configuration rollback failed; affected owners are unavailable")
            return {**plan, "phase": "waiting"}
        except BaseException as exc:
            self.host.config = previous
            if manifest is not None:
                os.environ["PERSONAL_AGENT_RESOURCE_MANIFEST"] = manifest
            self.block_failed()
            from assistant_runtime.api.configuration import ConfigurationError

            if (
                isinstance(exc, Exception)
                and not isinstance(exc, ConfigurationError)
                and self.updates.failure
            ):
                owner, stage = self.updates.failure
                recovery = (
                    "受影响模块已停止接收新请求，请重试并按提示重启恢复"
                    if self.updates.failed_owners
                    else "原配置已保留，请检查连接及凭据后重试"
                )
                raise ConfigurationError(
                    [owner],
                    "update_" + stage,
                    f"模块 {owner} 在{('准备' if stage == 'prepare' else '切换')}配置时失败（{type(exc).__name__}）；{recovery}",
                ) from exc
            raise
        finally:
            self.updates.authorized_tasks.discard(asyncio.current_task())
            for barrier in barriers:
                barrier.configuration_ready.set()

    def block_failed(self):
        unavailable = set(self.updates.failed_owners)
        for name in self.host.runtime.order():
            if set(self.host.runtime.plugins[name].manifest.requires) & unavailable:
                unavailable.add(name)
            if name in unavailable:
                context = self.host.runtime.contexts.get(name)
                if context:
                    context.state, context.error = "failed", "configuration_rollback_failed"
        if "pi" in unavailable:
            self.host.runtime.plugins["pi"].backend.client.configuration_unavailable = True
