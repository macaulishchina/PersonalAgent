"""Reusable, scoped OpenList filesystem port. Paths are not provider object IDs.

No scheduler, cloud credentials, arbitrary endpoint or implicit native fallback.
A directory listing establishes presence/absence; raw get errors never mean absence.
"""

import hashlib
import re
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode

from .errors import MediaError
from .library import filename, relative
from .repository import canonical

ACTIONS = ("list", "stat", "mkdir", "rename", "move", "download")


class OpenListFiles:
    def __init__(self, client, root):
        self.client = client
        self.root = root.rstrip("/")
        if not self.root or not self.root.startswith("/"):
            raise ValueError("OpenList filesystem targets require an explicit mounted subdirectory")
        relative(self.root[1:])
        if any("%" in p or p.startswith("@") for p in self.root[1:].split("/")):
            raise ValueError("Unsupported OpenList root")

    async def request(self, endpoint, body, *, write=False):
        return await self.client.post(endpoint, body, write=write, scope=True)

    def path(self, path):
        parts = relative(path)
        # Reject ambiguous encodings and OpenList's special sharing namespace.
        if any("%" in p or p.startswith("@") for p in parts):
            raise ValueError("Unsupported OpenList path component")
        return self.root + ("/" + "/".join(parts) if parts else "") or "/"

    def normalize(self, item, path):
        name = filename(item.get("name"))
        if (
            type(item.get("is_dir")) is not bool
            or type(item.get("size")) is not int
            or item["size"] < 0
            or not isinstance(item.get("modified"), str)
            or len(item["modified"]) > 100
            or PurePosixPath(path).name != name
        ):
            raise MediaError("openlist_invalid_metadata")
        result = {
            "file_id": "ol_" + hashlib.sha256(self.path(path).encode()).hexdigest()[:32],
            "identity_kind": "path",
            "name": name,
            "type": "folder" if item["is_dir"] else "file",
            "size": item["size"],
            "modified": item["modified"],
            "relative_path": path,
            "category": "video"
            if PurePosixPath(name).suffix.lower()
            in {".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".ts", ".m2ts"}
            else "other",
        }
        hashes = item.get("hash_info") or {}
        if not isinstance(hashes, dict):
            raise MediaError("openlist_invalid_metadata")
        for algorithm, width in (("sha256", 64), ("sha1", 40), ("md5", 32)):
            value = hashes.get(algorithm) or hashes.get(algorithm.upper())
            if value is not None:
                if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{%d}" % width, value):
                    raise MediaError("openlist_invalid_checksum")
                result.update(content_hash_name=algorithm, content_hash=value.lower())
                break
        return result

    async def listing(self, path="", marker="", *, refresh=False):
        absolute = self.path(path)
        scope = hashlib.sha256(absolute.encode()).hexdigest()[:16]
        page = 1
        if marker:
            if not isinstance(marker, str) or not re.fullmatch(scope + r":[1-9][0-9]{0,3}", marker):
                raise ValueError("Invalid OpenList listing cursor")
            page = int(marker.split(":")[1])
        if page > 1000:
            raise MediaError("openlist_listing_limit")
        data = await self.request(
            "/api/fs/list",
            {
                "path": absolute,
                "page": page,
                "per_page": 100,
                "refresh": refresh,
            },
        )
        content, total = data.get("content"), data.get("total")
        if content is None and total == 0:
            content = []
        if (
            not isinstance(content, list)
            or len(content) > 100
            or type(total) is not int
            or total < 0
            or len(content) != min(100, max(0, total - (page - 1) * 100))
        ):
            raise MediaError("openlist_incomplete_listing")
        items = []
        for row in content:
            if not isinstance(row, dict):
                raise MediaError("openlist_invalid_metadata")
            name = filename(row.get("name"))
            child = "/".join(filter(None, (path, name)))
            items.append(self.normalize(row, child))
        if len({i["name"] for i in items}) != len(items):
            raise MediaError("openlist_incomplete_listing")
        more = page * 100 < total
        return {
            "items": items,
            "marker": f"{scope}:{page + 1}" if more else None,
            "complete": not more,
            "coverage": {"relative_path": path, "recursive": False, "consistency": "live_pages_not_snapshot"},
        }

    async def stat(self, path, *, refresh=True):
        self.path(path)
        if not path:
            await self.listing(refresh=refresh)
            return {"type": "folder", "relative_path": ""}
        parent = str(PurePosixPath(path).parent)
        parent = "" if parent == "." else parent
        marker = ""
        for _ in range(10):
            page = await self.listing(parent, marker, refresh=refresh)
            found = [f for f in page["items"] if f["relative_path"] == path]
            if found:
                return found[0]
            marker = page["marker"]
            if not marker:
                return None
        raise MediaError("destination_listing_limit")

    async def absent(self, path):
        if await self.stat(path) is not None:
            raise MediaError("destination_name_conflict")

    async def change(self, action, source, destination):
        if action == "mkdir":
            result = await self.request("/api/fs/mkdir", {"path": self.path(destination)}, write=True)
        elif action == "rename":
            result = await self.request(
                "/api/fs/rename",
                {
                    "path": self.path(source),
                    "name": filename(PurePosixPath(destination).name),
                    "overwrite": False,
                },
                write=True,
            )
        elif action == "move":
            src, dst = PurePosixPath(source), PurePosixPath(destination)
            result = await self.request(
                "/api/fs/move",
                {
                    "src_dir": self.path("" if str(src.parent) == "." else str(src.parent)),
                    "dst_dir": self.path("" if str(dst.parent) == "." else str(dst.parent)),
                    "names": [filename(src.name)],
                    "overwrite": False,
                    "skip_existing": False,
                    "merge": False,
                },
                write=True,
            )
        else:
            raise MediaError("openlist_action_unsupported")
        # Do not persist upstream messages, URLs or arbitrary task metadata.
        tasks = result.get("tasks", [])
        if not isinstance(tasks, list) or len(tasks) > 10:
            raise MediaError("service_invalid_response", uncertain=True)
        ids = [t.get("id") for t in tasks if isinstance(t, dict)]
        if len(ids) != len(tasks) or any(not isinstance(i, str) or len(i) > 200 for i in ids):
            raise MediaError("service_invalid_response", uncertain=True)
        return {"acknowledged": True, "task_ids": ids}

    async def download(self, path):
        before = await self.stat(path, refresh=False)
        if not before or before["type"] != "file":
            raise MediaError("file_not_found")
        data = await self.request("/api/fs/get", {"path": self.path(path)})
        file = self.normalize(data, path)
        if any(file[k] != before[k] for k in ("name", "size", "modified", "type")):
            raise MediaError("download_source_changed")
        if not file.get("content_hash"):
            raise MediaError("download_checksum_unavailable")
        sign = data.get("sign", "")
        if not isinstance(sign, str) or len(sign) > 2000 or any(ord(c) < 32 for c in sign):
            raise MediaError("openlist_invalid_metadata")
        # Construct the same-origin proxy path ourselves. Ignore raw_url, site_url,
        # cloud Cookies and arbitrary redirects. The proxy uses the mount's credentials.
        url = self.client.url + "/p" + quote(self.path(path), safe="/")
        if sign:
            url += "?" + urlencode({"sign": sign})
        return {"file": file, "url": url, "headers": {}}

    def fingerprint(self, file):
        # Rename/move may update modified on the upstream drive. It remains a
        # submission precondition, but is not an invariant of the resulting file.
        # Lost acknowledgments separately require a matching content hash.
        return canonical(
            {
                k: file.get(k)
                for k in (
                    "type",
                    "size",
                    "content_hash_name",
                    "content_hash",
                )
            }
        )
