"""Configured OpenList read access, independent of Aliyun OAuth and share auth."""

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from .errors import MediaError
from .search import json_response


class OpenList:
    def __init__(self, session, url, credentials):
        self.session, self.url, self.credentials = session, url, Path(credentials)
        self.token = None
        self.base_path = ""
        self.lock = asyncio.Lock()

    async def authenticate(self):
        try:
            credentials = await asyncio.to_thread(lambda: json.loads(self.credentials.read_text()))
        except (OSError, ValueError):
            raise MediaError("openlist_not_configured") from None
        if not isinstance(credentials, dict) or not isinstance(credentials.get("base_path", ""), str):
            raise MediaError("openlist_not_configured")
        self.base_path = credentials.get("base_path", "").rstrip("/")
        if self.base_path and (not self.base_path.startswith("/") or ".." in self.base_path.split("/")):
            raise MediaError("openlist_invalid_account_root")
        if credentials.get("token"):
            self.token = credentials["token"]
            return
        if not credentials.get("username") or not credentials.get("password"):
            raise MediaError("openlist_not_configured")
        async with self.session.post(
            self.url + "/api/auth/login",
            json={
                "username": credentials["username"],
                "password": credentials["password"],
            },
            allow_redirects=False,
        ) as response:
            body = await json_response(response, limit=65536)
        self.token = (body.get("data") or {}).get("token") if body.get("code") == 200 else None
        if not self.token:
            raise MediaError("openlist_auth_required")

    async def post(self, path, body):
        async with self.lock:
            for attempt in range(2):
                if not self.token:
                    await self.authenticate()
                request_body = dict(body)
                if self.base_path and "path" in request_body:
                    original = request_body["path"]
                    if original != self.base_path and not original.startswith(self.base_path + "/"):
                        raise MediaError("openlist_target_outside_account_root")
                    request_body["path"] = original[len(self.base_path) :] or "/"
                async with self.session.post(
                    self.url + path,
                    json=request_body,
                    headers={"Authorization": self.token},
                    allow_redirects=False,
                ) as response:
                    data = await json_response(response)
                if data.get("code") == 401:
                    self.token = None
                    if attempt == 0:
                        continue
                    raise MediaError("openlist_auth_required")
                if (
                    response.status != 200
                    or data.get("code") != 200
                    or not isinstance(data.get("data"), dict)
                ):
                    raise MediaError("openlist_unavailable")
                return data["data"]
        raise MediaError("openlist_auth_required")

    async def verify(self, target, file, *, probe=False):
        import aiohttp

        if not self.url:
            return {"state": "not_configured"}
        name = file.get("name")
        if not isinstance(name, str) or name in {".", ".."} or "/" in name or "\x00" in name:
            return {"state": "invalid_filename"}
        path = target["openlist_path"].rstrip("/") + "/" + name
        try:
            # Refresh only this bounded target page; get resolves the exact file even
            # when its parent contains more than one page.
            await self.post(
                "/api/fs/list", {"path": target["openlist_path"], "refresh": True, "page": 1, "per_page": 1}
            )
            data = await self.post("/api/fs/get", {"path": path})
            if data.get("name") != name or data.get("size") != file.get("size"):
                return {"state": "mismatch", "path": path}
            result = {"state": "visible", "path": path, "size_matches": True}
            if probe:
                raw = data.get("raw_url", "")
                url = urlsplit(raw)
                if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                    raise MediaError("openlist_invalid_link")
                async with self.session.get(raw, headers={"Range": "bytes=0-4095"}) as response:
                    header = await response.content.read(4096)
                    result["readable"] = response.status in {200, 206} and bool(header)
                    result["range_http_status"] = response.status
                    result["probe_bytes"] = len(header)
                    if str(name).lower().endswith(".mkv"):
                        result["mkv_header_valid"] = header.startswith(bytes.fromhex("1a45dfa3"))
            return result
        except MediaError as exc:
            return {"state": exc.code, "path": path}
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
            return {"state": "openlist_unavailable", "path": path}
