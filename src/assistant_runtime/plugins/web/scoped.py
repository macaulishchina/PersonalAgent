"""Conversation-scoped browser entrance using the ordinary Web presentation."""

import re
from aiohttp import web
from assistant_runtime.api.conversation import ConversationRequest
from .links import COOKIE, IDENT, Links


class ScopedWeb:
    def __init__(self, plugin):
        self.plugin = plugin
        self.links = plugin.api.repository(Links, plugin.auth.digest)
        self.views = plugin.api.resource("conversation_views")
        from .entrances import Entrances

        self.entrances = Entrances(plugin)

    def routes(self):
        p = self.plugin
        prefix = "/c/{link:" + IDENT + "}"
        common = [
            ("GET", "/api/session", p.session),
            ("GET", "/api/messages", p.history),
            ("POST", "/api/messages", p.send),
            ("GET", "/api/events", p.events),
            ("POST", "/api/conversation", p.conversation_action),
            ("POST", "/api/uploads", p.upload),
            ("GET", "/api/uploads/{id}", p.attachment),
            ("GET", "/api/results/{id}", p.read_result),
            ("POST", "/api/logout", self.logout),
            ("POST", "/api/link", self.exchange),
        ]
        return [
            web.get(prefix + "/", p.index),
            *[web.route(method, prefix + path, handler) for method, path, handler in common],
            *self.entrances.routes(common),
        ]

    async def authorize(self, request):
        self.links.credential = self.plugin.auth.digest
        if request.match_info.get("entry"):
            await self.entrances.authorize(request)
            return
        ident = request.match_info.get("link")
        if not ident:
            return
        # A shell may load without credentials; no data is embedded in its HTML.
        if request.path.endswith("/c/" + ident + "/"):
            return
        if request.path.endswith("/api/link") and request.method == "POST":
            return
        grant = self.links.resolve(request.cookies.get(COOKIE), ident)
        if (
            request.method not in {"GET", "HEAD"}
            and grant["permission"] == "read"
            and not request.path.endswith("/api/logout")
        ):
            raise web.HTTPForbidden(text="此快捷链接仅允许查看对话")
        request["conversation_view"] = await self.views.open(grant["session_id"])
        request["conversation_grant"] = grant

    def valid(self, request):
        if request.match_info.get("entry"):
            return self.entrances.valid(request)
        ident = request.match_info.get("link")
        if ident:
            try:
                self.links.credential = self.plugin.auth.digest
                self.links.resolve(request.cookies.get(COOKIE), ident)
                return True
            except PermissionError:
                return False
        return self.plugin.auth.valid(request.cookies.get("personal_agent_session"))

    @staticmethod
    def state(request, state):
        entry = request.get("conversation_entry")
        if entry:
            state = {
                **state,
                "conversation_title": entry["title"],
                "conversation_access": entry["permission"],
                "conversation_auth": entry["kind"],
                "conversation_expires": entry["expires"],
            }
            if entry["kind"] == "account":
                return state
        grant = request.get("conversation_grant")
        if not grant:
            return state
        return {
            **state,
            "conversation_title": grant["title"],
            "conversation_access": grant["permission"],
            "notifications_unread": 0,
            # This entrance does not control the machine's fixed voice route.
            "voice": "disabled",
            "speaking": False,
        }

    async def exchange(self, request):
        data = await self.plugin.object_body(request)
        if set(data) != {"token"}:
            raise ValueError("Invalid conversation link exchange")
        self.links.credential = self.plugin.auth.digest
        ident = request.match_info["link"]
        browser, ident = self.links.exchange(
            data["token"], request.cookies.get(COOKIE), request.remote or "unknown", ident
        )
        response = web.json_response({"path": "/chat/c/" + ident + "/"})
        response.set_cookie(
            COOKIE,
            browser,
            httponly=True,
            samesite="Strict",
            secure=request.secure,
            max_age=7 * 86400,
            path="/chat/c/" + ident + "/",
        )
        return response

    async def logout(self, request):
        self.links.logout(request.cookies.get(COOKIE), request.match_info["link"])
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE, path="/chat/c/" + request.match_info["link"] + "/")
        return response

    async def manage(self, action, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("Invalid conversation link operation")
        self.links.credential = self.plugin.auth.digest
        if action in {"entrances", "create_entrance", "revoke_entrance", "accesses", "close_access"}:
            return await self.entrances.manage(action, arguments)
        if action == "sessions" and set(arguments) <= {"before"}:
            return await self.plugin.api.invoke(ConversationRequest("list", arguments))
        if action == "create_session" and set(arguments) == {"title"}:
            return await self.plugin.api.invoke(ConversationRequest("create", arguments))
        if action == "links" and not arguments:
            return {"links": self.links.list()}
        if (
            action == "create_link"
            and set(arguments) <= {"session_id", "permission", "expires_in"}
            and "session_id" in arguments
        ):
            ident = arguments["session_id"]
            if not isinstance(ident, str) or not re.fullmatch(IDENT, ident):
                raise ValueError("Invalid conversation identity")
            # Validate existence without changing a default binding or starting a turn.
            await self.plugin.api.invoke(ConversationRequest("state", {"session_id": ident}))
            sessions = await self.views.open(ident)
            return self.links.create(
                ident, sessions.title, arguments.get("permission", "chat"), arguments.get("expires_in", 0)
            )
        if action == "revoke_link" and set(arguments) == {"id"}:
            self.links.revoke(arguments["id"])
            return {"revoked": True}
        raise ValueError("Unsupported conversation link operation")
