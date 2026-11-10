"""Media capability owner. Tasks own plans; providers own auth and protocol IO."""

from .declaration import DECLARATION

import asyncio
import time

from assistant_runtime.api.contracts import MediaRequest, TaskRequest, NotificationRequest
from .providers import Providers, CAPABILITIES
from .errors import MediaError
from .downloads import Downloads
from .openlist import OpenList
from .openlist_sync import OpenListSync
from .openlist_library import OpenListLibrary
from .openlist_files import ACTIONS as OPENLIST_ACTIONS
from .repository import MediaRepository, TERMINAL
from .search import PanSou, share_id, share_provider, extraction_code
from .search_history import SearchHistory
from .search_candidates import DISCOVERY_PLATFORMS
from .settings import identifier
from .library import Library, filename
from .batches import Batches
from .verification import cloud_binding, readback


class MediaPlugin:
    manifest = DECLARATION.manifest

    def __init__(self, api, providers=None):
        self.api = api
        self.settings = api.config
        self.repo = api.repository(MediaRepository, self.settings)
        self.search_history = api.repository(SearchHistory)
        self.library = Library(self)
        self.batches = Batches(self)
        self.root = api.state_dir
        self.provider = Providers(self.root, self.settings, self.repo, providers)
        self.wakeup = asyncio.Event()
        self.accepting = False

    def maintenance_pending(self):
        # Accepted operations must finish or be explicitly resolved. Unknown/auth-wait
        # states remain blockers; silence is never evidence of completion.
        rows = self.repo.db.execute(
            "SELECT id,state FROM media_operations WHERE state NOT IN ('completed','failed','cancelled') LIMIT 100"
        ).fetchall()
        result = [{"id": row[0], "state": row[1]} for row in rows]
        rows = self.repo.db.execute(
            "SELECT id,state FROM media_batches WHERE state IN ('queued','running','waiting_auth') LIMIT 100"
        ).fetchall()
        return result + [{"id": row[0], "state": row[1]} for row in rows]

    async def start(self, context):
        import aiohttp

        self.context = context
        self.repo.notifications_enabled = self.api.available(NotificationRequest)
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
        self.search = PanSou(
            self.session, self.settings.search_url, self.search_history, self.settings.accounts
        )
        self.openlist = OpenList(
            self.session, self.settings.openlist_url, self.root / "credentials" / "openlist.json"
        )
        self.openlist_sync = OpenListSync(self)
        self.reporter = context.event_reporter()
        context.own(self.reporter.close)
        self.repo.recover()
        self.accepting = True
        context.own(self.stop_accepting)
        context.register(MediaRequest, self.request)
        context.spawn(self.worker(), "acquisitions")
        context.spawn(self.library.worker(), "library")
        context.spawn(self.openlist_sync.worker(), "openlist-sync")
        context.spawn(self.batches.worker(), "batches")
        self.downloads = Downloads(self)
        context.spawn(self.downloads.worker(), "downloads")
        if self.repo.notifications_enabled:
            context.spawn(self.notifications(), "outbox")
        from .notifications import restore_associations

        if self.repo.notifications_enabled:
            context.spawn(restore_associations(self), "notification-associations")
        if self.repo.db.execute(
            "SELECT 1 FROM media_operations WHERE state='waiting_auth' LIMIT 1"
        ).fetchone():
            context.spawn(self.restore_authorization(), "auth-recovery")

    async def stop_accepting(self):
        self.accepting = False

    async def authorize(self, caller, target=None):
        if caller is None:
            return "assistant"
        info = await self.api.invoke(TaskRequest("execution_info", {"run_ids": [caller]}))
        run = info.get(caller)
        if not run or run["state"] not in {"running", "waiting_user"}:
            raise ValueError("Media tools require an active task execution")
        if target and "media:" + target not in run.get("resources", []):
            raise ValueError(
                "Background transfer requires the task resource media:"
                + target
                + ". Internal delegation binding mismatch; no write submitted. "
                "Foreground must verify the original user request and correct the run binding "
                "with repair_task_run_resources after stopping it, or create a correctly scoped "
                "follow-up for an ended task. Do not ask the user to supply internal resource keys "
                "or repeat existing authorization. A background task cannot grant itself access."
            )
        return "task:" + run["task_id"]

    async def request(self, request):
        try:
            return await self._request(request)
        except MediaError as error:
            raise error.tool_failure(request.action) from error

    async def _request(self, request):
        if not self.accepting:
            raise RuntimeError("Media service is stopping")
        action, args = request.action, request.arguments
        if action == "operation_outcomes":
            from .outcomes import for_run

            return for_run(self.repo, args, request.caller_run)
        allowed = {
            "status": set(),
            "search": {"query", "before", "limit", "search_id", "refresh", "aliases", "platforms"},
            "search_history": {"before", "limit"},
            "search_sources": set(),
            "inspect": {"share_url", "password", "parent_file_id", "marker", "refresh", "account"},
            "files": {"target", "marker", "folder_id"},
            "capabilities": set(),
            "library": {"target", "marker", "folder_id", "location", "path"},
            "preview": {"target", "changes", "location"},
            "apply": {"request_id", "plan_id"},
            "collection": {"collection", "definition", "revision", "target", "before", "limit"},
            "asset": {"asset_id"},
            "batch": {"request_id", "target", "action", "items"},
            "batch_get": {"batch_id"},
            "batch_cancel": {"batch_id"},
            "transfer": {
                "request_id",
                "share_url",
                "password",
                "file_id",
                "target",
                "collection",
                "season",
                "episode",
                "folder_id",
                "name",
                "version",
                "parent_file_id",
                "object_type",
            },
            "download": {"request_id", "target", "file_id", "path"},
            "get": {"operation_id"},
            "list": {"before", "limit", "collection"},
            "cancel": {"operation_id"},
            "resume": {"operation_id"},
            "verify": {"operation_id", "probe"},
            "login": {"account"},
            "login_status": {"login_id", "account"},
        }
        if action not in allowed or not isinstance(args, dict) or set(args) - allowed[action]:
            raise ValueError("Unknown media action or arguments")
        owner = await self.authorize(request.caller_run)
        if action == "asset":
            return self.library.asset(args["asset_id"])
        if action == "batch":
            await self.authorize(request.caller_run, args["target"])
            return self.batches.create(args, owner, request.caller_run)
        if action in {"batch_get", "batch_cancel"}:
            result = self.batches.get(args["batch_id"])
            if action == "batch_cancel":
                await self.authorize(request.caller_run, result["target"])
                return self.batches.cancel(args["batch_id"], owner)
            return result
        if action == "capabilities":
            return {
                "accounts": {
                    name: {
                        **definition,
                        "actions": sorted(CAPABILITIES[definition["provider"]]),
                        "files": {
                            "backend": self.settings.files_for(name),
                            "addressing": "relative_path"
                            if self.settings.files_for(name) == "openlist"
                            else "file_id",
                            "actions": list(OPENLIST_ACTIONS)
                            if self.settings.files_for(name) == "openlist"
                            else sorted(CAPABILITIES[definition["provider"]]),
                            "download_requires_upstream_hash": True,
                            "directory_trash": self.settings.files_for(name) == "native"
                            and definition["provider"] in {"aliyun", "quark"},
                        },
                    }
                    for name, definition in self.settings.accounts.items()
                },
                "discovery": {
                    "platforms": list(DISCOVERY_PLATFORMS),
                    "aliases_per_search": 3,
                    "source_inventory": "search_sources",
                    "history": "search_history",
                    "coverage": "partial",
                    "identity_verification_required": True,
                },
                "acquisition_providers": sorted(
                    {a["provider"] for a in self.settings.accounts.values() if a["provider"] != "openlist"}
                ),
                "targets": list(self.settings.targets),
                "target_bindings": {
                    name: {
                        "account": target["account"],
                        "provider": self.settings.accounts[target["account"]]["provider"],
                        "task_resource": "media:" + name,
                    }
                    for name, target in self.settings.targets.items()
                },
                "cloud": "See each account actions; unsupported actions fail before submission",
                "limits": {"change_items": 100, "directory_depth": 16, "conflict_scan": 1000},
                "local": ["list", "mkdir", "rename", "move", "trash", "restore"],
                "restore_scope": "recorded_own_trash",
                "permanent_delete": False,
                "directory_transfer": {
                    "selection": "explicit_share_folder",
                    "preserves_all_children": True,
                    "cross_provider": False,
                    "max_entries": 1000,
                    "verification": "receipt_identity_and_relative_manifest",
                },
                "evidence": "implementation_available_provider_account_validation_is_separate",
            }
        if action == "library":
            if args.get("location", "cloud") == "local":
                from .local_library import LocalLibrary

                return await LocalLibrary(self.library).listing(
                    args["target"], args.get("path", ""), args.get("marker", "")
                )
            if args.get("location", "cloud") != "cloud":
                raise ValueError("Unknown library location")
            if self.settings.files_for(self.target(args["target"])["account"]) == "openlist":
                if args.get("folder_id") is not None:
                    raise ValueError("OpenList browsing uses relative path, not folder_id")
                return await OpenListLibrary(self.library).listing(
                    args["target"], args.get("path", ""), args.get("marker", "")
                )
            if args.get("path"):
                raise ValueError("Native browsing uses folder_id, not path")
            return await self.library.listing(args["target"], args.get("folder_id"), args.get("marker", ""))
        if action == "preview":
            await self.authorize(request.caller_run, args["target"])
            return await self.library.preview(args, owner)
        if action == "apply":
            # Resolve the persisted plan on the server, never accept a caller-supplied scope.
            import json

            row = self.repo.db.execute(
                "SELECT data FROM media_change_plans WHERE id=?", (args.get("plan_id"),)
            ).fetchone()
            if not row:
                raise ValueError("Change plan not found")
            await self.authorize(request.caller_run, json.loads(row[0])["target"])
            return self.library.apply(args, owner, request.caller_run)
        if action == "collection":
            if "definition" in args:
                self.target(args.get("target"))
                await self.authorize(request.caller_run, args["target"])
            return self.library.collection(args)
        if action == "search_history":
            return self.search_history.list(args.get("before"), args.get("limit", 20))
        if action == "search_sources":
            return await self.search.health()
        if action == "search":
            return await self.search.search(
                args.get("query"),
                args.get("before", 0),
                args.get("limit", 20),
                search_id=args.get("search_id"),
                refresh=args.get("refresh", False),
                aliases=args.get("aliases"),
                platforms=args.get("platforms"),
            )
        if action == "status":

            async def status(account):
                try:
                    await self.account_status(account)
                    error = None
                except MediaError as exc:
                    error = exc.code
                return {**self.settings.accounts[account], **self.repo.account(account), "error": error}

            values = await asyncio.gather(*(status(account) for account in self.settings.accounts))
            return {
                "accounts": dict(zip(self.settings.accounts, values)),
                "targets": self.settings.targets,
                "local_targets": self.settings.local_targets,
                "openlist_configured": bool(
                    self.settings.openlist_url and self.openlist.credentials.exists()
                ),
            }
        if action == "inspect":
            account = self.provider.resolve(
                args.get("account"), provider=share_provider(args.get("share_url"))
            )
            values = {
                "account": account,
                "share_id": share_id(args.get("share_url")),
                "password": self.password(args),
            }
            for field, default in (("parent_file_id", "root"), ("marker", "")):
                value = args.get(field, default)
                if not isinstance(value, str) or len(value) > 2000:
                    raise ValueError("Invalid share listing cursor/parent")
                values[field] = value
            return await self.search_history.inspect(self.provider, values, args.get("refresh", False))
        if action == "files":
            target_name = args.get("target")
            self.provider.require(self.target(target_name)["account"], "list")
            target, _ = await self.library.destination(target_name, args.get("folder_id"))
            marker = args.get("marker", "")
            if not isinstance(marker, str) or len(marker) > 2000:
                raise ValueError("Invalid media file cursor")
            return await self.provider.call("list", {**target, "marker": marker})
        if action in {"login", "login_status"} and request.caller_run:
            raise ValueError("Interactive account login is available only in the main conversation")
        if action in {"login", "login_status"}:
            account = self.provider.resolve(args.get("account"))
            self.provider.require(account, "login")
            adapter = self.provider.adapters[account]
        if action == "login":
            return dict(adapter.begin_login(self.context, lambda: self.login_completed(account)))
        if action == "login_status":
            login = adapter.login
            if not login or login["login_id"] != args.get("login_id"):
                raise ValueError("Media login not found; begin a new login")
            return dict(login)
        if action == "transfer":
            target_id = args.get("target")
            await self.authorize(request.caller_run, target_id)
            target = dict(self.target(target_id))
            self.provider.resolve(target["account"], provider=share_provider(args.get("share_url")))
            self.provider.require(target["account"], "copy")
            if args.get("name") is not None:
                self.provider.require(target["account"], "rename")
            intent = {
                "share_id": share_id(args.get("share_url")),
                "password": self.password(args),
                "file_id": identifier(args.get("file_id")),
                "target": target_id,
                "destination": target,
            }
            object_type = args.get("object_type", "file")
            if object_type not in {"file", "directory"}:
                raise ValueError("Transfer object_type must be file or directory")
            if object_type == "directory":
                if any(
                    args.get(key) is not None
                    for key in ("name", "collection", "season", "episode", "version")
                ):
                    raise ValueError(
                        "A directory transfer preserves the complete folder; episode/rename options are file-only"
                    )
                intent["kind"] = "directory"
            if args.get("parent_file_id") is not None:
                intent["parent_file_id"] = identifier(args["parent_file_id"])
            if args.get("folder_id"):
                intent["root_destination"] = target
                intent["destination"], _ = await self.library.destination(target_id, args["folder_id"])
            if args.get("name") is not None:
                intent["desired_name"] = filename(args["name"])
            if args.get("version") is not None:
                intent["version"] = identifier(args["version"])
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
            if self.settings.files_for(target["account"]) == "openlist":
                return await OpenListLibrary(self.library).enqueue_download(args, owner, request.caller_run)
            if args.get("path") is not None:
                raise ValueError("Native downloads use file_id, not path")
            self.provider.require(target["account"], "download_url")
            if target_id not in self.settings.local_targets:
                raise ValueError("No local download destination configured for this target")
            intent = {
                "kind": "download",
                "file_id": identifier(args.get("file_id")),
                "target": target_id,
                "destination": target,
                "local_destination": dict(self.settings.local_targets[target_id]),
            }
            # Existing direct-child callers retain their bindings; nested files resolve
            # from a provider ID through the configured root, never an arbitrary path.
            loc = await self.library.locate(target_id, intent["file_id"])
            parts = loc["relative_parts"][:-1]
            if parts:
                intent["root_destination"] = target
                intent["destination"], _ = await self.library.destination(
                    target_id, loc["file"]["parent_file_id"]
                )
                intent["relative_parts"] = parts
            if self.repo.db.execute(
                """SELECT 1 FROM media_operations, json_each(data,'$.steps') AS step
                 WHERE state NOT IN ('completed','failed','cancelled')
                 AND json_extract(data,'$.intent.target')=?
                 AND (json_extract(step.value,'$.file_id')=? OR json_extract(data,'$.intent.location')='local') LIMIT 1""",
                (target_id, intent["file_id"]),
            ).fetchone():
                raise MediaError("media_file_busy")
            if self.repo.local_busy(intent["local_destination"], "library"):
                raise MediaError("local_library_busy")
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
            await self.authorize(request.caller_run, item["data"]["intent"]["target"])
            if item["data"]["intent"].get("kind") == "library" and item["state"] == "uncertain":
                return await self.library.reconcile(item)
            if item["data"]["intent"].get("kind") == "directory":
                from .directory_transfer import reconcile

                return await reconcile(self, item)
            if item["data"]["intent"].get("kind") == "download" and item["state"] == "uncertain":
                if item["data"].get("submitted"):
                    # This state only reads and verifies the local destination.
                    item = self.repo.change(item["id"], "verifying")
                return self.repo.public(item)
            self.openlist_sync.retry(item["id"])
            if item["state"] != "completed":
                return self.repo.public(self.repo.get(item["id"]))
            if item["data"]["intent"].get("kind") in {"download", "library"}:
                return self.repo.public(self.repo.get(item["id"]))
            try:
                _, evidence = await readback(
                    self, item, item["data"]["saved_file_id"], item["data"]["verification"]
                )
            except MediaError as exc:
                evidence = {"state": "unconfirmed", "error": exc.code, "observed_at": time.time()}
            self.repo.change(item["id"], item["state"], cloud_verification=evidence)
            await self.verify_openlist(item, probe=args.get("probe", False))
            return self.repo.public(self.repo.get(item["id"]))
        await self.authorize(request.caller_run, item["data"]["intent"]["target"])
        if request.caller_run and item["data"]["owner"] != owner:
            raise ValueError("Background tasks may manage only their own media operations")
        if action == "cancel":
            return self.repo.public(self.repo.request_cancel(item["id"]))
        elif action == "resume":
            item = self.repo.get(item["id"])
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

    def login_completed(self, account="aliyun"):
        self.repo.resume_auth(account)
        self.wakeup.set()

    async def account_status(self, account):
        if self.settings.accounts[account]["provider"] != "openlist":
            return await self.provider.call("status", {"account": account})
        targets = [name for name, binding in self.settings.targets.items() if binding["account"] == account]
        if not targets:
            raise MediaError("openlist_no_targets")
        try:
            for target in targets:
                await OpenListLibrary(self.library).files(target).listing()
        except MediaError as exc:
            if exc.code == "auth_required":
                self.repo.account_state("auth_required", account=account)
            raise
        self.repo.account_state("ready", account=account)
        return {"state": "ready"}

    async def restore_authorization(self):
        for account in self.settings.accounts:
            try:
                await self.account_status(account)
            except MediaError:
                continue
            self.login_completed(account)

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
        if cloud_binding(self.target(intent["target"])) != cloud_binding(
            intent.get("root_destination", intent["destination"])
        ):
            raise MediaError("target_configuration_changed")
        if item["state"] == "queued":
            self.repo.change(ident, "checking")
            if cloud_binding(self.target(intent["target"])) != cloud_binding(
                intent.get("root_destination", intent["destination"])
            ):
                raise MediaError("target_configuration_changed")
            if "root_destination" in intent:
                await self.library.destination(intent["target"], intent["destination"]["folder_id"])
            folder = await self.provider.call("folder", intent["destination"])
            if folder.get("type") != "folder":
                raise MediaError("destination_not_a_folder")
            file = await self.provider.call("source", {**intent, "account": intent["destination"]["account"]})
            if (
                file.get("type") != "file"
                or file.get("category") != "video"
                or not isinstance(file.get("name"), str)
                or type(file.get("size")) is not int
                or file["size"] <= 0
            ):
                raise MediaError("source_must_be_a_verifiable_video_file")
            self.repo.change(ident, "checking", file=file)
            if intent.get("desired_name"):
                from pathlib import PurePosixPath

                if (
                    PurePosixPath(intent["desired_name"]).suffix.lower()
                    != PurePosixPath(file["name"]).suffix.lower()
                ):
                    raise MediaError("media_extension_changed")
                if intent["desired_name"] != file["name"]:
                    await self.library.conflict(
                        intent["target"], intent["destination"]["folder_id"], intent["desired_name"]
                    )
            present = await self.matches(intent, file)
            if present:
                await self.complete(ident, present)
                return
            capacity = await self.provider.call("capacity", {"account": intent["destination"]["account"]})
            if type(capacity.get("total")) is not int or type(capacity.get("used")) is not int:
                raise MediaError("capacity_unconfirmed")
            if capacity["total"] - capacity["used"] < file["size"]:
                raise MediaError("capacity_exhausted")
            self.library.protect_destination(
                intent["destination"], {file["name"], intent.get("desired_name")}, excluding=ident
            )
            if not self.repo.begin_write(ident, submitted_at=time.time()):
                return
            try:
                result = await self.provider.call("copy", {**intent, **intent["destination"]})
            except MediaError as exc:
                if not exc.uncertain:
                    # An explicit rejection confirms no copy was accepted.
                    self.repo.change(ident, "checking", submitted=False)
                raise
            self.repo.change(ident, "verifying", copy_receipt=result)
        item = self.repo.get(ident)
        if item["data"].get("organization_file_id"):
            await self.complete(
                ident,
                {
                    "file_id": item["data"]["organization_file_id"],
                    "verification": item["data"]["verification"],
                },
            )
            return
        receipt = item["data"].get("copy_receipt", {})
        if receipt.get("task_id") and not receipt.get("file_id"):
            observed = await self.provider.call(
                "copy_status", {**intent["destination"], "task_id": receipt["task_id"]}
            )
            if observed.get("file_id"):
                receipt = {**receipt, **observed}
                item = self.repo.change(ident, "verifying", copy_receipt=receipt)
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
        item = self.repo.get(ident)
        intent = item["data"]["intent"]
        actual, evidence = await readback(
            self, item, file["file_id"], file["verification"], before_rename=True
        )
        if intent.get("desired_name"):
            self.repo.change(
                ident,
                "verifying",
                organization_file_id=file["file_id"],
                verification=file["verification"],
                saved_file_id=file["file_id"],
                cloud_verification=evidence,
            )
            if actual["name"] != intent["desired_name"]:
                if item["data"].get("rename_submitted"):
                    raise MediaError("rename_result_unconfirmed", uncertain=True)
                await self.library.conflict(
                    intent["target"],
                    intent["destination"]["folder_id"],
                    intent["desired_name"],
                    file["file_id"],
                )
                self.library.protect_destination(
                    intent["destination"], {intent["desired_name"]}, excluding=ident
                )
                if not self.repo.begin_write(ident, "verifying", rename_submitted=True):
                    return
                try:
                    await self.provider.call(
                        "rename",
                        {**intent["destination"], "file_id": file["file_id"], "name": intent["desired_name"]},
                    )
                except MediaError as exc:
                    if not exc.uncertain:
                        self.repo.change(ident, "verifying", rename_submitted=False)
                    raise
                actual, evidence = await readback(self, item, file["file_id"], file["verification"])
                if actual["name"] != intent["desired_name"]:
                    raise MediaError("rename_result_unconfirmed", uncertain=True)
        self.repo.change(
            ident,
            "completed",
            file=actual,
            saved_file_id=file["file_id"],
            verification=file["verification"],
            cloud_verification=evidence,
            error=None,
        )
        item = self.repo.get(ident)
        known = {
            **item["data"]["file"],
            "file_id": file["file_id"],
            "parent_file_id": intent["destination"]["folder_id"],
        }
        self.library.remember(
            intent["target"],
            known,
            trashed=False,
            **{k: intent[k] for k in ("collection", "season", "episode", "version") if k in intent},
        )
        # Directory synchronization is persisted with completion and runs independently.
        # Explicit verification retains the separate file visibility/probe operation.
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
            intent = item["data"]["intent"]
            if cloud_binding(self.target(intent["target"])) != cloud_binding(
                intent.get("root_destination", intent["destination"])
            ):
                raise MediaError("target_configuration_changed")
            # Resolve only below the configured cloud root. OpenList may mount any
            # ancestor/subdirectory: its independent root is never inferred from cloud names.
            target, _ = await self.library.destination(intent["target"], intent["destination"]["folder_id"])
            result = await self.openlist.verify(target, item["data"]["file"], probe=probe)
        except MediaError as exc:
            result = {"state": exc.code}
        except Exception:
            # Read integration failure cannot revoke an already verified cloud copy.
            result = {"state": "openlist_unavailable"}
        self.repo.change(item["id"], item["state"], openlist=result)

    async def worker(self):
        while True:
            item = self.repo.due(("transfer", "directory"))
            if not item:
                observation = self.repo.observation_due("directory")
                if observation:
                    from .directory_transfer import reconcile

                    self.repo.begin_observation(observation)
                    try:
                        await reconcile(self, observation)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self.repo.change(
                            observation["id"],
                            "uncertain",
                            error="directory_observation_failed",
                            due=self.repo.get(observation["id"])["due"],
                        )
                    continue
            if item:
                try:
                    async with self.library.write_lock(item):
                        item = self.repo.get(item["id"])
                        if item["state"] in TERMINAL:
                            continue
                        if item["data"]["intent"].get("kind") == "directory":
                            from .directory_transfer import process

                            await process(self, item)
                        else:
                            await self.process(item)
                except MediaError as exc:
                    current = self.repo.get(item["id"])
                    if exc.code == "auth_required":
                        self.repo.account_state(
                            "auth_required", account=current["data"]["intent"]["destination"]["account"]
                        )
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
        from .notifications import publish

        while True:
            row = self.repo.db.execute(
                "SELECT key,payload FROM media_outbox ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row:
                import json

                try:
                    await publish(self, json.loads(row[1]))
                    with self.repo.db:
                        self.repo.db.execute("DELETE FROM media_outbox WHERE key=?", (row[0],))
                except Exception:
                    await asyncio.sleep(5)
            else:
                await asyncio.sleep(2)
