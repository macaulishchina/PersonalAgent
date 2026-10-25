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

COOKIE = "personal_agent_admin"
log = logging.getLogger(__name__)


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
        from .accounts import Accounts

        self.accounts = Accounts(business)

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
        except Conflict:
            response = web.json_response({"error": "配置已被其他操作修改，请重新加载"}, status=409)
        except PermissionError:
            response = web.json_response({"error": "登录失败或请求过于频繁"}, status=403)
        except PasswordPolicyError as exc:
            response = web.json_response({"error": str(exc)}, status=400)
        except PlatformCertificationError as exc:
            response = web.json_response({"error": str(exc), "code": "platform_not_certified"}, status=400)
        except ConfigurationError as exc:
            response = web.json_response({"error": str(exc), "issues": [exc.public()]}, status=400)
        except (ValueError, TypeError, KeyError, OSError):
            # Parser and owner exception strings may contain credentials. Do not echo them.
            response = web.json_response(
                {"error": "配置或文件操作失败；请检查字段、依赖及本地配置"}, status=400
            )
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            }
        )
        return response

    async def start(self):
        app = web.Application(middlewares=[self.guard], client_max_size=2 * 1024 * 1024)
        app.add_routes(
            [
                web.get("/", self.index),
                web.get("/console.js", self.javascript),
                web.get("/workspace.js", self.workspace_javascript),
                web.get("/console.css", self.styles),
                web.post("/api/login", self.login),
                web.post("/api/logout", self.logout),
                web.get("/api/admin-password", self.password_policy),
                web.post("/api/admin-password", self.change_password),
                web.get("/api/status", self.status),
                web.get("/api/configuration", self.read),
                web.post("/api/configuration", self.save),
                web.get("/api/catalog", self.describe),
                web.get("/api/resources", self.resources),
                web.get("/api/accounts", self.account_inventory),
                web.post("/api/account", self.account_action),
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
        if self.business.application.get("phase") in {
            "preparing",
            "applying",
            "waiting",
            "cancelling",
            "restarting",
        }:
            raise ValueError("Configuration application is active")

    async def close(self):
        await self.accounts.close()
        if self.runner:
            await self.runner.cleanup()

    async def index(self, request):
        return web.FileResponse(Path(__file__).with_name("static") / "index.html")

    async def javascript(self, request):
        return web.FileResponse(Path(__file__).with_name("static") / "console.js")

    async def workspace_javascript(self, request):
        return web.FileResponse(Path(__file__).with_name("static") / "workspace.js")

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
                return result

        result = await asyncio.to_thread(snapshot_view)
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
        for description in [
            inventory["application"],
            *(p.get("configuration") for p in inventory["plugins"]),
        ]:
            if description and "schema" in description:
                description["schema"] = public_rule(described_schema(description))
        return web.json_response(inventory)

    async def save(self, request):
        data = await request.json()
        async with self.writes:
            self.writable()
            result = await asyncio.to_thread(
                self.files.patch,
                data.get("version"),
                data.get("patches"),
                actor="administrator",
                resource_changes=data.get("resources"),
            )
            if result.get("changed"):
                result["application"] = self.business.update(result["version"])
        return web.json_response(result)

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
