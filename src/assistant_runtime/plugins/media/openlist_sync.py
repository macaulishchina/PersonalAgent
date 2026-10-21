"""Durable directory refresh after confirmed cloud changes; never replay cloud writes."""

import asyncio
import copy
import json
import logging
from pathlib import PurePosixPath
import time

from .errors import MediaError
from .verification import cloud_binding

log = logging.getLogger(__name__)


def summarize(sync):
    entries = list(sync["directories"].values())
    pending = [e["due"] for e in entries if e["state"] == "pending"]
    sync["state"] = (
        "pending" if pending else "failed" if any(e["state"] == "failed" for e in entries) else "synced"
    )
    sync["due"] = min(pending) if pending else 0


def track(data, state, settings, now):
    """Runs inside operation persistence: partial successes also durably dirty directories."""
    intent = data["intent"]
    if intent.get("location") == "local" or intent.get("kind") == "download":
        return
    sync = data.get("openlist_sync")
    seen = set(sync.get("observed_steps", [])) if sync else set()
    dirty, observed = set(), set()
    if intent.get("kind") == "library":
        for index, step in enumerate(data.get("steps", [])):
            if step["state"] != "completed" or index in seen:
                continue
            observed.add(index)
            if intent.get("file_backend") == "openlist":
                for field in ("path", "destination"):
                    parent = str(PurePosixPath(step[field]).parent)
                    dirty.add("path:" + ("" if parent == "." else parent))
            else:
                before = step.get("before")
                if before:
                    dirty.add("id:" + before["parent_file_id"])
                if step.get("destination"):
                    dirty.add("id:" + step["destination"][0])
    elif state == "completed" and not sync:
        dirty.add("id:" + intent["destination"]["folder_id"])
    if not dirty:
        return
    if not sync:
        root = intent.get("root_destination", intent["destination"])
        sync = {
            "state": "pending",
            "endpoint": settings.openlist_url,
            "binding": cloud_binding(root),
            "account": dict(settings.accounts.get(root["account"], {})),
            "observed_steps": [],
            "directories": {},
        }
        data["openlist_sync"] = sync
    sync["observed_steps"] = sorted(seen | observed)
    for key in dirty:
        old = sync["directories"].get(key, {})
        sync["directories"][key] = {
            "state": "pending" if sync["endpoint"] else "failed",
            "error": None if sync["endpoint"] else "openlist_not_configured",
            "generation": old.get("generation", 0) + 1,
            "attempts": 0,
            # Do not postpone a pending directory indefinitely during a long batch.
            "due": old["due"] if old.get("state") == "pending" else now + 1,
        }
    summarize(sync)


