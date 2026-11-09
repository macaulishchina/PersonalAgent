"""Separate authenticated operator entrance. No assistant tools are registered."""

import asyncio
import logging
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from .auth import AdminAccess, PasswordPolicyError
from .files import Conflict
from .catalog import catalog
from assistant_runtime.api.plugins import PlatformCertificationError
from assistant_runtime.api.configuration import ConfigurationError
from assistant_runtime.app.versions import CompatibilityError

COOKIE = "personal_agent_admin"
log = logging.getLogger(__name__)


class ConfigurationBusy(ValueError):
    pass


class Console:
    def __init__(self, business):
        self.business = business
        self.files = business.files
        self.settings = business.settings
        self.access = AdminAccess(business.state / "admin-token")
        self.runner = None
        self.site = None
        self.business.management_applied = self.reconfigure
        self.writes = asyncio.Lock()
        from .save_flow import SaveFlow

        self.save_flow = SaveFlow(business)
        from .chat_gateway import ChatGateway

        self.chat_gateway = ChatGateway(business)
        from .entrances import EntranceGateway

        self.entrances = EntranceGateway(business)
        from .accounts import Accounts

        self.accounts = Accounts(business)
        from .provider_queries import ProviderQueries

        self.provider_queries = ProviderQueries(business, self.accounts)
        from .permissions import Permissions

        self.permissions = Permissions(business)
        from .page_service import PageService

        self.plugin_pages = PageService(self)

    @web.middleware
    async def guard(self, request, handler):
        # Reject origin confusion even on login; never trust forwarded headers by default.
        settings = getattr(self, "settings", None)
        public_origin = getattr(settings, "public_origin", "")
        trusted = False
        if public_origin and request.remote:
            import ipaddress

            try:
                trusted = any(
                    ipaddress.ip_address(request.remote) in ipaddress.ip_network(p, strict=False)
                    for p in settings.trusted_proxies
                )
            except ValueError:
                pass
        expected = public_origin if trusted else request.scheme + "://" + request.host
        request["secure_context"] = expected.startswith("https://")
        if request.path.startswith("/entrances/"):
            # Native entrances follow the configured listener/proxy transport.
            # Their gateway rejects browser cookies/origins and the owning
            # plugin authenticates device credentials; HTTP/WS is supported.
            return await handler(request)
        if request.path in {"/", "/chat"} or request.path.startswith("/chat/"):
            origin = request.headers.get("Origin")
            if (origin and origin != expected) or (
                request.method not in {"GET", "HEAD"} and origin != expected
            ):
                raise web.HTTPForbidden(text="跨站请求已拒绝")
            request["gateway_scheme"] = urlsplit(expected).scheme
            request["gateway_origin"] = expected
            request["gateway_trusted_origin"] = bool(public_origin and trusted)
            return await handler(request)
        if request.method not in {"GET", "HEAD"}:
            origin = urlsplit(request.headers.get("Origin", ""))
            if origin.scheme + "://" + origin.netloc != expected:
                raise web.HTTPForbidden(text="请求来源不匹配")
            if request.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise web.HTTPUnsupportedMediaType()
        if request.path.startswith("/api/") and request.path != "/api/login":
            if not self.access.valid(request.cookies.get(COOKIE)):
                raise web.HTTPUnauthorized(text="请使用管理员口令登录")
        try:
            if request.method not in {"GET", "HEAD"} and not isinstance(await request.json(), dict):
                raise ValueError("Expected a configuration operation object")
            response = await handler(request)
        except ConfigurationBusy as exc:
            response = web.json_response({"error": str(exc), "code": "configuration_busy"}, status=409)
        except Conflict:
            response = web.json_response({"error": "配置已被其他操作修改，请重新加载"}, status=409)
        except PermissionError:
            response = web.json_response({"error": "登录失败或请求过于频繁"}, status=403)
        except PasswordPolicyError as exc:
            response = web.json_response({"error": str(exc)}, status=400)
        except PlatformCertificationError as exc:
            response = web.json_response({"error": str(exc), "code": "platform_not_certified"}, status=400)
        except CompatibilityError as exc:
            response = web.json_response({"error": str(exc), "code": "version_incompatible"}, status=400)
        except ConfigurationError as exc:
            log.warning(
                "configuration_validation_failed code=%s section=%s",
                exc.code,
                ".".join(map(str, exc.path[:2])),
            )
            response = web.json_response({"error": str(exc), "issues": [exc.public()]}, status=400)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            # Parser and owner exception strings may contain credentials. Do not echo them.
            import traceback

            log.warning(
                "configuration_operation_failed type=%s frames=%s",
                type(exc).__name__,
                [
                    f"{Path(f.filename).name}:{f.lineno}:{f.name}"
                    for f in traceback.extract_tb(exc.__traceback__)
                ],
            )
            response = web.json_response(
                {"error": "配置或文件操作失败；请检查字段、依赖及本地配置"}, status=400
            )
        response.headers.setdefault("Cache-Control", "no-store")
        plugin_page = request.path.startswith("/api/plugin-pages/")
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors "
                + ("'self'; form-action 'none'" if plugin_page else "'none'")
                + "; base-uri 'none'",
            }
        )
        return response

    async def start(self):
        app = web.Application(middlewares=[self.guard], client_max_size=3 * 1024 * 1024)
        app.add_routes(
            [
                web.get("/", self.chat_gateway.forward),
                web.route("*", "/entrances/{owner}/{route}", self.entrances.forward),
                web.post("/api/entrances/{owner}", self.entrance_manage),
                web.get("/config", self.configuration_redirect),
                web.get("/config/", self.index),
                web.route("*", "/chat", self.chat_gateway.forward),
                web.route("*", "/chat/{path:.*}", self.chat_gateway.forward),
                web.get("/console.js", self.javascript),
                web.get("/workspace.js", self.workspace_javascript),
                web.get("/plugin-pages.js", self.plugin_pages_javascript),
                web.get("/api/plugin-pages/{owner}/assets/{path:.*}", self.plugin_pages.asset),
                web.post("/api/plugin-pages/{owner}/validate", self.plugin_pages.validate_patch),
                web.post("/api/plugin-pages/{owner}/action", self.plugin_pages.action),
                web.get("/model-editor.js", self.model_javascript),
                web.get("/task-models.js", self.task_models_javascript),
                web.get("/connections.js", self.connections_javascript),
                web.get("/permissions.js", self.permissions_javascript),
                web.get("/api/permissions/mcp", self.permissions_inventory),
                web.post("/api/permissions/mcp", self.permissions_preview),
                web.post("/api/permissions/mcp/verify", self.permissions_verify),
                web.get("/console.css", self.styles),
                web.post("/api/login", self.login),
                web.post("/api/logout", self.logout),
                web.get("/api/admin-password", self.password_policy),
                web.post("/api/admin-password", self.change_password),
                web.get("/api/status", self.status),
                web.get("/api/configuration", self.read),
                web.post("/api/configuration", self.save),
                web.post("/api/configuration/discard", self.discard),
                web.post("/api/configuration/restart", self.restart),
                web.get("/api/catalog", self.describe),
                web.get("/api/resources", self.resources),
                web.post("/api/result-storage", self.result_storage),
                web.get("/api/accounts", self.account_inventory),
                web.post("/api/account", self.account_action),
                web.post("/api/provider-query", self.provider_query),
                web.post("/api/model-test", self.model_test),
                web.post("/api/model-capabilities", self.model_capabilities),
                web.post("/api/model-avatar", self.model_avatar_upload),
                web.get("/api/model-avatars", self.model_avatar_catalog),
                web.get("/api/model-avatar/builtin/{name}", self.model_avatar_builtin),
                web.get("/api/model-avatar/{asset_id}", self.model_avatar_read),
                web.post("/api/account-answer", self.account_answer),
                web.post("/api/account-cancel", self.account_cancel),
                web.get("/api/repair", self.repair_read),
                web.post("/api/repair", self.repair_save),
                web.post("/api/complete-step", self.complete_step),
                web.get("/api/history", self.history),
                web.post("/api/restore", self.restore),
                web.get("/api/application-preview", self.preview),
                web.post("/api/apply", self.apply),
                web.post("/api/update", self.update),
                web.post("/api/configuration-action", self.configuration_action),
                web.post("/api/abort-apply", self.abort_apply),
            ]
        )
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.settings.host, self.settings.port)
        await self.site.start()

    async def reconfigure(self):
        from .settings import ManagementSettings

        settings = ManagementSettings.load(self.settings.root / "management.toml")
        if (settings.host, settings.port) != (self.settings.host, self.settings.port):
            site = web.TCPSite(self.runner, settings.host, settings.port)
            await site.start()  # Bind before retiring the working management endpoint.
            await self.site.stop()
            self.site = site
        self.settings = self.business.settings = settings
        return {"host": settings.host, "port": settings.port, "public_origin": settings.public_origin}

    def writable(self):
        task = getattr(self.business, "apply_task", None)
        if (task is not None and not task.done()) or self.business.application.get("phase") in {
            "preparing",
            "applying",
            "waiting",
            "cancelling",
            "restarting",
            "recovering",
        }:
            raise ConfigurationBusy("上一轮设置仍在保存生效，请稍后再试；本页修改会保留。")

    async def close(self):
        await self.chat_gateway.close()
        await self.entrances.close()
        await self.permissions.close()
        await self.provider_queries.close()
        await self.accounts.close()
        if self.runner:
            await self.runner.cleanup()

    async def configuration_redirect(self, request):
        raise web.HTTPPermanentRedirect("/config/")

    async def index(self, request):
        return web.FileResponse(Path(__file__).with_name("static") / "index.html")

    async def javascript(self, request):
        return web.FileResponse(Path(__file__).with_name("static") / "console.js")

    async def entrance_manage(self, request):
        data = await request.json()
        if set(data) != {"action", "arguments"} or not isinstance(data["arguments"], dict):
            raise ValueError("Invalid entrance operation")
        result = await self.business.request(
            {"command": "frontend-manage", "owner": request.match_info["owner"], **data}
        )
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    async def permissions_javascript(self, request):
        return web.FileResponse(Path(__file__).with_name("static") / "permissions.js")

    async def permissions_inventory(self, request):
        return web.json_response(
            await asyncio.to_thread(self.permissions.inventory, request.query.get("version"))
        )

    async def permissions_preview(self, request):
        data = await request.json()
        if not isinstance(data.get("version"), str):
            raise ValueError("Configuration version required")
        return web.json_response(
            await asyncio.to_thread(self.permissions.inventory, data["version"], data.get("patch"))
        )

    async def permissions_verify(self, request):
        data = await request.json()
        if not isinstance(data.get("version"), str):
            raise ValueError("Configuration version required")
        return web.json_response(await self.permissions.verify(data))

    async def connections_javascript(self, request):
        return web.FileResponse(Path(__file__).with_name("static") / "connections.js")

    async def model_javascript(self, request):
        return web.FileResponse(Path(__file__).with_name("static") / "model-editor.js")

    async def task_models_javascript(self, request):
        return web.FileResponse(Path(__file__).with_name("static") / "task-models.js")

    async def workspace_javascript(self, request):
        return web.FileResponse(Path(__file__).with_name("static") / "workspace.js")

    async def plugin_pages_javascript(self, request):
        return web.FileResponse(Path(__file__).with_name("static") / "plugin-pages.js")

    async def styles(self, request):
        return web.FileResponse(Path(__file__).with_name("static") / "console.css")

    async def login(self, request):
        data = await request.json()
        session = self.access.login(data.get("token"), request.remote)
        response = web.json_response({"ok": True})
        response.set_cookie(
            COOKIE,
            session,
            httponly=True,
            secure=request.get("secure_context", request.secure),
            samesite="Strict",
            path="/",
        )
        return response

    async def logout(self, request):
        self.access.logout(request.cookies.get(COOKIE, ""))
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE, path="/")
        return response

    async def password_policy(self, request):
        return web.json_response(self.access.password_policy())

    async def change_password(self, request):
        from assistant_runtime.api.configuration_io import configuration_lock
        import time

        data = await request.json()
        async with self.writes:
            with configuration_lock(self.settings.root):
                self.access.change_password(
                    data.get("current_password"),
                    data.get("new_password"),
                    data.get("confirmation"),
                    request.remote,
                )
            try:
                self.files._audit(
                    {
                        "time": time.time(),
                        "actor": "administrator",
                        "result": "administrator_password_changed",
                    }
                )
            except OSError:
                log.warning("Administrator password changed; configuration audit unavailable")
        response = web.json_response({"changed": True, "reauthentication_required": True})
        response.del_cookie(COOKIE, path="/")
        return response

    async def status(self, request):
        return web.json_response(await self.business.status())

    async def read(self, request):
        from .fields import present

        def snapshot_view():
            with self.files.locked():
                snapshot = self.files.snapshot()
                result = present(
                    snapshot,
                    self.files.entry.name,
                    root=self.files.resources.root if self.files.resources else None,
                )
                result["resources"] = (
                    self.files.resources.public(snapshot.contents, snapshot.resources)
                    if self.files.resources
                    else []
                )
                result["pending_restart"] = self.save_flow.public()
                return result

        result = await asyncio.to_thread(snapshot_view)
        from assistant_runtime.core.models.settings import PRESETS

        result["provider_presets"] = PRESETS
        result["application"] = dict(self.business.application)
        loaded = getattr(self.business, "loaded_snapshot", None)
        if loaded:
            applied = present(
                loaded,
                self.files.entry.name,
                root=self.files.resources.root if self.files.resources else None,
            )
            by_path = {tuple(field["path"]): field for field in applied["fields"]}
            for field in result["fields"]:
                previous = by_path.get(tuple(field["path"]), {})
                if "value" in previous:
                    field["loaded_value"] = previous["value"]
            result["loaded_version"] = loaded.version
        return web.json_response(result)

    async def describe(self, request):
        from .fields import public_rule, described_schema

        inventory = await asyncio.to_thread(catalog)
        from assistant_runtime.app.release_state import read as release_record

        try:
            inventory["instance_release"] = release_record(self.settings.root)
        except CompatibilityError as exc:
            inventory["instance_release_error"] = str(exc)
        for description in [
            inventory["application"],
            *(p.get("configuration") for p in inventory["plugins"]),
        ]:
            if description and "schema" in description:
                description["schema"] = public_rule(described_schema(description))
                from .schema import expand

                for operation in description.get("interface", {}).get("actions", {}).values():
                    for key in ("input", "output"):
                        if key in operation:
                            operation[key] = public_rule(expand(operation[key]))
        return web.json_response(inventory)

    async def save(self, request):
        data = await request.json()
        async with self.writes:
            self.writable()
            result = await self.save_flow.save(
                data.get("version"),
                data.get("patches"),
                data.get("resources"),
                data.get("pending_id"),
            )
        return web.json_response(result)

    async def discard(self, request):
        data = await request.json()
        async with self.writes:
            await asyncio.to_thread(self.save_flow.discard, data.get("pending_id"))
        return web.json_response({"discarded": True})

    async def restart(self, request):
        data = await request.json()
        if data.get("confirmed") is not True:
            raise ValueError("Explicit restart confirmation required")
        async with self.writes:
            self.writable()
            return web.json_response(
                await self.save_flow.restart(data.get("version"), data.get("pending_id"))
            )

    async def history(self, request):
        return web.json_response({"versions": await asyncio.to_thread(self.files.history)})

    async def restore(self, request):
        data = await request.json()
        async with self.writes:
            self.writable()
            result = await asyncio.to_thread(self.files.restore, data.get("id"), data.get("version"))
        return web.json_response(result)

    async def apply(self, request):
        data = await request.json()
        async with self.writes:
            return web.json_response(
                self.business.apply(data.get("version"), data.get("mode"), data.get("confirmed")), status=202
            )

    async def update(self, request):
        data = await request.json()
        async with self.writes:
            self.writable()
            return web.json_response(self.business.update(data.get("version")), status=202)

    async def result_storage(self, request):
        data = await request.json()
        if set(data) - {"apply", "ids", "version", "confirmed"}:
            raise ValueError("Unknown result storage fields")
        if data.get("apply") and data.get("confirmed") is not True:
            raise ValueError("Result deletion requires confirmation")
        arguments = {key: value for key, value in data.items() if key != "confirmed"}
        try:
            result = await self.business.request(
                {
                    "command": "files",
                    "action": "result_collect" if data.get("apply") else "result_usage",
                    "arguments": arguments,
                }
            )
        except (RuntimeError, OSError, TimeoutError):
            return web.json_response(
                {"error": "业务服务或文件能力不可用；仍可编辑保留策略，启动后查看占用及清理。"}, status=503
            )
        return web.json_response(result)

    async def configuration_action(self, request):
        data = await request.json()
        return web.json_response(
            await self.business.request(
                {
                    "command": "configuration-action",
                    "owner": data.get("owner"),
                    "action": data.get("action"),
                    "arguments": data.get("arguments", {}),
                    "confirmed": data.get("confirmed", False),
                }
            )
        )

    async def abort_apply(self, request):
        return web.json_response(await self.business.abort_apply())

    async def preview(self, request):
        return web.json_response(await asyncio.to_thread(self.business.preview))

    async def resources(self, request):
        def read():
            with self.files.locked():
                snapshot = self.files.snapshot()
                return {
                    "version": snapshot.version,
                    "resources": self.business.resources.public(snapshot.contents, snapshot.resources),
                }

        return web.json_response(await asyncio.to_thread(read))

    async def provider_query(self, request):
        data = await request.json()
        return web.json_response(
            await self.provider_queries.action(
                data.get("name"), data.get("action"), data.get("version"), data.get("draft")
            )
        )

    async def model_test(self, request):
        return web.json_response(await self.provider_queries.test_model(await request.json()))

    async def model_capabilities(self, request):
        from .provider_queries import ProbeInput

        try:
            return web.json_response(await self.provider_queries.describe(await request.json()))
        except ProbeInput as exc:
            return web.json_response({"error": str(exc), "field": exc.field}, status=400)

    async def model_avatar_upload(self, request):
        import base64
        from assistant_runtime.core.models.presentation import normalize, digest, MAX_UPLOAD

        data = await request.json()

        def prepare():
            with self.files.locked():
                if data.get("version") != self.files.snapshot().version:
                    raise Conflict()
            value = data.get("data_base64")
            if (
                set(data) != {"version", "data_base64"}
                or not isinstance(value, str)
                or len(value) > (MAX_UPLOAD + 2) // 3 * 4
            ):
                raise ValueError("Invalid avatar upload")
            raw = normalize(base64.b64decode(value, validate=True))
            ident = digest(raw)
            return {
                "avatar": "asset:" + ident,
                "resource": {
                    "id": "model-avatar-" + ident,
                    "op": "set",
                    "value": base64.b64encode(raw).decode(),
                },
                "media_type": "image/png",
            }

        return web.json_response(await asyncio.to_thread(prepare))

    async def model_avatar_catalog(self, request):
        from assistant_runtime.core.models.builtin_avatars import catalog

        return web.json_response(catalog())

    async def model_avatar_builtin(self, request):
        from assistant_runtime.core.models.builtin_avatars import read

        raw, version = read(request.match_info["name"], request.query.get("version"))
        headers = {
            "ETag": '"' + version + '"',
            "Cache-Control": "private, max-age=31536000, immutable"
            if "version" in request.query
            else "private, no-cache",
            "X-Content-Type-Options": "nosniff",
        }
        if request.headers.get("If-None-Match") == headers["ETag"]:
            return web.Response(status=304, headers=headers)
        return web.Response(body=raw, content_type="image/svg+xml", headers=headers)

    async def model_avatar_read(self, request):
        from assistant_runtime.core.models.presentation import read

        ident = request.match_info["asset_id"]
        if request.query.get("version") != ident:
            raise ValueError("Avatar version required")

        def capture():
            with self.files.locked():
                snapshot = self.files.snapshot()
                if "model-avatar-" + ident not in snapshot.resources:
                    raise ValueError("Avatar is not referenced by saved configuration")
                return read(self.business.settings.root, ident)

        raw = await asyncio.to_thread(capture)
        headers = {
            "ETag": '"' + ident + '"',
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        }
        if request.headers.get("If-None-Match") == headers["ETag"]:
            return web.Response(status=304, headers=headers)
        return web.Response(body=raw, content_type="image/png", headers=headers)

    async def account_inventory(self, request):
        return web.json_response(await asyncio.to_thread(self.accounts.public))

    async def account_action(self, request):
        self.writable()
        data = await request.json()
        try:
            result = await self.accounts.action(data.get("name"), data.get("action"), data.get("confirmed"))
        except (RuntimeError, TimeoutError):
            return web.json_response(
                {"error": "账号操作未完成，请检查 Pi 安装、账号配置或授权状态"}, status=400
            )
        return web.json_response(result)

    async def account_answer(self, request):
        data = await request.json()
        return web.json_response(
            await self.accounts.answer(data.get("id"), data.get("prompt_id"), data.get("value"))
        )

    async def account_cancel(self, request):
        data = await request.json()
        return web.json_response(await self.accounts.cancel(data.get("id")))

    async def complete_step(self, request):
        data = await request.json()
        return web.json_response(
            self.business.complete_step(data.get("version"), data.get("component"), data.get("confirmed"))
        )

    async def repair_read(self, request):
        return web.json_response(await asyncio.to_thread(self.files.repair_inventory))

    async def repair_save(self, request):
        data = await request.json()
        if data.get("confirmed") is not True:
            raise ValueError("Repair replacement requires confirmation")
        async with self.writes:
            self.writable()
            result = await asyncio.to_thread(self.files.repair, data.get("version"), data.get("files"))
        return web.json_response(result)
