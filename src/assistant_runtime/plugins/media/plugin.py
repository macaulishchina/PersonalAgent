"""Media capability owner. Tasks own plans; providers own auth and protocol IO."""

import asyncio
import time

from ...kernel.contracts import MediaRequest, NotificationRequest, TaskRequest
from ...kernel.runtime import Manifest
from .aliyun import Aliyun
from .errors import MediaError
from .downloads import Downloads
from .openlist import OpenList
from .repository import MediaRepository, TERMINAL
from .search import PanSou, share_id, extraction_code
from .settings import MediaSettings, identifier


class MediaPlugin:
    manifest = Manifest("media", ("notifications",))

    def __init__(self, host, provider=None):
        self.host = host
        self.settings = MediaSettings.parse(host.config.media)
        self.repo = MediaRepository(host.store.db, self.settings)
        self.root = host.config.state_dir / "media"
        self.provider = provider or Aliyun(self.root, self.repo.account_state)
        self.wakeup = asyncio.Event()
        self.accepting = False

    async def start(self, context):
        import aiohttp

        self.context = context
        context.own(self.provider.close)
        self.provider.claim()
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.settings.timeout_seconds + 10),
            connector=aiohttp.TCPConnector(limit=4),
            trust_env=False,
        )
        context.own(self.session.close)
        self.download_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=1),
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
        )
        context.own(self.download_session.close)
        self.search = PanSou(self.session, self.settings.search_url)
        self.openlist = OpenList(
            self.session, self.settings.openlist_url, self.root / "credentials" / "openlist.json"
        )
        self.reporter = context.event_reporter()
        context.own(self.reporter.close)
        self.repo.recover()
        self.accepting = True
        context.own(self.stop_accepting)
        context.register(MediaRequest, self.request)
        context.spawn(self.worker(), "acquisitions")
        self.downloads = Downloads(self)
        context.spawn(self.downloads.worker(), "downloads")
        context.spawn(self.notifications(), "outbox")
        if self.repo.db.execute(
            "SELECT 1 FROM media_operations WHERE state='waiting_auth' LIMIT 1"
        ).fetchone():
            context.spawn(self.restore_authorization(), "auth-recovery")

    async def stop_accepting(self):
        self.accepting = False

    async def authorize(self, caller, target=None):
        if caller is None:
            return "assistant"
        info = await self.host.runtime.invoke(TaskRequest("execution_info", {"run_ids": [caller]}))
        run = info.get(caller)
        if not run or run["state"] not in {"running", "waiting_user"}:
            raise ValueError("Media tools require an active task execution")
        if target and "media:" + target not in run.get("resources", []):
            raise ValueError("Background transfer requires the task resource media:" + target)
        return "task:" + run["task_id"]

    async def request(self, request):
        if not self.accepting:
            raise RuntimeError("Media service is stopping")
        action, args = request.action, request.arguments
        allowed = {
            "status": set(),
            "search": {"query", "before", "limit"},
            "inspect": {"share_url", "password", "parent_file_id", "marker"},
            "files": {"target", "marker"},
            "transfer": {
                "request_id",
                "share_url",
                "password",
                "file_id",
                "target",
                "collection",
                "season",
                "episode",
            },
            "download": {"request_id", "target", "file_id"},
            "get": {"operation_id"},
            "list": {"before", "limit", "collection"},
            "cancel": {"operation_id"},
            "resume": {"operation_id"},
            "verify": {"operation_id", "probe"},
            "login": set(),
            "login_status": {"login_id"},
        }
        if action not in allowed or not isinstance(args, dict) or set(args) - allowed[action]:
            raise ValueError("Unknown media action or arguments")
        owner = await self.authorize(request.caller_run)
        if action == "search":
            return await self.search.search(args.get("query"), args.get("before", 0), args.get("limit", 20))
        if action == "status":
            try:
                await self.provider.call("status")
            except MediaError as exc:
                if exc.code != "auth_required":
                    return {
                        "account": self.repo.account(),
                        "error": exc.code,
                        "targets": self.settings.targets,
                        "local_targets": self.settings.local_targets,
                    }
            return {
                "account": self.repo.account(),
                "targets": self.settings.targets,
                "local_targets": self.settings.local_targets,
                "openlist_configured": bool(
                    self.settings.openlist_url and self.openlist.credentials.exists()
                ),
            }
        if action == "inspect":
            values = {"share_id": share_id(args.get("share_url")), "password": self.password(args)}
            for field, default in (("parent_file_id", "root"), ("marker", "")):
                value = args.get(field, default)
                if not isinstance(value, str) or len(value) > 2000:
                    raise ValueError("Invalid share listing cursor/parent")
                values[field] = value
            return await self.provider.call("inspect", values)
        if action == "files":
            target = self.target(args.get("target"))
            marker = args.get("marker", "")
            if not isinstance(marker, str) or len(marker) > 2000:
                raise ValueError("Invalid media file cursor")
            return await self.provider.call("list", {**target, "marker": marker})
        if action in {"login", "login_status"} and request.caller_run:
            raise ValueError("Interactive account login is available only in the main conversation")
        if action == "login":
            return dict(self.provider.begin_login(self.context, self.login_completed))
        if action == "login_status":
            login = self.provider.login
            if not login or login["login_id"] != args.get("login_id"):
                raise ValueError("Media login not found; begin a new login")
            return dict(login)
        if action == "transfer":
            target_id = args.get("target")
            await self.authorize(request.caller_run, target_id)
            target = dict(self.target(target_id))
            intent = {
                "share_id": share_id(args.get("share_url")),
                "password": self.password(args),
                "file_id": identifier(args.get("file_id")),
                "target": target_id,
                "destination": target,
            }
            collection = args.get("collection")
            if collection is not None:
                if not isinstance(collection, str) or not 1 <= len(collection) <= 200:
                    raise ValueError("Collection identity must be 1..200 characters")
                intent["collection"] = collection
            for field in ("season", "episode"):
                value = args.get(field)
                if value is not None:
                    if not collection or type(value) is not int or not 0 <= value <= 100000:
                        raise ValueError("Season/episode require a collection and nonnegative integer")
                    intent[field] = value
            if (args.get("season") is None) != (args.get("episode") is None):
                raise ValueError("Provide both season and episode")
            item = self.repo.create(args.get("request_id"), intent, owner, request.caller_run)
            self.wakeup.set()
            return self.repo.public(item)
        if action == "download":
            target_id = args.get("target")
            target = dict(self.target(target_id))
            await self.authorize(request.caller_run, target_id)
            if target_id not in self.settings.local_targets:
                raise ValueError("No local download destination configured for this target")
            intent = {
                "kind": "download",
                "file_id": identifier(args.get("file_id")),
                "target": target_id,
                "destination": target,
                "local_destination": dict(self.settings.local_targets[target_id]),
            }
            item = self.repo.create(args.get("request_id"), intent, owner, request.caller_run)
            return self.repo.public(item)
        if action == "list":
            collection = args.get("collection")
            if collection is not None and (not isinstance(collection, str) or len(collection) > 200):
                raise ValueError("Invalid collection filter")
            return self.repo.page(
                before=args.get("before"), limit=args.get("limit", 20), collection=collection
            )
        item = self.repo.get(identifier(args.get("operation_id")))
        if action == "get":
            return self.repo.public(item)
        if action == "verify":
            if type(args.get("probe", False)) is not bool:
                raise ValueError("probe must be boolean")
            if item["state"] != "completed":
                return self.repo.public(item)
            if item["data"]["intent"].get("kind") == "download":
                return self.repo.public(item)
            await self.verify_openlist(item, probe=args.get("probe", False))
            return self.repo.public(self.repo.get(item["id"]))
        await self.authorize(request.caller_run, item["data"]["intent"]["target"])
        if request.caller_run and item["data"]["owner"] != owner:
            raise ValueError("Background tasks may manage only their own media operations")
        if action == "cancel":
            if item["state"] not in TERMINAL:
                if item["data"]["intent"].get("kind") == "download":
                    self.repo.change(
                        item["id"],
                        item["state"]
                        if item["state"] in {"checking", "downloading", "verifying"}
                        else "queued",
                        cancel_requested=True,
                    )
                    return self.repo.public(self.repo.get(item["id"]))
                cancel_now = not item["data"].get("submitted") and item["state"] != "checking"
                self.repo.change(
                    item["id"], "cancelled" if cancel_now else item["state"], cancel_requested=True
                )
        elif action == "resume":
            if item["state"] in {"uncertain", "failed", "waiting_auth"}:
                self.repo.change(
                    item["id"], "verifying" if item["data"].get("submitted") else "queued", attempts=0
                )
                self.wakeup.set()
        return self.repo.public(self.repo.get(item["id"]))

    @staticmethod
    def password(args):
        return extraction_code(args.get("share_url"), args.get("password", ""))

    def target(self, name):
        if not isinstance(name, str) or name not in self.settings.targets:
            raise ValueError("Unknown configured media target")
        return self.settings.targets[name]

    def login_completed(self):
        self.repo.resume_auth()
        self.wakeup.set()

    async def restore_authorization(self):
        # Login may have atomically saved new credentials just before a crash,
        # without getting to commit pending-operation resumption.
        try:
            await self.provider.call("status")
        except MediaError:
            return
        self.login_completed()

    async def matches(self, intent, file, *, expected_file_id=None, after_submit=False):
        marker = ""
        for _ in range(10):
            page = await self.provider.call("list", {**intent["destination"], "marker": marker})
            for candidate in page["items"]:
                if candidate.get("name") == file["name"]:
                    if candidate.get("size") != file["size"]:
                        raise MediaError("destination_name_conflict")
                    if (
                        file.get("content_hash")
                        and candidate.get("content_hash") == file["content_hash"]
                        and candidate.get("content_hash_name") == file.get("content_hash_name")
                    ):
                        candidate["verification"] = "content_hash"
                        return candidate
                    if expected_file_id and candidate.get("file_id") == expected_file_id:
                        candidate["verification"] = "provider_file_id"
                        return candidate
                    if after_submit and not expected_file_id and not file.get("content_hash"):
                        return None  # Same name/size alone is not proven identity.
                    raise MediaError("destination_name_conflict")
            marker = page.get("marker")
            if not marker:
                return None
        raise MediaError("destination_listing_limit")

    async def process(self, item):
        ident, data = item["id"], item["data"]
        intent = data["intent"]
        if item["state"] == "queued":
            self.repo.change(ident, "checking")
            if self.target(intent["target"]) != intent["destination"]:
                raise MediaError("target_configuration_changed")
            folder = await self.provider.call("folder", intent["destination"])
            if folder.get("type") != "folder":
                raise MediaError("destination_not_a_folder")
            file = await self.provider.call("source", intent)
            if (
                file.get("type") != "file"
                or file.get("category") != "video"
                or not isinstance(file.get("name"), str)
                or type(file.get("size")) is not int
                or file["size"] <= 0
            ):
                raise MediaError("source_must_be_a_verifiable_video_file")
            self.repo.change(ident, "checking", file=file)
            present = await self.matches(intent, file)
            if present:
                await self.complete(ident, present)
                return
            capacity = await self.provider.call("capacity")
            if type(capacity.get("total")) is not int or type(capacity.get("used")) is not int:
                raise MediaError("capacity_unconfirmed")
            if capacity["total"] - capacity["used"] < file["size"]:
                raise MediaError("capacity_exhausted")
            if self.repo.get(ident)["data"].get("cancel_requested"):
                self.repo.change(ident, "cancelled")
                return
            self.repo.change(ident, "submitting", submitted=True, submitted_at=time.time())
            try:
                result = await self.provider.call("copy", {**intent, **intent["destination"]})
            except MediaError as exc:
                if not exc.uncertain:
                    # An explicit rejection confirms no copy was accepted.
                    self.repo.change(ident, "checking", submitted=False)
                raise
            self.repo.change(ident, "verifying", copy_receipt=result)
        item = self.repo.get(ident)
        present = await self.matches(
            intent,
            item["data"]["file"],
            expected_file_id=item["data"].get("copy_receipt", {}).get("file_id"),
            after_submit=True,
        )
        if present:
            await self.complete(ident, present)
        else:
            elapsed = time.time() - item["data"].get("submitted_at", item["created_at"])
            self.repo.change(
                ident,
                "uncertain" if elapsed > 600 else "verifying",
                due=time.time() + 10,
                error="copy_result_not_yet_observed",
            )

    async def complete(self, ident, file):
        self.repo.change(
            ident, "completed", saved_file_id=file["file_id"], verification=file["verification"], error=None
        )
        item = self.repo.get(ident)
        await self.verify_openlist(item)
        # Operation facts and pending notice commit together; a lost publish ack
        # reuses the same notification key instead of executing copy again.
        self.reporter.offer(
            type="media.transfer_completed",
            subject="media:" + ident,
            event_id="media-complete:" + ident,
            data={"operation_id": ident, "target": item["data"]["intent"]["target"]},
        )

    async def verify_openlist(self, item, probe=False):
        try:
            result = await self.openlist.verify(
                item["data"]["intent"]["destination"], item["data"]["file"], probe=probe
            )
        except Exception:
            # Read integration failure cannot revoke an already verified cloud copy.
            result = {"state": "openlist_unavailable"}
        self.repo.change(item["id"], item["state"], openlist=result)

    async def worker(self):
        while True:
            item = self.repo.due()
            if item:
                try:
                    await self.process(item)
                except MediaError as exc:
                    current = self.repo.get(item["id"])
                    if exc.code == "auth_required":
                        self.repo.account_state("auth_required")
                        state = "waiting_auth"
                    elif current["data"].get("submitted"):
                        state = "verifying"
                    elif exc.code in {
                        "rate_limited",
                        "provider_network_error",
                        "provider_unavailable",
                        "sdk_worker_timeout",
                    }:
                        state = "queued"
                    else:
                        state = "failed"
                    attempts = current["data"].get("attempts", 0) + 1
                    if attempts >= 8 and state in {"queued", "verifying"}:
                        state = "uncertain" if current["data"].get("submitted") else "failed"
                    self.repo.change(
                        item["id"],
                        state,
                        due=time.time() + min(300, 2**attempts),
                        attempts=attempts,
                        error=exc.code,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    current = self.repo.get(item["id"])
                    self.repo.change(
                        item["id"],
                        "uncertain" if current["data"].get("submitted") else "failed",
                        error="media_internal_error",
                    )
                continue
            self.wakeup.clear()
            try:
                await asyncio.wait_for(self.wakeup.wait(), 2)
            except TimeoutError:
                pass

    async def notifications(self):
        while True:
            row = self.repo.db.execute(
                "SELECT key,payload FROM media_outbox ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row:
                import json

                try:
                    await self.host.runtime.invoke(NotificationRequest("publish", json.loads(row[1])))
                    with self.repo.db:
                        self.repo.db.execute("DELETE FROM media_outbox WHERE key=?", (row[0],))
                except Exception:
                    await asyncio.sleep(5)
            else:
                await asyncio.sleep(2)
