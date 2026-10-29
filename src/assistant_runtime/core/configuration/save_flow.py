"""Operator save workflow. Restart proposals never become startup configuration."""

import asyncio
import json
import uuid

from .files import Conflict, atomic, redact
from .fields import declared_fields, redact_declared, rule_for


def overlaps(left, right):
    return left[: len(right)] == right or right[: len(left)] == left


class SaveFlow:
    def __init__(self, business):
        self.business = business
        self.files = business.files
        self.path = self.files.state / "restart-draft.json"

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else None

    def check(self, ident):
        draft = self.read()
        if (draft or {}).get("id") != ident:
            raise Conflict("Restart proposal changed; reload before saving")
        return draft

    def public(self):
        draft = self.read()
        if not draft:
            return None
        fields = declared_fields()
        patches = []
        for patch in draft["patches"]:
            value = patch.get("value")
            safe = redact(redact_declared(value, rule_for(patch["path"], fields)))
            if safe == value:
                patches.append(patch)
        return {
            "id": draft["id"],
            "patches": patches,
            "count": len(draft["patches"]) + len(draft["resources"]),
            "opaque_count": len(draft["patches"]) - len(patches) + len(draft["resources"]),
            "labels": [".".join(p["path"]) for p in draft["patches"]]
            + ["resource:" + p["id"] for p in draft["resources"]],
        }

    def persist(self, version, patches, resources):
        if not patches and not resources:
            self.path.unlink(missing_ok=True)
            return
        atomic(
            self.path,
            json.dumps(
                {"id": uuid.uuid4().hex, "version": version, "patches": patches, "resources": resources}
            ).encode(),
        )

    def discard(self, ident):
        self.check(ident)
        self.path.unlink(missing_ok=True)

    async def plan(self, candidate):
        running = self.business.process is not None and self.business.process.returncode is None
        if not running:
            return {"fields": [], "restart": []}
        entry = await asyncio.to_thread(self.business.freeze, candidate)
        before = self.business.loaded_snapshot
        resources = (
            [
                key
                for key in before.resources.keys() | candidate.resources.keys()
                if before.resources.get(key) != candidate.resources.get(key)
            ]
            if before
            else []
        )
        return await self.business.request(
            {
                "command": "configuration-update",
                "preview": True,
                "entry": str(entry),
                "version": candidate.version,
                "resources": resources,
            }
        )

    async def save(self, version, patches, resources, pending_id=None):
        draft = self.check(pending_id)
        patches = [] if patches is None else patches
        resources = [] if resources is None else resources
        if (
            not isinstance(patches, list)
            or len(patches) > 512
            or any(not isinstance(p, dict) for p in patches)
        ):
            raise ValueError("Expected bounded configuration patches")
        if (
            not isinstance(resources, list)
            or len(resources) > 128
            or any(not isinstance(p, dict) for p in resources)
        ):
            raise ValueError("Expected bounded resource changes")
        if draft:
            if draft["version"] != version:
                raise Conflict(
                    "Configuration changed after the restart proposal; discard the proposal and edit again"
                )
            # Visible edits are submitted by the page. Keep only opaque operations
            # server-side; credentials are never sent back to the browser.
            visible = {tuple(p["path"]) for p in self.public()["patches"]}
            patches = [
                p
                for p in draft["patches"]
                if tuple(p["path"]) not in visible
                and not any(
                    overlaps(p["path"], n.get("path", [])) for n in patches if isinstance(n.get("path"), list)
                )
            ] + patches
            replaced = {(p.get("id"), p.get("key")) for p in resources}
            resources = [
                p for p in draft["resources"] if (p.get("id"), p.get("key")) not in replaced
            ] + resources
        candidate = await asyncio.to_thread(
            self.files.patch, version, patches, resource_changes=resources, _preview=True
        )
        plan = await self.plan(candidate)
        restart = [p["path"] for p in plan["fields"] if p["mode"] == "restart"]
        hot, deferred = [], []
        for patch in patches:
            (deferred if any(overlaps(patch["path"], path) for path in restart) else hot).append(patch)
        cold_resources = [p for p in resources if "resource:" + p["id"] in plan["restart"]]
        hot_resources = [p for p in resources if p not in cold_resources]
        try:
            partial = await asyncio.to_thread(
                self.files.patch, version, hot, resource_changes=hot_resources, _preview=True
            )
            partial_plan = await self.plan(partial) if deferred or cold_resources else plan
            if partial_plan["restart"]:
                raise ValueError("Dependent settings require one restart")
        except ValueError:
            # Never apply half of a dependency-coupled configuration.
            hot, hot_resources, deferred, cold_resources = [], [], patches, resources
        self.persist(version, deferred, cold_resources)
        result = await asyncio.to_thread(self.files.patch, version, hot, resource_changes=hot_resources)
        if deferred or cold_resources:
            self.persist(result["version"], deferred, cold_resources)
        if result.get("changed") or self.business.loaded != result["version"]:
            result["application"] = self.business.update(result["version"])
        result["pending_restart"] = self.public()
        return result

    async def restart(self, version, ident):
        draft = self.check(ident)
        if not draft or draft["version"] != version:
            raise Conflict("Reload and review the pending changes before restarting")
        result = await asyncio.to_thread(
            self.files.patch, version, draft["patches"], resource_changes=draft["resources"]
        )
        # Retain the proposal if restart acceptance fails. Once accepted, the
        # existing manager owns its immutable snapshot and failure/recovery record.
        self.persist(result["version"], draft["patches"], draft["resources"])
        result["application"] = self.business.apply(result["version"], "force", True)
        self.path.unlink(missing_ok=True)
        return result
