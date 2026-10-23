"""Authenticated HTTP/SSE entrance; conversation ownership stays in the host."""

from .declaration import DECLARATION

import asyncio
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from PIL import UnidentifiedImageError

from .auth import AuthRepository

COOKIE = "personal_agent_session"


class WebPlugin:
    manifest = DECLARATION.manifest

    def __init__(self, api):
        self.api = api
        self.settings = api.config
        self.runner = None
        self.streams = set()
        self.upload_lock = asyncio.Lock()

    async def start(self, context):
        if not self.settings.enable:
            return
        self.conversation = self.api.resource("conversation")
        self.auth = self.api.repository(
            AuthRepository,
            self.settings.credential_path(self.api.state_dir.parent),
            self.settings.approved_short_password_sha256,
        )
        static = Path(__file__).with_name("static")
        if not (static / "index.html").exists():
            raise RuntimeError("Build the web interface with npm --prefix frontend run build")
        self.app = web.Application(client_max_size=11 * 1024 * 1024, middlewares=[self.guard])
        self.app.add_routes(
            [
                web.post("/api/login", self.login),
                web.post("/api/logout", self.logout),
                web.get("/api/session", self.session),
                web.get("/api/messages", self.history),
                web.post("/api/messages", self.send),
                web.get("/api/events", self.events),
                web.post("/api/control", self.control),
                web.get("/api/notifications", self.notification_list),
                web.post("/api/notifications", self.notification_action),
                web.get("/api/tasks", self.task_list),
                web.post("/api/tasks", self.task_action),
                web.get("/api/tasks/{run_id}/trace", self.task_trace),
                web.get("/api/tasks/{run_id}/artifacts/{name}", self.task_artifact),
                web.post("/api/uploads", self.upload),
                web.get("/api/uploads/{id}", self.attachment),
                web.get("/", self.index),
            ]
        )
        self.app.router.add_static("/assets/", static / "assets", show_index=False)
        self.runner = web.AppRunner(self.app, access_log=None, shutdown_timeout=2)
        context.own(self.close)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.settings.host, self.settings.port).start()
        context.spawn(self.cleanup_uploads(), "upload-retention")

    @web.middleware
    async def guard(self, request, handler):
        try:
            # Numeric hosts prevent arbitrary domain DNS-rebinding. localhost is for tunnels.
            hostname = urlsplit("//" + request.host).hostname
            if hostname != "localhost":
                ipaddress.ip_address(hostname or "")
            origin = request.headers.get("Origin")
            expected = f"{request.scheme}://{request.host}"
            if origin and origin != expected:
                raise web.HTTPForbidden(text="跨站请求已拒绝")
            if request.method not in ("GET", "HEAD") and origin != expected:
                raise web.HTTPForbidden(text="缺少同源请求标识")
            if request.path.startswith("/api/") and request.path != "/api/login":
                if not self.auth.valid(request.cookies.get(COOKIE)):
                    raise web.HTTPUnauthorized(text="请先输入访问口令")
            response = await handler(request)
        except web.HTTPException:
            raise
        except (ValueError, TypeError, KeyError, UnidentifiedImageError) as exc:
            response = web.json_response({"error": str(exc)[:180]}, status=400)
        except RuntimeError as exc:
            response = web.json_response({"error": str(exc)[:180]}, status=503)
        except PermissionError as exc:
            response = web.json_response({"error": str(exc)}, status=401)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob: data:; media-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    async def index(self, request):
        return web.FileResponse(Path(__file__).with_name("static") / "index.html")

    async def object_body(self, request):
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("请求必须是 JSON 对象")
        return data

    async def login(self, request):
        data = await self.object_body(request)
        token = self.auth.login(data.get("token"), request.remote or "unknown")
        response = web.json_response({"ok": True})
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="Strict",
            secure=request.secure,
            max_age=7 * 86400,
            path="/",
        )
        return response

    async def logout(self, request):
        self.auth.logout(request.cookies.get(COOKIE, ""))
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE, path="/")
        return response

    async def session(self, request):
        return web.json_response(self.conversation.state())

    async def history(self, request):
        before = int(request.query["before"]) if request.query.get("before") else None
        return web.json_response(self.conversation.history(before))

    async def send(self, request):
        data = await self.object_body(request)
        result = await self.conversation.submit(
            data["request_id"], data.get("text", ""), data.get("attachments", []), data.get("notification_id")
        )
        return web.json_response(result, status=202)

    async def control(self, request):
        data = await self.object_body(request)
        return web.json_response(await self.conversation.control(data["action"]))

    async def notification_list(self, request):
        from assistant_runtime.api.contracts import NotificationRequest

        if not self.api.resource("notifications_enabled"):
            return web.json_response({"items": [], "unread": 0, "before": None})
        return web.json_response(
            await self.api.invoke(
                NotificationRequest(
                    "groups" if request.query.get("grouped") == "true" else "list",
                    {
                        "unread": request.query.get("unread") == "true",
                        "before": request.query.get("before"),
                    },
                )
            )
        )

    async def notification_action(self, request):
        from assistant_runtime.api.contracts import NotificationRequest

        data = await self.object_body(request)
        if data.get("action") not in {"read", "read_all", "read_group", "group", "retry", "get"}:
            raise ValueError("Unsupported inbox action")
        return web.json_response(
            await self.api.invoke(NotificationRequest(data["action"], data.get("arguments", {})))
        )

    async def task_list(self, request):
        from assistant_runtime.api.contracts import TaskRequest

        if not self.api.resource("tasks_enabled"):
            return web.json_response({"tasks": [], "runs": [], "before": None})
        args = {
            key: request.query[key] for key in ("query", "view", "before", "task_id") if key in request.query
        }
        for key in ("include_archived", "active_only"):
            if key in request.query:
                if request.query[key] not in {"true", "false"}:
                    raise ValueError("Task filters must be true or false")
                args[key] = request.query[key] == "true"
        if "known" in request.query:
            args["known"] = json.loads(request.query["known"])
        if "limit" in request.query:
            args["limit"] = int(request.query["limit"])
        action = "get" if "task_id" in args else "list"
        return web.json_response(await self.api.invoke(TaskRequest(action, args)))

    async def task_trace(self, request):
        from assistant_runtime.api.contracts import TaskRequest

        if set(request.query) - {"after", "limit"}:
            raise ValueError("Unknown execution trace query")
        result = await self.api.invoke(
            TaskRequest(
                "trace",
                {
                    "run_id": request.match_info["run_id"],
                    "after": int(request.query.get("after", "0")),
                    "limit": int(request.query.get("limit", "100")),
                },
            )
        )
        # The timeline needs activity metadata only, not execution internals or prompts.
        return web.json_response(
            {
                key: result[key]
                for key in ("run_id", "task_id", "state", "items", "next", "truncated", "outcome_scope")
            }
        )

    async def task_action(self, request):
        from assistant_runtime.api.contracts import TaskRequest

        d = await self.object_body(request)
        return web.json_response(
            await self.api.invoke(TaskRequest(d["action"], d.get("arguments", {})), timeout=90)
        )

    async def task_artifact(self, request):
        import re
        from assistant_runtime.api.contracts import TaskRequest

        name, ident = request.match_info["name"], request.match_info["run_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", ident) or Path(name).name != name or name in {".", ".."}:
            raise web.HTTPNotFound()
        try:
            result = await self.api.invoke(TaskRequest("artifact_path", {"run_id": ident, "name": name}))
        except (ValueError, KeyError, FileNotFoundError):
            raise web.HTTPNotFound() from None
        path = Path(result["path"])
        response = web.FileResponse(path)
        response.headers["Content-Type"] = "application/octet-stream"
        response.headers["Content-Disposition"] = "attachment"
        return response

    async def events(self, request):
        if len(self.streams) >= 12:
            raise web.HTTPServiceUnavailable(text="连接数量已达上限")
        cursor = int(request.headers.get("Last-Event-ID") or request.query.get("after", "0"))
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)
        self.streams.add(response)
        try:
            while self.auth.valid(request.cookies.get(COOKIE)):
                events = self.conversation.changes(cursor)
                if events is None:
                    await response.write(b"event: reset\ndata: {}\n\n")
                    break
                payload = ""
                for event in events:
                    cursor = event["seq"]
                    payload += (
                        f"id: {cursor}\nevent: update\ndata: "
                        + json.dumps(event, ensure_ascii=False)
                        + "\n\n"
                    )
                if not payload:
                    payload = ": heartbeat\n\n"
                await asyncio.wait_for(response.write(payload.encode()), 3)
                await asyncio.sleep(0.15 if events else 0.5)
        except (ConnectionError, TimeoutError, RuntimeError):
            pass
        finally:
            self.streams.discard(response)
        return response

    async def upload(self, request):
        async with self.upload_lock:
            reader = await request.multipart()
            field = await reader.next()
            if not field or field.name != "image":
                raise ValueError("请选择图片")
            data = bytearray()
            while chunk := await field.read_chunk():
                data.extend(chunk)
                if len(data) > 10 * 1024 * 1024:
                    raise ValueError("单张图片不能超过 10 MB")

            media = await self.conversation.upload(data, field.filename)
            ident = media["id"]
            return web.json_response(
                {"id": ident, "name": media["name"], "url": f"/api/uploads/{ident}"}, status=201
            )

    async def attachment(self, request):
        ident = request.match_info["id"]
        if len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
            raise web.HTTPNotFound()
        try:
            path, media_type = self.conversation.attachment(ident)
        except FileNotFoundError:
            raise web.HTTPNotFound() from None
        return web.FileResponse(path, headers={"Content-Type": media_type})

    async def cleanup_uploads(self):
        while True:
            async with self.upload_lock:
                self.conversation.expire_uploads()
            await asyncio.sleep(3600)

    async def close(self):
        for response in list(self.streams):
            response.force_close()
        if self.runner:
            await self.runner.cleanup()
