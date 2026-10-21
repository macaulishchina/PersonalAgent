"""OpenList path plans on the existing media queue; no second execution engine."""

from pathlib import PurePosixPath
import hashlib

from .errors import MediaError
from .library import filename, relative
from .openlist_files import OpenListFiles


class OpenListLibrary:
    def __init__(self, library):
        self.library, self.p, self.repo = library, library.p, library.repo

    def binding(self, target):
        root = self.p.target(target)
        if self.p.settings.files_for(root["account"]) != "openlist":
            raise MediaError("target_configuration_changed")
        if not self.p.settings.openlist_url:
            raise MediaError("openlist_not_configured")
        return {
            "backend": "openlist",
            "url": self.p.settings.openlist_url,
            "account": dict(self.p.settings.accounts[root["account"]]),
            "target": dict(root),
        }

    def files(self, target):
        self.binding(target)
        return OpenListFiles(self.p.openlist, self.p.target(target)["openlist_path"])

    def busy(self, target, *, except_operation=""):
        if self.repo.db.execute(
            """SELECT 1 FROM media_operations WHERE id!=? AND state NOT IN
            ('completed','failed','cancelled') AND json_extract(data,'$.intent.target')=?
            AND json_extract(data,'$.intent.kind') IN ('download','library') LIMIT 1""",
            (except_operation, target),
        ).fetchone():
            raise MediaError("media_file_busy")

    async def listing(self, target, path="", marker=""):
        result = await self.files(target).listing(path, marker)
        for file in result["items"]:
            self.library.remember(
                target, file, backend="openlist", present=True, superseded_by=None, trashed=False
            )
        return {**result, "backend": "openlist", "addressing": "relative_path"}

    async def preview(self, args, owner):
        target = args["target"]
        binding = self.binding(target)
        files = self.files(target)
        self.busy(target)
        changes = args.get("changes")
        if not isinstance(changes, list) or not 1 <= len(changes) <= 100:
            raise ValueError("A plan requires 1..100 changes")
        steps, touched = [], set()
        for change in changes:
            fields = {
                "mkdir": {"action", "path"},
                "rename": {"action", "path", "name"},
                "move": {"action", "path", "folder"},
            }
            if not isinstance(change, dict) or not isinstance(change.get("action"), str):
                raise ValueError("Invalid OpenList change")
            action = change["action"]
            if action not in fields or set(change) != fields[action]:
                raise ValueError("OpenList supports mkdir(path), rename(path,name), move(path,folder)")
            source = change["path"]
            if not relative(source):
                raise ValueError("Cannot modify a configured root")
            files.path(source)
            before = None if action == "mkdir" else await files.stat(source)
            if action != "mkdir" and (not before or before["type"] != "file"):
                raise MediaError("source_must_be_a_file")
            if action == "rename":
                name = filename(change["name"])
                if PurePosixPath(name).suffix.lower() != PurePosixPath(source).suffix.lower():
                    raise ValueError("Renaming must preserve the file extension")
                destination = str(PurePosixPath(source).with_name(name))
            elif action == "move":
                relative(change["folder"])
                destination = "/".join(filter(None, (change["folder"], PurePosixPath(source).name)))
            else:
                destination = source
            files.path(destination)
            parent = str(PurePosixPath(destination).parent)
            parent = "" if parent == "." else parent
            folder = await files.stat(parent)
            if not folder or folder["type"] != "folder":
                raise MediaError("destination_not_a_folder")
            await files.absent(destination)
            paths = {source, destination}
            if touched & paths:
                raise ValueError("Overlapping changes must use separate plans")
            touched.update(paths)
            steps.append(
                {
                    "action": action,
                    "path": source,
                    "destination": destination,
                    "before": before,
                    "state": "pending",
                }
            )
        return self.library.save_plan(
            owner,
            {
                "target": target,
                "root": self.p.target(target),
                "file_backend": "openlist",
                "backend_binding": binding,
                "steps": steps,
            },
        )

    async def process(self, item):
        ident, intent = item["id"], item["data"]["intent"]
        target = intent["target"]
        if self.binding(target) != intent["backend_binding"]:
            raise MediaError("target_configuration_changed")
        files, steps = self.files(target), item["data"]["steps"]
        # Downloads wait; two queued library plans are serialized by Library.lock.
        if self.repo.db.execute(
            """SELECT 1 FROM media_operations WHERE state NOT IN ('completed','failed','cancelled')
            AND json_extract(data,'$.intent.target')=? AND json_extract(data,'$.intent.kind')='download'""",
            (target,),
        ).fetchone():
            raise MediaError("media_file_busy")
        for step in steps:
            if step["state"] == "completed":
                continue
            current = self.repo.get(ident)
            if current["data"].get("cancel_requested") and step["state"] != "submitted":
                self.repo.change(ident, "cancelled", steps=steps, submitted=False)
                return
            action, before = step["action"], step["before"]
            if step["state"] != "submitted":
                if before != (None if action == "mkdir" else await files.stat(step["path"])):
                    raise MediaError("library_precondition_changed")
                await files.absent(step["destination"])
                step["state"] = "submitted"
                self.repo.change(ident, "submitting", steps=steps, submitted=True)
                try:
                    step["receipt"] = await files.change(action, step["path"], step["destination"])
                except MediaError as exc:
                    if not exc.uncertain:
                        step["state"] = "pending"
                        self.repo.change(ident, "checking", steps=steps, submitted=False)
                    raise
                self.repo.change(ident, "verifying", steps=steps)
            actual = await files.stat(step["destination"])
            if not actual:
                raise MediaError("library_write_unconfirmed", uncertain=True)
            acknowledged = step.get("receipt", {}).get("acknowledged", False)
            if action == "mkdir":
                if not acknowledged or actual["type"] != "folder":
                    raise MediaError("mkdir_identity_unconfirmed", uncertain=True)
            else:
                source = await files.stat(step["path"])
                if source or files.fingerprint(actual) != files.fingerprint(before):
                    raise MediaError("library_write_unconfirmed", uncertain=True)
                if not acknowledged and not before.get("content_hash"):
                    raise MediaError("library_write_unconfirmed", uncertain=True)
            # Evidence is the requested filesystem state, not a stable cloud ID.
            step.update(state="completed", result=actual, evidence="observed_path_state")
            if before:
                self.library.remember(
                    target,
                    before,
                    backend="openlist",
                    present=False,
                    superseded_by=self.library.asset_id(target, actual["file_id"]),
                )
            self.library.remember(
                target, actual, backend="openlist", present=True, superseded_by=None, trashed=False
            )
            self.repo.change(ident, "checking", steps=steps, submitted=False)
        self.repo.change(ident, "completed", steps=steps, submitted=False, error=None)

    async def enqueue_download(self, args, owner, caller):
        target = args["target"]
        binding = self.binding(target)
        if args.get("file_id") is not None:
            raise ValueError("OpenList downloads use path, not provider file_id")
        path = args.get("path")
        if not relative(path):
            raise ValueError("A file path is required")
        if target not in self.p.settings.local_targets:
            raise ValueError("No local download destination configured for this target")
        intent = {
            "kind": "download",
            "target": target,
            "destination": self.p.target(target),
            "file_id": "ol_" + hashlib.sha256(self.files(target).path(path).encode()).hexdigest()[:32],
            "path": path,
            "file_backend": "openlist",
            "backend_binding": binding,
            "relative_parts": relative(path)[:-1],
            "local_destination": dict(self.p.settings.local_targets[target]),
        }
        if self.repo.db.execute(
            "SELECT 1 FROM media_operations WHERE request_id=?", (args.get("request_id"),)
        ).fetchone():
            return self.repo.public(self.repo.create(args.get("request_id"), intent, owner, caller))
        if self.repo.db.execute(
            """SELECT 1 FROM media_operations WHERE state NOT IN ('completed','failed','cancelled')
            AND json_extract(data,'$.intent.target')=? AND json_extract(data,'$.intent.kind')='library'""",
            (target,),
        ).fetchone():
            raise MediaError("media_file_busy")
        file = (await self.files(target).download(path))["file"]
        if file["category"] != "video":
            raise MediaError("source_must_be_a_verifiable_video_file")
        item = self.repo.create(args.get("request_id"), intent, owner, caller, initial={"file": file})
        return self.repo.public(item)
