"""Operator-only MCP projections and isolated discovery; integrations owns policy and storage."""

import asyncio
import hashlib
import json
import time
from copy import deepcopy

from assistant_runtime.api.system import system
from assistant_runtime.core.integrations.configuration import (
    patch_registry,
    public_entry,
    tool_permission,
    validate_registry,
)
from .files import Conflict

EMPTY = {"revision": 0, "servers": {}, "removed": []}


def fingerprint(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()


class Permissions:
    def __init__(self, business):
        self.business = business
        self.path = business.state / "mcp-verifications.json"
        try:
            self.checks = json.loads(self.path.read_text()) if self.path.stat().st_size < 1024 * 1024 else {}
        except (OSError, ValueError):
            self.checks = {}
        if not isinstance(self.checks, dict):
            self.checks = {}
        self.slots = asyncio.Semaphore(2)
        self.pending = set()

    def capture(self, version=None, patch=None):
        with self.business.files.locked():
            snapshot = self.business.files.snapshot()
            if version is not None and version != snapshot.version:
                raise Conflict()
            registered = self.business.files.resources.registered(snapshot.contents)
            resource = registered.get("mcp")
            raw = snapshot.resources.get("mcp")
            if raw is None and resource is not None and resource.read_only and resource.path.is_file():
                if resource.path.stat().st_size > 512 * 1024:
                    raise ValueError("MCP registry exceeds bound")
                raw = resource.path.read_bytes()
            data = validate_registry(json.loads(raw or json.dumps(EMPTY)))
            if patch is not None:
                if resource is None or resource.read_only:
                    raise ValueError("MCP registry requires local maintenance")
                data = patch_registry(data, patch)
            loaded = getattr(self.business, "loaded_snapshot", None)
            applied = (
                validate_registry(json.loads(loaded.resources.get("mcp") or json.dumps(EMPTY)))
                if loaded
                else EMPTY
            )
            return snapshot.version, data, applied, bool(resource and not resource.read_only)

    def inventory(self, version=None, patch=None):
        version, data, applied, writable = self.capture(version, patch)
        rows = []
        for ident, row in data["servers"].items():
            public = public_entry(ident, row)
            checks = {}
            for context in ("foreground", "background"):
                check = self.checks.get(ident + ":" + context)
                if check:
                    checks[context] = {
                        **deepcopy(check),
                        "current": check.get("fingerprint") == fingerprint(row["definition"]),
                    }
                    for tool in checks[context].get("tools", []):
                        tool["permission"] = tool_permission(row["definition"], tool["name"], context)
            public.update(
                application="applied" if applied["servers"].get(ident) == row else "pending",
                verification=checks,
            )
            rows.append(public)
        return {
            "version": version,
            "revision": data["revision"],
            "writable": writable,
            "servers": rows,
            "running": getattr(self.business, "process", None) is not None
            and self.business.process.returncode is None,
            "snapshot_notice": "新策略在空闲点应用到主对话；已运行后台任务沿用启动时的权限快照。",
        }

    async def verify(self, data):
        if len(self.pending) >= 4:
            return {"state": "unavailable", "reason": "验证繁忙，请稍后重试。"}
        version, registry, _, _ = self.capture(data.get("version"), data.get("patch"))
        ident, context = data.get("name"), data.get("context", "foreground")
        if ident not in registry["servers"] or context not in ("foreground", "background"):
            raise ValueError("Invalid MCP verification target")
        row = registry["servers"][ident]
        if context not in row["definition"]["contexts"]:
            return {"state": "unavailable", "reason": "该服务未允许在所选范围使用。"}
        task = asyncio.current_task()
        self.pending.add(task)
        try:
            async with self.slots:
                try:
                    reply = await self.business.request(
                        {
                            "command": "mcp",
                            "action": "verify_draft",
                            "arguments": {"name": ident, "context": context, "definition": row["definition"]},
                        }
                    )
                except (OSError, ValueError, RuntimeError, TimeoutError):
                    reply = {"state": "unavailable", "reason": "validation_unavailable"}
            # Keep only bounded descriptive metadata, never arbitrary provider replies.
            tools = []
            for item in reply.get("tools", [])[:200]:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    continue
                annotations = item.get("annotations") or {}
                if not isinstance(annotations, dict):
                    annotations = {}
                tools.append(
                    {
                        "name": item["name"][:200],
                        "description": str(item.get("description", ""))[:300],
                        "annotations": {
                            k: v
                            for k, v in annotations.items()
                            if k in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
                            and type(v) is bool
                        },
                        "permission": tool_permission(row["definition"], item["name"], context),
                    }
                )
            check = {
                "fingerprint": fingerprint(row["definition"]),
                "configuration_version": version,
                "server_revision": row["revision"],
                "checked_at": time.time(),
                "state": reply.get("state", "unavailable"),
                "tools_count": reply.get("tools_count", len(tools)),
                "tools": tools,
                "truncated": bool(reply.get("truncated"))
                or (isinstance(reply.get("tools_count"), int) and reply["tools_count"] > len(tools)),
                "validation": "initialize_and_discover_no_tool_execution",
            }
            reasons = {
                "validation_unavailable": "业务服务未就绪或验证未完成；未调用模型或业务工具。",
                "missing_service_environment": "缺少所需环境凭据，请先保存凭据或检查服务环境。",
                "command_not_found": "找不到 MCP 可执行文件，请先安装软件或更正命令。",
                "working_directory_missing": "工作目录不存在，请更正目录。",
            }
            if reply.get("reason") in reasons:
                check["reason"] = reasons[reply["reason"]]
            elif reply.get("missing_allowed_tools"):
                check["reason"] = "已发现工具，但指定范围中包含未找到的工具；请检查名称。"
            # Changes during a probe never attach stale evidence to a different draft.
            self.capture(version)
            self.checks.pop(ident + ":" + context, None)
            self.checks[ident + ":" + context] = check
            self.checks = dict(list(self.checks.items())[-128:])
            raw = json.dumps(self.checks).encode()
            while len(raw) >= 1024 * 1024:
                self.checks.pop(next(iter(self.checks)))
                raw = json.dumps(self.checks).encode()
            system.atomic_write(self.path, raw)
            return {**check, "current": True, "context": context}
        finally:
            self.pending.discard(task)

    async def close(self):
        for task in list(self.pending):
            task.cancel()
        await asyncio.gather(*self.pending, return_exceptions=True)
