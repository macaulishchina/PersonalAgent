"""Bounded OpenList transport with separate credentials and an explicit API allowlist."""

import asyncio
import json
import time
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
        self.scope_checked = False

    async def authenticate(self):
        self.scope_checked = False
        try:
            credentials = await asyncio.to_thread(
                lambda: json.loads(self.credentials.read_text(encoding="utf-8"))
            )
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
        data = body.get("data")
        self.token = (
            data.get("token")
            if response.status == 200 and body.get("code") == 200 and isinstance(data, dict)
            else None
        )
        if not isinstance(self.token, str) or not self.token:
            self.token = None
            raise MediaError("auth_required")

    async def post(self, path, body, *, write=False, scope=False):
        import aiohttp

        allowed = {"/api/fs/list", "/api/fs/get"}
        mutations = {"/api/fs/mkdir", "/api/fs/rename", "/api/fs/move"}
        if path not in (mutations if write else allowed):
            raise MediaError("openlist_action_unsupported")
        try:
            return await self._post(path, body, write=write, scope=scope)
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
            raise MediaError("provider_network_error", uncertain=write) from None
        except MediaError as exc:
            if write and exc.code in {"service_response_limit", "service_invalid_response"}:
                raise MediaError(exc.code, uncertain=True) from None
            raise

    async def check_scope(self):
        async with self.session.get(
            self.url + "/api/me", headers={"Authorization": self.token}, allow_redirects=False
        ) as response:
            body = await json_response(response, limit=65536)
        if response.status == 401 or body.get("code") == 401:
            self.token = None
            raise MediaError("auth_required")
        data = body.get("data")
        if response.status != 200 or body.get("code") != 200 or not isinstance(data, dict):
            raise MediaError("openlist_account_unavailable")
        if not isinstance(data.get("base_path"), str) or data["base_path"].rstrip("/") != self.base_path:
            raise MediaError("openlist_account_root_changed")
        self.scope_checked = True

    async def _post(self, path, body, *, write, scope):
        async with self.lock:
            for attempt in range(2):
                if not self.token:
                    await self.authenticate()
                if scope and not self.scope_checked:
                    try:
                        await self.check_scope()
                    except MediaError as exc:
                        if exc.code == "auth_required" and attempt == 0:
                            continue  # No filesystem request has been submitted yet.
                        raise
                request_body = dict(body)
                for field in ("path", "src_dir", "dst_dir"):
                    if field not in request_body:
                        continue
                    original = request_body[field]
                    if (
                        self.base_path
                        and original != self.base_path
                        and not original.startswith(self.base_path + "/")
                    ):
                        raise MediaError("openlist_target_outside_account_root")
                    request_body[field] = original[len(self.base_path) :] or "/"
                async with self.session.post(
                    self.url + path,
                    json=request_body,
                    headers={"Authorization": self.token},
                    allow_redirects=False,
                ) as response:
                    data = await json_response(response)
                if response.status == 401 or data.get("code") == 401:
                    self.token = None
                    self.scope_checked = False
                    if attempt == 0 and not write:
                        continue
                    raise MediaError("auth_required")
                if response.status == 403 or data.get("code") == 403:
                    raise MediaError("permission_denied")
                if response.status != 200 or data.get("code") != 200:
                    raise MediaError("openlist_unavailable", uncertain=write)
                value = data.get("data")
                if value is None and write:
                    return {}
                if not isinstance(value, dict):
                    raise MediaError("service_invalid_response", uncertain=write)
                return value
        raise MediaError("openlist_auth_required")

    async def verify(self, target, file, *, probe=False, refresh=True):
        import aiohttp

        if not self.url:
            return {"state": "not_configured"}
        name = file.get("name")
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or any(c in name for c in ("/", "\\", "\x00"))
        ):
            return {"state": "invalid_filename"}
        path = target["openlist_path"].rstrip("/") + "/" + name
        unverified = {"configured_path": path, "path_verified": False, "observed_at": time.time()}
        try:
            # Refresh only this bounded target page; get resolves the exact file even
            # when its parent contains more than one page.
            await self.post(
                "/api/fs/list",
                {"path": target["openlist_path"], "refresh": refresh, "page": 1, "per_page": 1},
                scope=True,
            )
            data = await self.post("/api/fs/get", {"path": path}, scope=True)
            if (
                data.get("is_dir") is not False
                or data.get("name") != name
                or data.get("size") != file.get("size")
            ):
                return {"state": "mismatch", **unverified}
            hashes = data.get("hash_info") or {}
            algorithm = file.get("content_hash_name") or ""
            observed_hash = (
                (hashes.get(algorithm) or hashes.get(algorithm.upper())) if isinstance(hashes, dict) else None
            )
            if observed_hash is not None and not isinstance(observed_hash, str):
                return {"state": "invalid_metadata", **unverified}
            if (
                file.get("content_hash")
                and observed_hash
                and observed_hash.lower() != file["content_hash"].lower()
            ):
                return {"state": "mismatch", **unverified}
            result = {
                "state": "visible",
                "path": path,
                "path_verified": True,
                "path_scope": "openlist_mount",
                "observed_at": time.time(),
                "size_matches": True,
                "evidence": "content_hash" if observed_hash and file.get("content_hash") else "name_size",
            }
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
            return {"state": exc.code, **unverified}
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
            return {"state": "openlist_unavailable", **unverified}
