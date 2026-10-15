"""Authenticated HTTP/SSE entrance; conversation ownership stays in the host."""

import asyncio
import ipaddress
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from aiohttp import web
from PIL import UnidentifiedImageError

from ...kernel.runtime import Manifest
from .auth import Access
from .settings import WebSettings

COOKIE = "personal_agent_session"


class WebPlugin:
    manifest = Manifest("web", ("conversation",))

    def __init__(self, host):
        self.host = host
        self.settings = WebSettings.parse(host.config.web)
        self.runner = None
        self.streams = set()
        self.upload_lock = asyncio.Lock()

    async def start(self, context):
        if not self.settings.enable:
            return
        self.conversation = self.host.conversation
        self.db = self.conversation.store.db
        self.uploads = self.host.config.state_dir / "web-uploads"
        self.uploads.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.auth = Access(
            self.settings.credential_path(self.host.config.state_dir),
            self.db,
            approved_short_password_sha256=self.settings.approved_short_password_sha256,
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
                web.get("/api/tasks", self.task_list),
                web.post("/api/tasks", self.task_action),
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
        store = self.conversation.store
        before = int(request.query["before"]) if request.query.get("before") else None
        messages = store.history(self.conversation.session, before)
        return web.json_response(
            {
                "messages": messages,
                "cursor": store.cursor(),
                "state": self.conversation.state(),
                "before": messages[0]["ordinal"] if len(messages) == 60 else None,
            }
        )

    async def send(self, request):
        data = await self.object_body(request)
        result = await self.conversation.submit(
            data["request_id"], data.get("text", ""), data.get("attachments", [])
        )
        return web.json_response(result, status=202)

    async def control(self, request):
        data = await self.object_body(request)
        return web.json_response(await self.conversation.control(data["action"]))

    async def task_list(self, request):
        from ...kernel.contracts import TaskRequest

        if "tasks" not in self.host.runtime.plugins:
            return web.json_response({"tasks": [], "runs": []})
        return web.json_response(
            await self.host.runtime.invoke(TaskRequest("list", {"query": request.query.get("query", "")}))
        )

    async def task_action(self, request):
        from ...kernel.contracts import TaskRequest

        d = await self.object_body(request)
        return web.json_response(
            await self.host.runtime.invoke(TaskRequest(d["action"], d.get("arguments", {})), timeout=90)
        )

    async def task_artifact(self, request):
        import re

        name, ident = request.match_info["name"], request.match_info["run_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", ident) or Path(name).name != name or name in {".", ".."}:
            raise web.HTTPNotFound()
        plugin = self.host.runtime.plugins["tasks"]
        run = plugin.repo.run(ident)
        entries = self.host.workfiles.artifacts(plugin.owner(run))
        if name not in {entry["name"] for entry in entries}:
            raise web.HTTPNotFound()
        path = self.host.workfiles.area(plugin.owner(run)) / "artifacts" / name
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
                events = self.conversation.store.changes(self.conversation.session, cursor)
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

            media = await self.conversation.media.add_image(data, field.filename)
            ident = media["id"]
            return web.json_response(
                {"id": ident, "name": media["name"], "url": f"/api/uploads/{ident}"}, status=201
            )

    async def attachment(self, request):
        ident = request.match_info["id"]
        if len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
            raise web.HTTPNotFound()
        row = self.db.execute("SELECT media_type FROM chat_uploads WHERE id=?", (ident,)).fetchone()
        if not row or row[0] not in {"image/jpeg", "video/mp4"} or not (self.uploads / ident).is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(self.uploads / ident, headers={"Content-Type": row[0]})

    async def cleanup_uploads(self):
        while True:
            async with self.upload_lock:
                for (ident,) in self.db.execute(
                    "SELECT id FROM chat_uploads WHERE attached=0 AND created<?", (time.time() - 86400,)
                ).fetchall():
                    (self.uploads / ident).unlink(missing_ok=True)
                    with self.db:
                        self.db.execute("DELETE FROM chat_uploads WHERE id=? AND attached=0", (ident,))
            await asyncio.sleep(3600)

    async def close(self):
        for response in list(self.streams):
            response.force_close()
        if self.runner:
            await self.runner.cleanup()
