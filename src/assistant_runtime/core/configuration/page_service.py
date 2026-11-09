"""Administrator-only page endpoints; config ownership stays on the server."""

import asyncio
import hashlib
import json
import mimetypes
import re
import time
from aiohttp import web
from assistant_runtime.api.configuration import ConfigurationError
from assistant_runtime.api.system import system
from . import pages
from .files import Conflict
from .schema import resolve
from assistant_runtime.api.configuration import ConfigurationSpec


class PageService:
    def __init__(self, console):
        self.console = console
        self.active = set()

    async def asset(self, request):
        owner = request.match_info["owner"]
        spec = pages.description(owner)
        page = spec.get("interface", {}).get("page")
        if not page:
            raise web.HTTPNotFound(text="插件没有管理页面")
        try:
            path = pages.asset(owner, page, request.match_info["path"])
        except (ValueError, OSError):
            raise web.HTTPNotFound(text="插件页面资源缺失或不支持，请检查安装包") from None
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".js":
            mime = "text/javascript"
        return web.Response(body=await asyncio.to_thread(path.read_bytes), content_type=mime)

    async def validate_patch(self, request):
        from .fields import patch_documents

        owner = request.match_info["owner"]
        spec = pages.description(owner)
        data = await request.json()
        prefix = spec["namespace"].split(".")
        if not spec["namespace"]:
            raise ValueError("An independently managed page needs an owned namespace")
        patch = data.get("patch", {})
        path = patch.get("path")
        if not isinstance(path, list) or any(p in {"__proto__", "constructor", "prototype"} for p in path):
            raise ValueError("Invalid field path")
        scoped = {**patch, "path": prefix + path}
        with self.console.files.locked():
            snapshot = self.console.files.snapshot()
            if data.get("version") != snapshot.version:
                raise Conflict()
            # Validate ownership, read-only and secret operation semantics, without saving.
            patch_documents(snapshot, self.console.files.entry.name, [scoped])
        return web.json_response({"ok": True})

    async def action(self, request):
        owner = request.match_info["owner"]
        spec = pages.description(owner)
        data = await request.json()
        name = data.get("action")
        operation = spec.get("interface", {}).get("actions", {}).get(name)
        if not operation:
            raise ConfigurationError([owner], "undeclared", "管理操作未声明")
        read_only = operation.get("read_only", False)
        if type(read_only) is not bool:
            raise ValueError("Invalid operation declaration")
        arguments = resolve(
            ConfigurationSpec(schema=operation.get("input", {"type": "object"})), data.get("arguments", {})
        )
        ident = data.get("request_id", "")
        if not re.fullmatch(r"[a-zA-Z0-9-]{16,80}", ident):
            raise ValueError("A unique request ID is required")
        receipt = self.console.business.state / "page-operations" / owner / (ident + ".json")
        fingerprint = hashlib.sha256(
            json.dumps([name, arguments], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        def recorded():
            value = json.loads(receipt.read_text(encoding="utf-8"))
            if value.pop("fingerprint", None) != fingerprint:
                raise ConfigurationError([owner, name], "request_conflict", "操作标识已用于另一组参数")
            return web.json_response(value)

        key = (owner, name)
        if not read_only and receipt.exists():
            return recorded()
        if key in self.active:
            return web.json_response(
                {"error": "此操作仍在执行，请等待或查询状态", "code": "operation_busy"}, status=409
            )
        if not read_only and data.get("confirmed") is not True:
            raise ConfigurationError([owner, name], "confirmation", "此操作需要确认")
        with self.console.files.locked():
            if data.get("version") != self.console.files.snapshot().version:
                raise Conflict()
        status = await self.console.business.status()
        if not status.get("loaded_version") or status.get("state", "running") not in {"running", "degraded"}:
            return web.json_response({"error": "业务未启动，仍可编辑配置", "code": "unavailable"}, status=503)
        if not read_only and status["loaded_version"] != data.get("version"):
            raise ConfigurationError([owner], "not_applied", "请先处理未应用的配置，再执行此操作")
        # Recheck after await: concurrent clicks cannot dispatch twice.
        if key in self.active:
            return web.json_response({"error": "操作仍在执行", "code": "operation_busy"}, status=409)
        if not read_only and receipt.exists():
            return recorded()
        self.active.add(key)
        result = {
            "request_id": ident,
            "state": "unknown",
            "code": "outcome_unknown",
            "message": "操作结果待确认；请查询插件状态，不要直接重复操作",
        }
        try:
            if not read_only:
                receipt.parent.mkdir(parents=True, exist_ok=True)
                system.private(receipt.parent, directory=True)
                system.atomic_write(receipt, json.dumps({**result, "fingerprint": fingerprint}).encode())
            try:
                value = await self.console.business.request(
                    {
                        "command": "configuration-action",
                        "owner": owner,
                        "action": name,
                        "arguments": arguments,
                        "confirmed": data.get("confirmed", False),
                        "version": status["loaded_version"],
                    }
                )
                result = {"request_id": ident, "state": "completed", "result": value}
            except ConfigurationError as exc:
                result = {
                    "request_id": ident,
                    "state": "unknown" if exc.code == "outcome_unknown" else "rejected",
                    "code": exc.code,
                    "issues": [exc.public()],
                    "message": str(exc),
                }
            except (Exception, asyncio.CancelledError):
                if read_only:
                    result = {
                        "request_id": ident,
                        "state": "unavailable",
                        "code": "operation_unavailable",
                        "message": "查询未完成，请检查插件运行状态",
                    }
            if not read_only:
                # Persist only safe status, never operation arguments or result payloads.
                saved = {k: v for k, v in result.items() if k != "result"}
                saved["recorded_at"] = time.time()
                saved["fingerprint"] = fingerprint
                system.atomic_write(receipt, json.dumps(saved).encode())
            return web.json_response(result)
        finally:
            self.active.discard(key)