class OpenListSync:
    def __init__(self, plugin):
        self.p, self.repo = plugin, plugin.repo
        self.lock = asyncio.Lock()

    def retry(self, ident):
        item = self.repo.get(ident)
        sync = copy.deepcopy(item["data"].get("openlist_sync"))
        if not sync:
            # Explicit verification may enqueue older completed operations too.
            track(item["data"], item["state"], self.p.settings, time.time())
            sync = item["data"].get("openlist_sync")
        if sync:
            sync["endpoint"] = self.p.settings.openlist_url
            for entry in sync["directories"].values():
                entry.update(
                    state="pending",
                    attempts=0,
                    error=None,
                    due=time.time(),
                    generation=entry["generation"] + 1,
                )
            summarize(sync)
            self.repo.change(ident, item["state"], due=item["due"], openlist_sync=sync)

    async def refresh(self, item, key):
        from .openlist_files import OpenListFiles

        intent, sync = item["data"]["intent"], item["data"]["openlist_sync"]
        target = self.p.target(intent["target"])
        if (
            sync["endpoint"] != self.p.settings.openlist_url
            or sync["binding"] != cloud_binding(target)
            or sync["account"] != self.p.settings.accounts[target["account"]]
        ):
            raise MediaError("target_configuration_changed")
        if not sync["endpoint"]:
            raise MediaError("openlist_not_configured")
        if key.startswith("id:"):
            destination, _ = await self.p.library.destination(intent["target"], key[3:])
            path = destination["openlist_path"]
        else:
            path = OpenListFiles(self.p.openlist, target["openlist_path"]).path(key[5:])
        # A single refreshed page invalidates the directory cache, regardless of its size.
        result = await self.p.openlist.post(
            "/api/fs/list", {"path": path, "refresh": True, "page": 1, "per_page": 1}, scope=True
        )
        total, content = result.get("total"), result.get("content")
        if (
            type(total) is not int
            or total < 0
            or (content is not None and not isinstance(content, list))
            or len(content or []) != min(1, total)
        ):
            raise MediaError("openlist_incomplete_listing")
        return path

    async def run_once(self):
        async with self.lock:
            now = time.time()
            rows = self.repo.db.execute(
                "SELECT id FROM media_operations WHERE json_extract(data,'$.openlist_sync.state')='pending' "
                "AND json_extract(data,'$.openlist_sync.due')<=? ORDER BY json_extract(data,'$.openlist_sync.due'),id LIMIT 64",
                (now,),
            ).fetchall()
            groups = {}
            for (ident,) in rows:
                item = self.repo.get(ident)
                sync = item["data"]["openlist_sync"]
                for key, entry in sync["directories"].items():
                    if entry["state"] != "pending" or entry["due"] > now:
                        continue
                    group = json.dumps(
                        [
                            item["data"]["intent"]["target"],
                            sync["endpoint"],
                            sync["binding"],
                            sync["account"],
                            key,
                        ],
                        sort_keys=True,
                    )
                    if group not in groups and len(groups) >= 32:
                        continue
                    groups.setdefault(group, []).append((item, key, entry["generation"]))
            for members in groups.values():
                item, key, _ = members[0]
                error, path = None, None
                try:
                    path = await self.refresh(item, key)
                except Exception as exc:
                    error = exc.code if isinstance(exc, MediaError) else "openlist_sync_unavailable"
                for snapshot, key, generation in members:
                    current = self.repo.get(snapshot["id"])
                    sync = current["data"]["openlist_sync"]
                    entry = sync["directories"][key]
                    if entry["generation"] != generation:
                        continue  # A later confirmed change still needs a fresh observation.
                    attempts = entry["attempts"] + 1
                    entry.update(
                        state="synced" if error is None else "failed" if attempts >= 8 else "pending",
                        error=error,
                        attempts=attempts,
                        observed_at=time.time(),
                        due=time.time() + min(300, 2**attempts),
                    )
                    if path is not None:
                        entry["path"] = path
                    summarize(sync)
                    self.repo.change(current["id"], current["state"], due=current["due"], openlist_sync=sync)
                    if (
                        sync["state"] == "synced"
                        and current["data"]["intent"].get("kind", "transfer") == "transfer"
                    ):
                        await self.verify_transfer(current)
                    log.info(
                        "OpenList sync operation=%s state=%s error=%s attempts=%d",
                        current["id"],
                        entry["state"],
                        error,
                        attempts,
                    )
            return len(groups)

    async def verify_transfer(self, item):
        # Keep existing post-transfer file visibility evidence, off the acquisition
        # worker and without forcing the same directory refresh a second time.
        intent = item["data"]["intent"]
        try:
            target, _ = await self.p.library.destination(intent["target"], intent["destination"]["folder_id"])
            result = await self.p.openlist.verify(target, item["data"]["file"], refresh=False)
        except Exception:
            result = {"state": "openlist_unavailable"}
        current = self.repo.get(item["id"])
        self.repo.change(item["id"], current["state"], due=current["due"], openlist=result)

    async def worker(self):
        while True:
            try:
                await self.run_once()
            except Exception:
                # Fault isolation: never take acquisition/voice down with the refresh worker.
                log.error("OpenList sync worker failed; pending receipts retained")
            await asyncio.sleep(1)
