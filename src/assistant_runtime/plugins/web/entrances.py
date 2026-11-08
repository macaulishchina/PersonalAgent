"""Two ways into one bound conversation; approval is a trusted capability only."""

import re
import time
from aiohttp import web
from assistant_runtime.api.conversation import ConversationAccessRequest
from assistant_runtime.api.ai_usage import current_usage
from .access import AccessRequests, REQUEST_COOKIE, VISIT_COOKIE, REQUEST_SECONDS, CLAIM_SECONDS
from .links import IDENT

OWNER_COOKIE = "personal_agent_session"


class Entrances:
    def __init__(self, plugin):
        self.plugin = plugin
        self.repo = plugin.api.repository(AccessRequests, plugin.auth.digest)
        self.views = plugin.api.resource("conversation_views")

    def refresh(self):
        self.repo.credential = self.plugin.auth.digest

    def routes(self, common):
        p = self.plugin
        prefix = "/e/{entry:" + IDENT + "}"
        routes = [
            web.get(prefix + "/", p.index),
            web.get(prefix + "/api/access", self.status),
            web.post(prefix + "/api/request", self.start),
            web.post(prefix + "/api/cancel", self.cancel),
            web.post(prefix + "/api/claim", self.claim),
            web.post(prefix + "/api/account", self.account),
        ]
        for mode in ("visit", "account"):
            base = prefix + "/" + mode
            routes.append(web.get(base + "/", p.index))
            routes.extend(
                web.route(method, base + path, handler)
                for method, path, handler in common
                if path not in {"/api/link", "/api/logout"}
            )
            routes.append(web.post(base + "/api/logout", p.logout if mode == "account" else self.logout))
        # A signed-in owner retains the normal Web features and login lifetime.
        base = prefix + "/account"
        routes.extend(
            [
                web.post(base + "/api/control", p.control),
                web.get(base + "/api/models", p.models),
                web.get(base + "/api/model-avatar/builtin/{name}", p.model_avatar),
                web.get(base + "/api/model-avatar/{asset_id}", p.model_avatar),
                web.get(base + "/api/notifications", p.notification_list),
                web.post(base + "/api/notifications", p.notification_action),
                web.get(base + "/api/tasks", p.task_list),
                web.post(base + "/api/tasks", p.task_action),
                web.get(base + "/api/tasks/{run_id}/trace", p.task_trace),
                web.get(base + "/api/tasks/{run_id}/artifacts/{name}", p.task_artifact),
            ]
        )
        return routes

    def owner(self, request):
        return self.plugin.auth.session_info(request.cookies.get(OWNER_COOKIE))

    def resolve(self, request):
        self.refresh()
        ident = request.match_info["entry"]
        if request.path.startswith("/e/" + ident + "/account/"):
            owner = self.owner(request)
            if not owner:
                raise PermissionError("请登录进入")
            return self.repo.resolve(ident, "account", owner["id"], hashed=True)
        return self.repo.resolve(ident, "temporary", request.cookies.get(VISIT_COOKIE))

    async def authorize(self, request):
        self.refresh()
        ident = request.match_info["entry"]
        # The landing/status/claim routes do not expose conversation data.
        if not re.search(r"/(?:account|visit)/api/", request.path):
            return
        access = self.resolve(request)
        if (
            request.method not in {"GET", "HEAD"}
            and access["permission"] == "read"
            and not request.path.endswith("/api/logout")
        ):
            raise web.HTTPForbidden(text="此授权仅允许查看对话")
        request["conversation_view"] = await self.views.open(access["session_id"])
        request["conversation_entry"] = access
        request["chat_mount"] = "/chat/e/" + ident + ("/account" if access["kind"] == "account" else "/visit")
        if access["kind"] != "account":
            request["conversation_grant"] = access

    def valid(self, request):
        try:
            self.resolve(request)
            return True
        except PermissionError:
            return False

    async def body(self, request):
        data = await self.plugin.object_body(request)
        if data:
            raise ValueError("此操作不接受浏览器指定身份、会话或时长")

    @staticmethod
    def visible(item):
        if not item:
            return None
        return {k: item[k] for k in ("id", "code", "state", "created", "deadline", "permission")}

    async def status(self, request):
        entry = self.repo.entry(request.match_info["entry"])
        item = self.repo.lookup(request.match_info["entry"], request.cookies.get(REQUEST_COOKIE))
        result = {
            "title": entry["title"],
            "account_signed_in": bool(self.owner(request)),
            "request": self.visible(item),
        }
        if item and item["state"] == "consumed":
            try:
                access = self.repo.resolve(
                    request.match_info["entry"], "temporary", request.cookies.get(VISIT_COOKIE)
                )
                if access["request_id"] == item["id"]:
                    result["visit_path"] = "/chat/e/" + request.match_info["entry"] + "/visit/"
            except PermissionError:
                pass
        return web.json_response(result)

    async def start(self, request):
        await self.body(request)
        ident = request.match_info["entry"]
        previous = self.repo.lookup(ident, request.cookies.get(REQUEST_COOKIE))
        claim, item = self.repo.start(ident, request.cookies.get(REQUEST_COOKIE), request.remote or "unknown")
        if not previous or previous["id"] != item["id"]:
            self.context.publish(
                "web.access_requested",
                endpoint_id=self.repo.entry(ident)["endpoint_id"],
                request_id=item["id"],
            )
        response = web.json_response({"request": self.visible(item)}, status=202)
        response.set_cookie(
            REQUEST_COOKIE,
            claim,
            httponly=True,
            samesite="Strict",
            secure=request.secure,
            path="/chat/e/" + ident + "/",
            max_age=REQUEST_SECONDS + CLAIM_SECONDS,
        )
        return response

    async def cancel(self, request):
        await self.body(request)
        self.repo.cancel(request.match_info["entry"], request.cookies.get(REQUEST_COOKIE))
        return web.json_response({"cancelled": True})

    async def claim(self, request):
        await self.body(request)
        ident = request.match_info["entry"]
        token, expires = self.repo.claim(
            ident, request.cookies.get(REQUEST_COOKIE), request.cookies.get(VISIT_COOKIE)
        )
        response = web.json_response({"path": "/chat/e/" + ident + "/visit/", "expires": expires})
        if token:
            response.set_cookie(
                VISIT_COOKIE,
                token,
                httponly=True,
                samesite="Strict",
                secure=request.secure,
                path="/chat/e/" + ident + "/",
                max_age=max(1, int(expires - time.time())),
            )
        return response

    async def account(self, request):
        await self.body(request)
        owner = self.owner(request)
        if not owner:
            raise web.HTTPUnauthorized(text="请使用 Web 访问口令登录")
        self.repo.account(request.match_info["entry"], owner)
        return web.json_response({"path": "/chat/e/" + request.match_info["entry"] + "/account/"})

    async def logout(self, request):
        self.repo.close(self.resolve(request)["id"])
        response = web.json_response({"ok": True})
        response.del_cookie(VISIT_COOKIE, path="/chat/e/" + request.match_info["entry"] + "/")
        return response

    async def device_request(self, request):
        self.refresh()
        if (
            current_usage.get() is not None
            or not self.plugin.settings.enable
            or not isinstance(request, ConversationAccessRequest)
            or not isinstance(request.endpoint_id, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]*:[A-Za-z0-9_-]{1,96}", request.endpoint_id)
            or len(request.endpoint_id) > 128
            or not isinstance(request.arguments, dict)
        ):
            raise PermissionError("Invalid access approver")
        args = request.arguments
        if request.action == "pending" and not args:
            return {"requests": self.repo.pending(request.endpoint_id)}
        if request.action == "decide" and set(args) <= {"request_id", "decision", "seconds"}:
            return self.repo.decide(
                request.endpoint_id, args.get("request_id"), args.get("decision"), args.get("seconds")
            )
        raise ValueError("Invalid access approval operation")

    async def manage(self, action, args):
        self.refresh()
        if action == "entrances" and not args:
            return {"entrances": self.repo.entries()}
        if action == "create_entrance" and set(args) <= {"session_id", "endpoint_id", "permission", "title"}:
            view = await self.views.open(args.get("session_id"))
            return self.repo.create(
                args["session_id"],
                args.get("title", view.title),
                args.get("endpoint_id"),
                args.get("permission", "chat"),
            )
        if action == "revoke_entrance" and set(args) == {"id"}:
            self.repo.revoke(args["id"])
            return {"closed": True}
        if action == "accesses" and not args:
            accounts = self.plugin.auth.sessions()
            items = self.repo.accesses(accounts) + self.plugin.scoped.links.accesses()
            items += [
                {
                    **a,
                    "kind": "account_login",
                    "permission": "chat",
                    "title": "Web 账号登录（全部网页会话）",
                    "session_id": None,
                }
                for a in accounts
            ]
            return {"accesses": items}
        if action == "close_access" and set(args) == {"id", "kind"}:
            kind = args["kind"]
            if kind in {"temporary", "account"}:
                self.repo.close(args["id"])
            elif kind == "account_login":
                self.plugin.auth.close_session(args["id"])
            elif kind == "link":
                self.plugin.scoped.links.close_access(args["id"])
            else:
                raise ValueError("Invalid access kind")
            return {"closed": True}
        raise ValueError("Unsupported access management action")
