"""Operator-only live configuration composition; module code owns each update."""

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
            if key == "plugin_settings":
                affected = {
                    name
                    for name in current.enabled_plugins
                    if current.plugin_settings.get(name) != candidate.plugin_settings.get(name)
                }
            reason = ""
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
            affected = {"models", "pi"} if resource in {"credentials", "gateway_key"} else set()
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
                system.atomic_write(receipt, json.dumps({"version": revision, "phase": "applying"}).encode())
            try:
                result = await self._apply(entry, revision, resources, preview=preview)
            except BaseException:
                if not preview:
                    system.atomic_write(
                        receipt, json.dumps({"version": revision, "phase": "failed"}).encode()
                    )
                raise
            if not preview:
                system.atomic_write(receipt, json.dumps({**result, "version": revision}).encode())
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
