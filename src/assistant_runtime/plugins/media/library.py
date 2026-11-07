"""Scoped library facts and durable, preconditioned change plans.

Provider IDs identify cloud objects; receipts retain historical paths. Plans are
bounded data, never executable scripts. Unknown writes are inspected, not replayed.
"""

import asyncio
import hashlib
import json
import time
import uuid
from pathlib import PurePosixPath

from .errors import MediaError
from .repository import canonical
from .settings import identifier


def filename(value):
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or len(value.encode()) > 255
        or any(c in value for c in "/\\")
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("Invalid media filename")
    return value


def relative(value):
    if not isinstance(value, str) or len(value) > 2000 or value.startswith("/"):
        raise ValueError("Expected a bounded relative media path")
    parts = value.split("/") if value else []
    if len(parts) > 16:
        raise ValueError("Media directory nesting limit")
    return [filename(p) for p in parts]


class Library:
    def __init__(self, plugin):
        self.p, self.repo = plugin, plugin.repo
        self.db = self.repo.db
        self.lock = asyncio.Lock()
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS media_assets (
            id TEXT PRIMARY KEY, target TEXT NOT NULL, file_id TEXT NOT NULL,
            updated REAL NOT NULL, data TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS media_assets_target ON media_assets(target,id);
          CREATE TABLE IF NOT EXISTS media_change_plans (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, created REAL NOT NULL, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS media_collections (
            id TEXT PRIMARY KEY, revision INTEGER NOT NULL, data TEXT NOT NULL);
        """)

    def asset_id(self, target, file_id):
        binding = self.p.target(target)
        return hashlib.sha256(canonical([target, binding, file_id]).encode()).hexdigest()[:32]

    def remember(self, target, file, **extra):
        ident = self.asset_id(
            target, ("local_" if extra.get("location") == "local" else "") + file["file_id"]
        )
        row = self.db.execute("SELECT data FROM media_assets WHERE id=?", (ident,)).fetchone()
        data = json.loads(row[0]) if row else {}
        if not row:
            prior = self.db.execute(
                """SELECT data FROM media_operations WHERE state='completed'
                AND json_extract(data,'$.intent.target')=? AND json_extract(data,'$.saved_file_id')=?
                LIMIT 1""",
                (target, file["file_id"]),
            ).fetchone()
            if prior:
                intent = json.loads(prior[0])["intent"]
                if intent.get("root_destination", intent["destination"]) == self.p.target(target):
                    data.update(
                        {k: intent[k] for k in ("collection", "season", "episode", "version") if k in intent}
                    )
            if (
                self.db.execute("SELECT count(*) FROM media_assets").fetchone()[0]
                >= self.p.settings.max_operations
            ):
                raise ValueError("Library catalog capacity reached")
        data.update(file=file, binding=dict(self.p.target(target)), **{"present": True, **extra})
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO media_assets VALUES(?,?,?,?,?)",
                (ident, target, file["file_id"], time.time(), canonical(data)),
            )
        return {"asset_id": ident, "target": target, **data}

    async def locate(self, target, file_id):
        root = self.p.target(target)
        if file_id == root["folder_id"]:
            return {"file": {"file_id": file_id, "type": "folder"}, "relative_parts": []}
        result = await self.p.provider.call("locate", {**root, "file_id": identifier(file_id)})
        for part in result["relative_parts"]:
            filename(part)
        return result

    async def destination(self, target, folder_id=None):
        root = dict(self.p.target(target))
        if not folder_id or folder_id == root["folder_id"]:
            return root, []
        result = await self.locate(target, folder_id)
        if result["file"]["type"] != "folder":
            raise MediaError("destination_not_a_folder")
        root["folder_id"] = folder_id
        root["openlist_path"] = str(PurePosixPath(root["openlist_path"]).joinpath(*result["relative_parts"]))
        return root, result["relative_parts"]

    async def listing(self, target, folder_id=None, marker=""):
        if not isinstance(marker, str) or len(marker) > 2000:
            raise ValueError("Invalid media listing cursor")
        dest, parts = await self.destination(target, folder_id)
        page = await self.p.provider.call("list", {**dest, "marker": marker})
        items = []
        for file in page["items"]:
            file = {**file, "parent_file_id": dest["folder_id"]}
            items.append(
                self.remember(target, file, relative_path="/".join([*parts, file["name"]]), trashed=False)
            )
        return {
            "items": items,
            "marker": page.get("marker"),
            "complete": not page.get("marker"),
            "coverage": {
                "target": target,
                "folder_id": dest["folder_id"],
                "recursive": False,
                "observed_at": time.time(),
            },
        }

    async def conflict(self, target, folder_id, name, except_id=None):
        dest, _ = await self.destination(target, folder_id)
        marker = ""
        for _ in range(10):
            page = await self.p.provider.call("list", {**dest, "marker": marker})
            for file in page["items"]:
                if file["name"] == name and file["file_id"] != except_id:
                    raise MediaError("destination_name_conflict")
            marker = page.get("marker")
            if not marker:
                return
        raise MediaError("destination_listing_limit")

    def busy(self, target, file_id, *, excluding=None):
        direct = self.db.execute(
            """SELECT 1 FROM media_operations WHERE state NOT IN
            ('completed','failed','cancelled') AND json_extract(data,'$.intent.target')=?
            AND ((json_extract(data,'$.intent.kind')='download' AND json_extract(data,'$.intent.file_id')=?)
                 OR json_extract(data,'$.organization_file_id')=?
                 OR json_extract(data,'$.copy_receipt.file_id')=?) LIMIT 1""",
            (target, file_id, file_id, file_id),
        ).fetchone()
        if direct:
            return True
        for ident, raw in self.db.execute(
            "SELECT id,data FROM media_operations WHERE state NOT IN ('completed','failed','cancelled') "
            "AND json_extract(data,'$.intent.target')=? AND json_extract(data,'$.intent.kind')='directory'",
            (target,),
        ):
            if ident != excluding and any(
                row["file_id"] == file_id
                for row in json.loads(raw).get("destination_manifest", {}).get("entries", [])
            ):
                return True
        for ident, raw in self.db.execute(
            "SELECT id,data FROM media_operations WHERE state NOT IN ('completed','failed','cancelled') "
            "AND json_extract(data,'$.intent.target')=? AND json_extract(data,'$.intent.kind')='library'",
            (target,),
        ):
            if ident == excluding:
                continue
            for step in json.loads(raw).get("steps", []):
                if step["state"] == "completed":
                    continue
                ids = {
                    step.get("file_id"),
                    *[row["file_id"] for row in step.get("manifest", {}).get("entries", [])],
                }
                if file_id in ids:
                    return True
        return False

    async def preview(self, args, owner):
        from .tool_contracts import CHANGES
        from assistant_runtime.mcp.contracts import validate

        validate(args.get("changes"), CHANGES, "changes")
        if args.get("location", "cloud") == "local":
            from .local_library import LocalLibrary

            return await LocalLibrary(self).preview(args, owner)
        if args.get("location", "cloud") != "cloud":
            raise ValueError("Unknown library location")
        target = args["target"]
        root = self.p.target(target)
        if self.p.settings.files_for(root["account"]) == "openlist":
            from .openlist_library import OpenListLibrary

            return await OpenListLibrary(self).preview(args, owner)
        changes = args.get("changes")
        if not isinstance(changes, list) or not 1 <= len(changes) <= 100:
            raise ValueError("A library plan requires 1..100 changes")
        steps, seen = [], set()
        for change in changes:
            if not isinstance(change, dict):
                raise ValueError("Expected a change object")
            action = change.get("action")
            directory = action == "trash" and "folder_id" in change
            fields = {
                "mkdir": {"action", "folder_id", "name"},
                "rename": {"action", "file_id", "name"},
                "move": {"action", "file_id", "folder_id"},
                "trash": {"action", "folder_id"} if directory else {"action", "file_id"},
                "restore": {"action", "file_id"},
            }
            if not isinstance(action, str) or action not in fields or set(change) != fields[action]:
                raise ValueError("Unknown library change or fields")
            self.p.provider.require(root["account"], action)
            step = dict(change)
            if directory:
                step = {"action": "trash", "file_id": change["folder_id"], "object_type": "folder"}
            if action == "mkdir":
                identifier(step["folder_id"])
                filename(step["name"])
                await self.destination(target, step["folder_id"])
                before = None
            else:
                fid = identifier(step["file_id"])
                if fid in seen or fid == root["folder_id"]:
                    raise ValueError("One change per file per plan; configured roots cannot change")
                seen.add(fid)
                if self.busy(target, fid):
                    raise MediaError("media_file_busy")
                if action == "restore":
                    row = self.db.execute(
                        "SELECT data FROM media_assets WHERE id=?", (self.asset_id(target, fid),)
                    ).fetchone()
                    saved = json.loads(row[0]) if row else {}
                    if not saved.get("trashed") or not saved.get("trash_operation"):
                        raise ValueError(
                            "Restore requires our recorded trash operation and original location"
                        )
                    before = saved["file"]
                    step["recycle_id"] = saved.get("recycle_id")
                    if before["type"] == "folder":
                        if not saved.get("manifest"):
                            raise MediaError("directory_content_unverifiable")
                        step.update(object_type="folder", manifest=saved["manifest"])
                else:
                    loc = await self.locate(target, fid)
                    before = loc["file"]
                    if before["type"] != "file" and not (directory and before["type"] == "folder"):
                        raise ValueError(
                            "Folder management supports explicit trash(folder_id) and recorded restore only"
                        )
                    if directory:
                        from .directories import snapshot

                        step["manifest"] = await snapshot(self.p.provider, before, root)
                        children = {r["file_id"] for r in step["manifest"]["entries"]}
                        if children & (seen - {fid}) or any(self.busy(target, child) for child in children):
                            raise MediaError("media_file_busy")
                        seen.update(children)
                    self.remember(
                        target, before, relative_path="/".join(loc["relative_parts"]), trashed=False
                    )
                if action == "rename":
                    filename(step["name"])
                    if (
                        PurePosixPath(step["name"]).suffix.lower()
                        != PurePosixPath(before["name"]).suffix.lower()
                    ):
                        raise ValueError("Renaming must preserve the file extension")
                if action in {"move", "restore"}:
                    await self.destination(target, step.get("folder_id", before["parent_file_id"]))
            step["before"] = before
            if action != "trash":
                dest = step.get("folder_id") or before["parent_file_id"]
                name = step.get("name") or before["name"]
                await self.conflict(target, dest, name, step.get("file_id"))
                if any(s.get("destination") == [dest, name] for s in steps):
                    raise ValueError("Conflicting destinations in this plan")
                step["destination"] = [dest, name]
            step["state"] = "pending"
            steps.append(step)
        return self.save_plan(owner, {"target": target, "root": root, "steps": steps})

    def save_plan(self, owner, data):
        if (
            self.db.execute("SELECT count(*) FROM media_change_plans").fetchone()[0]
            >= self.p.settings.max_operations
        ):
            raise ValueError("Library plan capacity reached")
        ident = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO media_change_plans VALUES(?,?,?,?)", (ident, owner, time.time(), canonical(data))
            )
        return {"plan_id": ident, **data, "expires_in_seconds": 3600}

    def apply(self, args, owner, caller):
        row = self.db.execute(
            "SELECT owner,created,data FROM media_change_plans WHERE id=?", (identifier(args.get("plan_id")),)
        ).fetchone()
        if not row or (owner != "assistant" and row[0] != owner):
            raise ValueError("Change plan not found or not owned by this task")
        data = json.loads(row[2])
        intent = {
            "kind": "library",
            "target": data["target"],
            "destination": data["root"],
            "plan_id": args["plan_id"],
        }
        if data.get("file_backend") == "openlist":
            intent.update(file_backend="openlist", backend_binding=data["backend_binding"])
        if data.get("location") == "local":
            intent.update(location="local", local_binding=data["local_binding"])
        # A lost apply reply remains recoverable even after the preview expires.
        existing = self.db.execute(
            "SELECT id FROM media_operations WHERE request_id=?", (args.get("request_id"),)
        ).fetchone()
        if not existing and time.time() - row[1] > 3600:
            raise ValueError("Change plan expired; preview current state again")
        if not existing:
            for step in data["steps"]:
                for fid in {
                    step.get("file_id"),
                    *[r["file_id"] for r in step.get("manifest", {}).get("entries", [])],
                } - {None}:
                    if self.busy(data["target"], fid):
                        raise MediaError("media_file_busy")
        item = self.repo.create(
            args.get("request_id"), intent, owner, caller, initial={"steps": data["steps"]}
        )
        return self.repo.public(item)

    async def process(self, item, *, observe_only=False):
        ident = item["id"]
        intent = item["data"]["intent"]
        target = intent["target"]
        if self.p.target(target) != intent["destination"]:
            raise MediaError("target_configuration_changed")
        if intent.get("location") == "local":
            from .local_library import LocalLibrary

            return await LocalLibrary(self).process(item)
        if intent.get("file_backend", "native") != self.p.settings.files_for(
            intent["destination"]["account"]
        ):
            raise MediaError("target_configuration_changed")
        if intent.get("file_backend") == "openlist":
            from .openlist_library import OpenListLibrary

            return await OpenListLibrary(self).process(item)
        steps = item["data"]["steps"]
        for step in steps:
            if step["state"] == "completed":
                continue
            if observe_only and step["state"] != "submitted":
                continue
            current = self.repo.get(ident)
            if current["data"].get("cancel_requested") and step["state"] != "submitted":
                self.repo.change(ident, "cancelled", steps=steps, submitted=False)
                return
            action = step["action"]
            fid = step.get("file_id")
            before = step["before"]
            if fid and self.busy(target, fid, excluding=ident):
                raise MediaError("media_file_busy")
            if step["state"] != "submitted":
                if action not in {"mkdir", "restore"}:
                    actual = (await self.locate(target, fid))["file"]
                    if any(
                        actual.get(k) != before.get(k)
                        for k in (
                            ("file_id", "name", "parent_file_id", "size", "content_hash")
                            if before["type"] == "file"
                            else ("file_id", "name", "parent_file_id", "type")
                        )
                    ):
                        raise MediaError("library_precondition_changed")
                    if step.get("object_type") == "folder":
                        from .directories import snapshot

                        manifest = await snapshot(self.p.provider, before, intent["destination"])
                        if manifest["digest"] != step["manifest"]["digest"]:
                            raise MediaError("library_precondition_changed")
                if action != "trash":
                    await self.conflict(target, *step["destination"], fid)
                step["state"] = "submitted"
                step["submitted_at"] = time.time()
                self.repo.change(ident, "submitting", steps=steps, submitted=True)
                arguments = {
                    **intent["destination"],
                    **{k: v for k, v in step.items() if k in {"file_id", "folder_id", "name"}},
                }
                if action == "move":
                    arguments["to_folder_id"] = step["folder_id"]
                if action == "restore":
                    arguments.update(recycle_id=step.get("recycle_id"), expected_file=before)
                try:
                    reply = await self.p.provider.call(action, arguments)
                except MediaError as exc:
                    if not exc.uncertain:
                        step["state"] = "pending"
                        self.repo.change(ident, "checking", steps=steps, submitted=False)
                    raise
                step["receipt"] = reply
                self.repo.change(ident, "verifying", steps=steps)
            # Verify every submitted write, including one interrupted by shutdown.
            if action == "trash":
                marker = ""
                found = None
                for _ in range(10):
                    page = await self.p.provider.call(
                        "trash_list", {**intent["destination"], "marker": marker}
                    )
                    matches = [f for f in page["items"] if f["file_id"] == fid]
                    if len(matches) > 1:
                        raise MediaError("library_write_unconfirmed", uncertain=True)
                    found = matches[0] if matches else None
                    marker = page.get("marker")
                    if found or not marker:
                        break
                if not found:
                    raise MediaError("library_write_unconfirmed", uncertain=True)
                from .verification import recycle_evidence

                step["verification"] = recycle_evidence(
                    before,
                    found,
                    parent_scope=page.get("parent_scope", "original"),
                    submitted_at=step.get("submitted_at"),
                )
                self.remember(
                    target,
                    before,
                    trashed=True,
                    trash_operation=ident,
                    recycle_id=found.get("recycle_id"),
                    manifest=step.get("manifest"),
                )
            else:
                saved_id = fid or step.get("receipt", {}).get("file_id")
                if not saved_id:
                    raise MediaError("mkdir_identity_unconfirmed", uncertain=True)
                loc = await self.locate(target, saved_id)
                file = loc["file"]
                if [file["parent_file_id"], file["name"]] != step["destination"]:
                    raise MediaError("library_write_unconfirmed", uncertain=True)
                if (
                    before
                    and before["type"] == "file"
                    and any(file.get(k) != before.get(k) for k in ("size", "content_hash"))
                ):
                    raise MediaError("library_content_changed", uncertain=True)
                if step.get("object_type") == "folder":
                    from .directories import snapshot

                    manifest = await snapshot(self.p.provider, file, intent["destination"])
                    if manifest["digest"] != step["manifest"]["digest"]:
                        raise MediaError("directory_restore_unconfirmed", uncertain=True)
                self.remember(target, file, relative_path="/".join(loc["relative_parts"]), trashed=False)
                step["result"] = file
            step["state"] = "completed"
            self.repo.change(ident, "checking", steps=steps, submitted=False)
        if observe_only and any(step["state"] != "completed" for step in steps):
            self.repo.change(
                ident, "uncertain", steps=steps, submitted=False, error="unsubmitted_steps_remain"
            )
        else:
            self.repo.change(ident, "completed", steps=steps, submitted=False, error=None)

    async def reconcile(self, item):
        """Observe submitted steps only. Querying never dispatches remaining writes."""
        if item["state"] in {"completed", "cancelled"}:
            return self.repo.public(item)
        async with self.lock:
            current = self.repo.get(item["id"])
            if (
                current["data"].get("intent", {}).get("file_backend", "native") != "native"
                or current["data"]["intent"].get("location") == "local"
            ):
                raise MediaError("reconciliation_unsupported")
            try:
                await self.process(current, observe_only=True)
            except MediaError as exc:
                self.repo.change(
                    item["id"],
                    "uncertain",
                    error=exc.code,
                    verification_issue={"code": exc.code, "field": exc.field, "evidence": exc.evidence},
                )
            return self.repo.public(self.repo.get(item["id"]))

    async def worker(self):
        while True:
            item = self.repo.due("library")
            if not item:
                observation = self.repo.observation_due("library")
                if observation:
                    self.repo.begin_observation(observation)
                    try:
                        await self.reconcile(observation)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self.repo.change(
                            observation["id"],
                            "uncertain",
                            error="library_observation_failed",
                            due=self.repo.get(observation["id"])["due"],
                        )
                    continue
            if item:
                try:
                    async with self.lock:
                        await self.process(item)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    current = self.repo.get(item["id"])
                    code = exc.code if isinstance(exc, MediaError) else "library_internal_error"
                    state = (
                        "waiting_auth"
                        if code == "auth_required"
                        else ("uncertain" if current["data"].get("submitted") else "failed")
                    )
                    attempts = current["data"].get("attempts", 0) + 1
                    if attempts < 4 and code in {
                        "provider_network_error",
                        "provider_unavailable",
                        "sdk_worker_timeout",
                        "rate_limited",
                        "file_not_found",
                        "library_write_unconfirmed",
                    }:
                        state = "verifying" if current["data"].get("submitted") else "queued"
                    self.repo.change(
                        item["id"],
                        state,
                        error=code,
                        verification_issue={
                            "field": getattr(exc, "field", None),
                            "code": code,
                            "evidence": getattr(exc, "evidence", None),
                        },
                        attempts=attempts,
                        due=time.time() + min(30, 2**attempts),
                    )
            else:
                await asyncio.sleep(1)

    def update_local_location(self, target, old, new, trashed, restored):
        from pathlib import Path

        root = Path(self.p.settings.local_targets[target]["path"])
        rows = self.db.execute(
            "SELECT id,data FROM media_assets WHERE target=? AND json_extract(data,'$.location') IS NULL",
            (target,),
        ).fetchall()
        with self.db:
            for ident, raw in rows:
                data = json.loads(raw)
                if data.get("local_path") == str(root / (new if restored else old)):
                    data.update(local_verified=not trashed, local_path=str(root / (old if trashed else new)))
                    self.db.execute(
                        "UPDATE media_assets SET data=?,updated=? WHERE id=?",
                        (canonical(data), time.time(), ident),
                    )

    def asset(self, ident):
        row = self.db.execute(
            "SELECT target,updated,data FROM media_assets WHERE id=?", (identifier(ident),)
        ).fetchone()
        if not row:
            raise ValueError("Media asset not observed; browse its folder first")
        return {
            "asset_id": ident,
            "target": row[0],
            "observed_at": row[1],
            **json.loads(row[2]),
            "evidence_scope": "last_observation",
        }

    def collection(self, args):
        name = args.get("collection")
        if not isinstance(name, str) or not 1 <= len(name) <= 200:
            raise ValueError("Collection identity required")
        row = self.db.execute("SELECT revision,data FROM media_collections WHERE id=?", (name,)).fetchone()
        if "definition" in args:
            from .tool_contracts import COLLECTION
            from assistant_runtime.mcp.contracts import validate

            validate(args["definition"], COLLECTION, "definition")
            if row and json.loads(row[1]).get("target") != args.get("target"):
                raise ValueError("Collection belongs to another target")
            value = args["definition"]
            if (
                not isinstance(value, dict)
                or set(value) != {"title", "season", "total_episodes", "evidence", "required_state"}
                or not isinstance(value["title"], str)
                or not 1 <= len(value["title"]) <= 200
                or type(value["season"]) is not int
                or not 0 <= value["season"] <= 100000
                or (
                    value["total_episodes"] is not None
                    and (
                        type(value["total_episodes"]) is not int or not 1 <= value["total_episodes"] <= 10000
                    )
                )
                or not isinstance(value["evidence"], str)
                or not 1 <= len(value["evidence"]) <= 2000
                or value["required_state"] not in {"cloud", "local"}
            ):
                raise ValueError("Invalid collection definition or completion evidence")
            if args.get("revision") != (row[0] if row else 0):
                raise ValueError("Collection revision changed")
            revision = (row[0] if row else 0) + 1
            value = {**value, "target": args["target"]}
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO media_collections VALUES(?,?,?)",
                    (name, revision, canonical(value)),
                )
            row = (revision, canonical(value))
        definition = json.loads(row[1]) if row else None
        target = definition["target"] if definition else args.get("target")
        if args.get("target") and args["target"] != target:
            raise ValueError("Collection belongs to another target")
        if target:
            self.p.target(target)
        rows = self.db.execute(
            "SELECT id,target,data FROM media_assets WHERE json_extract(data,'$.collection')=? "
            "AND (? IS NULL OR target=?) ORDER BY id LIMIT 10001",
            (name, target, target),
        ).fetchall()
        assets = [
            (ident, data)
            for ident, scope, data in rows
            if scope in self.p.settings.targets and json.loads(data).get("binding") == self.p.target(scope)
        ]
        episodes = set()
        for _, data in assets:
            data = json.loads(data)
            if definition and data.get("season") == definition["season"]:
                if (
                    definition["required_state"] == "cloud"
                    and not data.get("trashed")
                    and data.get("present", True)
                ) or (definition["required_state"] == "local" and data.get("local_verified")):
                    episodes.add(data.get("episode"))
        total = definition and definition["total_episodes"]
        missing = sorted(set(range(1, total + 1)) - episodes) if total else None
        pending = self.db.execute(
            """SELECT count(*) FROM media_operations
            WHERE state NOT IN ('completed','failed','cancelled')
            AND json_extract(data,'$.intent.collection')=?
            AND (? IS NULL OR json_extract(data,'$.intent.target')=?)""",
            (name, target, target),
        ).fetchone()[0]
        limit = args.get("limit", 20)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Collection page limit must be 1..50")
        import base64

        before = args.get("before")
        last = ""
        if before:
            try:
                if not isinstance(before, str) or len(before) > 2048:
                    raise ValueError()
                scope, scope_target, last = json.loads(base64.urlsafe_b64decode(before))
                if scope != name or scope_target != target:
                    raise ValueError()
                identifier(last)
            except Exception:
                raise ValueError("Invalid collection cursor") from None
        page = [ident for ident, _ in assets if ident > last][: limit + 1]
        return {
            "collection": name,
            "target": target,
            "revision": row[0] if row else 0,
            "definition": definition,
            "observed_episodes": sorted(e for e in episodes if type(e) is int),
            "missing": missing,
            "coverage_complete": len(rows) <= 10000,
            "pending_operations": pending,
            "completion_candidate": bool(total and not missing and len(rows) <= 10000 and not pending),
            "evidence_scope": "recorded_observations_reverify_before_finishing",
            "items": [self.asset(ident) for ident in page[:limit]],
            "before": base64.urlsafe_b64encode(canonical([name, target, page[limit - 1]]).encode()).decode()
            if len(page) > limit
            else None,
        }
