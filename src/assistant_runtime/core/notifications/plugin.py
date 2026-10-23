"""Notification ownership, producer attribution and durable channel dispatch."""

from .declaration import DECLARATION

import asyncio
import math
from urllib.parse import urlsplit
from assistant_runtime.api.contracts import NotificationRequest, NotificationLink, TaskRequest
from assistant_runtime.core.notifications.settings import NotificationSettings
from assistant_runtime.core.notifications.repository import Notifications
from assistant_runtime.core.notifications.groups import association
from assistant_runtime.core.notifications import policy
from assistant_runtime.core.notifications.channels import ChatChannel, ContextChannel, VoiceChannel, Deferred


class NotificationsPlugin:
    manifest = DECLARATION.manifest

    def __init__(self, host, *, voice_host=None):
        self.host = host
        self.settings = NotificationSettings.parse(host.config.notifications)
        self.repo = Notifications(host.store.db, self.settings)
        self.channels = {}
        self.context = None
        host.notifications = self
        from .channels import InboxChannel

        self.register_channel("inbox", InboxChannel())
        if getattr(host, "conversation", None) is not None:
            self.register_channel("chat", ChatChannel(host))
            self.register_channel("context", ContextChannel(host))
        if self.settings.voice_enabled:
            self.register_channel("voice", VoiceChannel(voice_host or host, self.settings))

    def register_channel(self, name, adapter):
        if self.context or name in self.channels or not name.isidentifier():
            raise ValueError("Register a unique notification channel before startup")
        self.channels[name] = adapter

    def project_chat(self, messages):
        self.repo.groups.project_chat(messages)

    async def start(self, context):
        if set(self.settings.channels) - self.channels.keys():
            raise ValueError("Configured notification channel is not registered")
        self.context = context
        context.register(NotificationRequest, self.request)
        context.register(NotificationLink, self.link)
        for name, channel in self.channels.items():
            self.repo.recover(name, channel.replay_safe)
            context.spawn(self.worker(name, channel), "delivery:" + name)

    def source(self, caller):
        from assistant_runtime.core.tasks.access import notification_origin

        return notification_origin(self.repo.db, caller)

    def validate(self, args):
        allowed = {
            "event_key",
            "title",
            "body",
            "url",
            "priority",
            "voice",
            "expires_at",
            "id",
            "revision",
            "group_key",
            "group_title",
            "kind",
            "report_with_task",
        }
        if set(args) - allowed:
            raise ValueError("Unknown notification fields")
        for key, limit in (("title", 200), ("body", 8000)):
            if not isinstance(args.get(key), str) or not 0 < len(args[key].strip()) <= limit:
                raise ValueError("Invalid notification " + key)
        key = args.get("event_key", "")
        if not args.get("id") and (not isinstance(key, str) or not 0 < len(key) <= 240):
            raise ValueError("Notification requires stable event_key")
        url = args.get("url", "")
        if not isinstance(url, str) or len(url) > 2000:
            raise ValueError("Invalid source URL")
        if url:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("Source URL must be HTTP(S) without credentials")
        expires = args.get("expires_at")
        if expires is not None and (type(expires) not in (int, float) or not math.isfinite(expires)):
            raise ValueError("expires_at must be a Unix timestamp")
        if (
            args.get("priority", "normal") not in {"normal", "important"}
            or type(args.get("voice", False)) is not bool
        ):
            raise ValueError("Invalid notification priority/voice")
        if args.get("voice") and "voice" not in self.channels:
            raise ValueError("Voice notifications disabled")
        kind = args.get("kind", "attention")
        if not isinstance(kind, str) or kind not in policy.KINDS:
            raise ValueError("Notification kind must be progress, result or attention")
        if kind == "progress" and (args.get("voice") or args.get("priority") == "important"):
            raise ValueError("Progress is silent; use attention for an important or spoken update")
        covered = args.get("report_with_task", False)
        if type(covered) is not bool or (
            covered and (kind != "result" or args.get("voice") or args.get("priority") == "important")
        ):
            raise ValueError("report_with_task requires a normal, unspoken result")
        self.group_fields(args)
        return {
            **({"report_with_task": True} if covered else {}),
            **{
                key: args.get(key, default)
                for key, default in (
                    ("kind", "attention"),
                    ("title", ""),
                    ("body", ""),
                    ("url", ""),
                    ("priority", "normal"),
                    ("voice", False),
                    ("expires_at", None),
                )
            },
        }

    @staticmethod
    def group_fields(args):
        for key, limit in (("group_key", 200), ("group_title", 200)):
            value = args.get(key)
            if value is not None and (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > limit
                or any(ord(c) < 32 for c in value)
            ):
                raise ValueError("Invalid notification " + key)
        if args.get("group_title") and not args.get("group_key"):
            raise ValueError("Group title requires a group key")
        return {"key": args["group_key"], "title": args.get("group_title")} if args.get("group_key") else None

    async def link(self, request):
        # This contract is internal only. The owning producer supplies durable evidence,
        # not a title match or an AI-selected target task.
        source, origin = self.source(request.caller_run) if request.caller_run else (request.source, {})
        group = self.group_fields({"group_key": request.group_key, "group_title": request.group_title})
        payload = {"title": request.group_title or request.event_key, "origin": origin}
        if group:
            payload["group"] = group
        ident, title = association(source, request.event_key, payload)
        return self.repo.groups.link(
            request.source,
            request.event_key,
            ident,
            title if request.caller_run or request.group_title else None,
        )

    async def describe(self, items):
        # Execution facts remain owned by tasks. Resolve them at read time so an
        # event published before completion gains final timing without republishing,
        # changing its content revision/read state or replaying any delivery.
        origins = [item["payload"].get("origin", {}) for item in items]
        ids = list({origin["run_id"] for origin in origins if origin.get("task_id") and origin.get("run_id")})
        info = {}
        if ids:
            try:
                info = await self.host.runtime.invoke(
                    TaskRequest("execution_info", {"run_ids": ids}), timeout=1
                )
            except (ValueError, RuntimeError, AttributeError, TimeoutError):
                pass
        for item, origin in zip(items, origins):
            task_id, run_id = origin.get("task_id"), origin.get("run_id")
            if not task_id or not run_id or item["source"] != "task:" + task_id:
                continue
            run = info.get(run_id)
            if run and run["task_id"] != task_id:
                run = None
            item["execution"] = {
                "kind": policy.kind(item["payload"]),
                "state": run["state"] if run else None,
                "duration_seconds": run["duration_seconds"] if run else origin.get("duration_seconds"),
            }
        return items

    async def request(self, request):
        action, args = request.action, request.arguments
        if not isinstance(args, dict):
            raise ValueError("Notification arguments must be an object")
        if request.caller_run and action not in {"publish", "update", "get", "list", "groups", "group"}:
            raise ValueError("Background tasks cannot mark user notifications read or retry delivery")
        if action in {"publish", "update"}:
            payload = self.validate(args)
            source, origin = self.source(request.caller_run)
            if action == "update" and not args.get("id"):
                raise ValueError("Update requires notification id")
            # Run identity is server-derived metadata, not part of event deduplication.
            payload["origin"] = origin
            group = self.group_fields(args)
            if group:
                payload["group"] = group
            old = None
            if args.get("id"):
                old = self.repo.get(args["id"])
            else:
                row = self.repo.db.execute(
                    "SELECT id FROM notifications WHERE source=? AND event_key=?", (source, args["event_key"])
                ).fetchone()
                if row:
                    old = self.repo.get(row[0])
            if old and args.get("id") and not request.caller_run:
                source = old["source"]
            if payload.get("report_with_task"):
                if old and old["payload"].get("report_with_task"):
                    payload["kind"] = old["payload"]["kind"]
                elif request.caller_run and origin.get("reports_completion"):
                    payload["kind"] = "progress"

            if old:
                if "report_with_task" not in args and old["payload"].get("report_with_task"):
                    payload["report_with_task"] = True
                if action == "publish" and not args.get("id") and "kind" not in old["payload"]:
                    # A pre-type publication may have committed just before an upgrade.
                    # Retrying that event preserves its receipt; purpose changes require update.
                    payload.pop("kind", None)
                    payload.pop("report_with_task", None)
                if "kind" not in args:
                    if "kind" in old["payload"]:
                        payload["kind"] = old["payload"]["kind"]
                    else:
                        payload.pop("kind", None)
                if policy.kind(payload) == "progress" and policy.kind(old["payload"]) != "progress":
                    raise ValueError("A delivered result/attention cannot be demoted to silent progress")
                if policy.kind(payload) == "progress" and (
                    payload["voice"] or payload["priority"] == "important"
                ):
                    raise ValueError("Progress is silent; use attention for an important or spoken update")
                if action == "update" and group and group != old["payload"].get("group"):
                    raise ValueError("Notification association cannot be changed")
                if not group and old["payload"].get("group"):
                    payload["group"] = old["payload"]["group"]
                payload["origin"] = old["payload"].get("origin", {})
            channels = policy.channels(payload, self.settings.channels)
            return self.repo.publish(
                source,
                args.get("event_key", ""),
                payload,
                channels,
                ident=args.get("id"),
                revision=args.get("revision"),
            )
        if action == "get":
            return (await self.describe([self.repo.get(args["id"])]))[0]
        if action == "list":
            listing = self.repo.list(
                unread=args.get("unread", False), before=args.get("before"), limit=args.get("limit", 50)
            )
            listing["items"] = await self.describe(listing["items"])
            return listing
        if action == "groups":
            listing = self.repo.groups.list(
                args.get("unread", False), args.get("before"), args.get("limit", 20)
            )
            await self.describe([group["latest"] for group in listing["items"]])
            return listing
        if action == "group":
            listing = self.repo.groups.members(
                args.get("group_id"), args.get("before"), args.get("limit", 20), args.get("snapshot")
            )
            listing["items"] = await self.describe(listing["items"])
            return listing
        if action == "read_group":
            return self.repo.groups.read(args.get("group_id"), args.get("before"))
        if action == "read_all":
            return self.repo.read_all(args.get("before"))
        if action == "read":
            return self.repo.mark_read(args["id"], args["revision"])
        if action == "retry":
            if args.get("channel") not in self.channels:
                raise ValueError("Unknown notification channel")
            return self.repo.retry(args["id"], args["revision"], args["channel"])
        raise ValueError("Unknown notification action")

    async def deliver_once(self, name, channel):
        item = self.repo.claim(name)
        if not item:
            return False
        try:
            await asyncio.wait_for(channel.deliver(item), self.settings.delivery_timeout)
        except Deferred as exc:
            self.repo.finish(item, name, "pending", str(exc), delay=5, deferred=True)
        except asyncio.CancelledError:
            # Keep inflight; restart recovery knows whether replay is safe.
            raise
        except Exception as exc:
            attempt = next(d["attempts"] for d in item["deliveries"] if d["channel"] == name)
            state = (
                "unknown"
                if not channel.replay_safe
                else "failed"
                if attempt >= self.settings.max_attempts
                else "pending"
            )
            self.repo.finish(
                item,
                name,
                state,
                str(exc)[:300],
                delay=min(3600, self.settings.retry_seconds * 2 ** (attempt - 1)),
            )
        else:
            self.repo.finish(item, name, "delivered")
        return True

    async def worker(self, name, channel):
        while True:
            delivered = await self.deliver_once(name, channel)
            await asyncio.sleep(0.05 if delivered else 0.5)

    def diagnostics(self):
        return {"channels": list(self.channels), "unread": self.repo.unread_count()}
