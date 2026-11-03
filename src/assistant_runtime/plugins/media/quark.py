"""Bounded Quark share protocol. Cookie ownership is separate from OpenList.

Only the adapter sees cookies/stokens/fid tokens. Copy returns a durable provider
task receipt immediately; the shared operation worker owns subsequent observation.
"""

import asyncio
from assistant_runtime.api.system import system
import hashlib
from http.cookies import SimpleCookie
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import aiohttp

from .errors import MediaError
from .private import disk, save
from .search import json_response
from .settings import identifier

BASE = "https://drive-pc.quark.cn/1/clouddrive"
ENDPOINTS = {
    ("GET", "/member"),
    ("GET", "/file/info"),
    ("POST", "/file"),
    ("POST", "/file/download"),
    ("POST", "/file/rename"),
    ("POST", "/file/move"),
    ("POST", "/file/delete"),
    ("GET", "/file/recycle/list"),
    ("POST", "/file/recycle/recover"),
    ("GET", "/file/sort"),
    ("POST", "/share/sharepage/token"),
    ("GET", "/share/sharepage/detail"),
    ("POST", "/share/sharepage/save"),
    ("GET", "/task"),
}
WRITES = {"copy", "mkdir", "rename", "move", "trash", "restore"}
WRITE_ENDPOINTS = {
    "/share/sharepage/save",
    "/file",
    "/file/rename",
    "/file/move",
    "/file/delete",
    "/file/recycle/recover",
}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) quark-cloud-drive/3.14.2 Chrome/112.0.5615.165 "
    "Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch"
)


def file_record(row):
    if not isinstance(row, dict):
        raise MediaError("provider_invalid_response")
    fid, name, size = row.get("fid"), row.get("file_name"), row.get("size")
    if not isinstance(name, str) or not 1 <= len(name) <= 1000 or type(size) is not int or size < 0:
        raise MediaError("provider_invalid_response")
    try:
        identifier(fid)
        parent = identifier(row.get("pdir_fid", "0"))
    except ValueError:
        raise MediaError("provider_invalid_response") from None
    folder = row.get("dir") is True or row.get("file_type") == 0
    return {
        "file_id": fid,
        "name": name,
        "size": size,
        "parent_file_id": "root" if parent == "0" else parent,
        "type": "folder" if folder else "file",
        "category": "video" if not folder and row.get("category") == 1 else "other",
    }


class Quark:
    def __init__(self, root, account_changed, *, account):
        self.credentials = Path(root) / "credentials" / account
        self.credentials.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.account_changed = account_changed
        self.lock = asyncio.Lock()
        self.lease = None
        self.session = None
        self.closed = False
        self.cookies = {}

    def claim(self):
        if self.closed:
            raise MediaError("media_stopping")
        if self.lease is None:
            lease = (self.credentials / "owner.lock").open("a")
            system.private(lease.name)
            try:
                system.lock(lease, blocking=False)
            except OSError:
                lease.close()
                raise MediaError("media_credentials_already_owned") from None
            self.lease = lease

    async def close(self):
        self.closed = True
        if self.session is not None:
            await self.session.close()
        if self.lease is not None:
            self.lease.close()

    async def load_auth(self):
        try:
            value = await disk(
                lambda: json.loads((self.credentials / "account.json").read_text(encoding="utf-8"))
            )
            cookie = value["cookie"]
            if not isinstance(cookie, str) or len(cookie) > 32768 or "\n" in cookie or "\r" in cookie:
                raise ValueError()
            parsed = SimpleCookie()
            parsed.load(cookie)
            self.cookies = {k: v.value for k, v in parsed.items()}
            uid = self.cookies.get("__uid")
            if not uid:
                raise ValueError()
            identity = hashlib.sha256(uid.encode()).hexdigest()
            path = self.credentials / "identity.json"
            bound = await disk(
                lambda: json.loads(path.read_text(encoding="utf-8"))["identity"] if path.exists() else None
            )
            if bound and bound != identity:
                raise MediaError("account_mismatch")
        except (OSError, ValueError, KeyError, TypeError):
            raise MediaError("auth_required") from None
        # A cookie field alone does not establish login. Verify before binding/writing.
        member = (await self.request("/member"))["data"]
        if not isinstance(member, dict) or type(member.get("total_capacity")) is not int:
            raise MediaError("provider_invalid_response")
        if not bound:
            await disk(save, path, {"identity": identity})
        return member

    async def request(self, path, *, method="GET", params=None, body=None):
        if (method, path) not in ENDPOINTS:
            raise MediaError("provider_action_unsupported")
        write = method == "POST" and path in WRITE_ENDPOINTS
        self.requests += 1
        if self.requests > 24:
            raise MediaError("provider_request_limit")
        try:
            async with self.session.request(
                method,
                BASE + path,
                params={"pr": "ucpro", "fr": "pc", **(params or {})},
                json=body,
                headers={
                    "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
                    "Referer": "https://pan.quark.cn/",
                    "User-Agent": USER_AGENT,
                },
                allow_redirects=False,
            ) as response:
                if response.status == 401:
                    raise MediaError("auth_required")
                if response.status == 429:
                    raise MediaError("rate_limited")
                if response.status == 404 and path == "/file/info":
                    raise MediaError("file_not_found")
                if response.status != 200:
                    raise MediaError("provider_unavailable", uncertain=write)
                result = await json_response(response, limit=1024**2)
                # Never forward raw upstream errors, which can contain credentials/URLs.
                code = result.get("code")
                if code in {401, 40101, 31001}:
                    raise MediaError("auth_required")
                if code != 0:
                    raise MediaError("provider_request_rejected", uncertain=write)
                shape = list if path == "/file/download" else dict
                if not isinstance(result.get("data"), shape):
                    raise MediaError("provider_invalid_response", uncertain=write)
                changed = False
                for key in ("__puus", "__pus"):
                    if key in response.cookies and response.cookies[key].value != self.cookies.get(key):
                        self.cookies[key] = response.cookies[key].value
                        changed = True
                if changed:
                    await disk(
                        save,
                        self.credentials / "account.json",
                        {"cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())},
                    )
                return result
        except MediaError as exc:
            if write and exc.code in {"service_response_limit", "service_invalid_response"}:
                raise MediaError(exc.code, uncertain=True) from None
            raise
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
            raise MediaError("provider_network_error", uncertain=write) from None

    async def page(self, parent, marker="", *, share=None):
        if marker and (not isinstance(marker, str) or not re.fullmatch(r"[1-9][0-9]{0,3}", marker)):
            raise ValueError("Invalid Quark page cursor")
        page = int(marker or "1")
        result = await self.request(
            "/share/sharepage/detail" if share else "/file/sort",
            params={
                "pdir_fid": "0" if parent == "root" else identifier(parent),
                "_page": page,
                "_size": 50,
                "_fetch_total": 1,
                "_sort": "file_type:asc,file_name:asc",
                **(share or {}),
            },
        )
        rows = result.get("data", {}).get("list")
        metadata = result.get("metadata")
        total = metadata.get("_total") if isinstance(metadata, dict) else None
        if not isinstance(rows, list) or len(rows) > 50 or type(total) is not int or total < 0:
            raise MediaError("provider_incomplete_listing")
        if len(rows) < min(50, max(0, total - (page - 1) * 50)):
            raise MediaError("provider_incomplete_listing")
        seen = set()
        for row in rows:
            record = file_record(row)
            # The token-bound share root is virtual. A root entry may retain its
            # original cloud parent; children and ordinary cloud pages are not virtual.
            if (
                not isinstance(row.get("pdir_fid"), str)
                or record["file_id"] in seen
                or row["pdir_fid"] == record["file_id"]
                or (
                    not (share and parent == "root")
                    and row["pdir_fid"] != ("0" if parent == "root" else parent)
                )
            ):
                raise MediaError("provider_file_identity_mismatch")
            seen.add(record["file_id"])
        next_page = str(page + 1) if page * 50 < total else None
        return rows, next_page

    async def share(self, args):
        result = await self.request(
            "/share/sharepage/token",
            method="POST",
            body={
                "pwd_id": identifier(args["share_id"]),
                "passcode": args.get("password", ""),
            },
        )
        token = result.get("data", {}).get("stoken")
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise MediaError("provider_invalid_response")
        return {"pwd_id": args["share_id"], "stoken": token}

    async def source(self, args, share):
        marker = ""
        for _ in range(10):
            rows, marker = await self.page(args.get("parent_file_id", "root"), marker, share=share)
            for row in rows:
                if row.get("fid") == args["file_id"]:
                    return row
            if not marker:
                raise MediaError("source_file_not_found")
        raise MediaError("source_listing_limit")

    async def file(self, fid):
        if fid == "root":
            return {"file_id": "root", "type": "folder"}
        result = await self.request("/file/info", params={"fid": identifier(fid), "_fetch_full_path": 0})
        row = result.get("data")
        if not isinstance(row, dict) or row.get("fid") != fid:
            raise MediaError("provider_file_identity_mismatch")
        return file_record(row)

    async def recycle_page(self, marker=""):
        if marker and (not isinstance(marker, str) or not re.fullmatch(r"[1-9][0-9]{0,3}", marker)):
            raise ValueError("Invalid Quark page cursor")
        page = int(marker or "1")
        data = (
            await self.request(
                "/file/recycle/list",
                params={
                    "_page": page,
                    "_size": 50,
                    "_fetch_total": 1,
                },
            )
        )["data"]
        rows, total = data.get("list"), data.get("total")
        if (
            not isinstance(rows, list)
            or len(rows) > 50
            or type(total) is not int
            or total < 0
            or len(rows) != min(50, max(0, total - (page - 1) * 50))
        ):
            raise MediaError("provider_incomplete_listing")
        items = []
        for row in rows:
            file = file_record(row)
            try:
                recycle_id = identifier(row.get("record_id"))
            except ValueError:
                raise MediaError("provider_invalid_response") from None
            items.append({**file, "recycle_id": recycle_id})
        if len({f["file_id"] for f in items}) != len(items):
            raise MediaError("provider_invalid_response")
        return {"items": items, "marker": str(page + 1) if page * 50 < total else None}

    async def restore_record(self, args):
        try:
            recycle_id = identifier(args.get("recycle_id"))
        except ValueError:
            raise MediaError("recycle_record_missing") from None
        expected = args.get("expected_file")
        if not isinstance(expected, dict):
            raise MediaError("recycle_record_missing")
        marker = ""
        for _ in range(10):
            page = await self.recycle_page(marker)
            for entry in page["items"]:
                if entry["file_id"] == args["file_id"]:
                    if entry["recycle_id"] != recycle_id or any(
                        entry.get(k) != expected.get(k)
                        for k in ("file_id", "name", "parent_file_id", "size", "type")
                    ):
                        raise MediaError("recycle_record_changed")
                    return recycle_id
            marker = page["marker"]
            if not marker:
                raise MediaError("recycle_record_missing")
        raise MediaError("recycle_listing_limit")

    async def dispatch(self, action, args, member):
        if action == "status":
            return {"state": "ready", "login_method": "private_cookie"}
        if action == "capacity":
            return {"total": member.get("total_capacity"), "used": member.get("use_capacity")}
        if action == "trash_list":
            return await self.recycle_page(args.get("marker", ""))
        if action in {"trash", "restore"}:
            fid = identifier(args["file_id"])
            if action == "trash":
                path = "/file/delete"
                body = {"action_type": 2, "filelist": [fid], "exclude_fids": []}
            else:
                record = await self.restore_record(args)
                path = "/file/recycle/recover"
                body = {"select_mode": 2, "record_list": [record]}
            result = await self.request(path, method="POST", body=body)
            try:
                task = identifier(result["data"].get("task_id"))
            except ValueError:
                raise MediaError("library_write_unconfirmed", uncertain=True) from None
            return {"file_id": fid, "async_task_id": task}
        if action == "download_url":
            file = await self.file(identifier(args["file_id"]))
            if file.get("parent_file_id") != args["folder_id"]:
                raise MediaError("file_outside_configured_target")
            rows = (await self.request("/file/download", method="POST", body={"fids": [file["file_id"]]}))[
                "data"
            ]
            if len(rows) != 1 or file_record(rows[0]) != file:
                raise MediaError("download_source_changed")
            row = rows[0]
            checksum = row.get("md5")
            if not isinstance(checksum, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", checksum):
                raise MediaError("download_checksum_unavailable")
            url = row.get("download_url")
            if not isinstance(url, str) or len(url) > 16384:
                raise MediaError("invalid_original_download_url")
            parsed = urlsplit(url)
            # Cookie-bearing requests must remain on the provider's download hosts.
            # Do not use preview_url, redirects, or an object-store ETag as a hash.
            host = parsed.hostname or ""
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.password
                or parsed.port not in {None, 443}
                or not host.startswith("dl-")
                or not host.endswith(".drive.quark.cn")
            ):
                raise MediaError("invalid_original_download_url")
            return {
                "file": {**file, "content_hash_name": "md5", "content_hash": checksum.lower()},
                "url": url,
                "credential_origin": f"https://{parsed.netloc}",
                "headers": {
                    "Referer": "https://pan.quark.cn/",
                    "User-Agent": USER_AGENT,
                    "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
                },
            }
        if action in {"rename", "move"}:
            fid = identifier(args["file_id"])
            body = (
                {"fid": fid, "file_name": args["name"]}
                if action == "rename"
                else {
                    "action_type": 1,
                    "exclude_fids": [],
                    "filelist": [fid],
                    "to_pdir_fid": "0"
                    if args["to_folder_id"] == "root"
                    else identifier(args["to_folder_id"]),
                }
            )
            result = await self.request("/file/" + action, method="POST", body=body)
            receipt = {"file_id": fid}
            task = result["data"].get("task_id")
            if task is not None:
                try:
                    receipt["async_task_id"] = identifier(task)
                except ValueError:
                    raise MediaError("library_write_unconfirmed", uncertain=True) from None
            return receipt
        if action == "mkdir":
            result = await self.request(
                "/file",
                method="POST",
                body={
                    "pdir_fid": "0" if args["folder_id"] == "root" else identifier(args["folder_id"]),
                    "file_name": args["name"],
                    "dir_path": "",
                    "dir_init_lock": False,
                },
            )
            fid = result["data"].get("fid")
            if not isinstance(fid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", fid):
                raise MediaError("mkdir_identity_unconfirmed", uncertain=True)
            return {"file_id": fid}
        if action in {"folder", "file"}:
            file = await self.file(args["folder_id"] if action == "folder" else args["file_id"])
            if action == "file" and file.get("parent_file_id") != args["folder_id"]:
                raise MediaError("file_outside_configured_target")
            return file
        if action == "locate":
            file = await self.file(args["file_id"])
            current, parts, seen = file, [], set()
            for _ in range(16):
                if current["file_id"] == args["folder_id"]:
                    return {"file": file, "relative_parts": list(reversed(parts))}
                if current["file_id"] in seen or current["file_id"] == "root":
                    break
                seen.add(current["file_id"])
                parts.append(current["name"])
                current = await self.file(current["parent_file_id"])
                if current["type"] != "folder":
                    break
            raise MediaError("file_outside_configured_target")
        if action == "list":
            rows, marker = await self.page(args["folder_id"], args.get("marker", ""))
            return {"items": [file_record(r) for r in rows], "marker": marker}
        if action in {"inspect", "source", "copy"}:
            share = await self.share(args)
            if action == "inspect":
                rows, marker = await self.page(
                    args.get("parent_file_id", "root"), args.get("marker", ""), share=share
                )
                return {
                    "items": [
                        {**file_record(r), "parent_file_id": args.get("parent_file_id", "root")} for r in rows
                    ],
                    "marker": marker,
                }
            row = await self.source(args, share)
            if action == "source":
                return {**file_record(row), "parent_file_id": args.get("parent_file_id", "root")}
            token = row.get("share_fid_token")
            if not isinstance(token, str) or not token:
                raise MediaError("source_transfer_token_missing")
            result = await self.request(
                "/share/sharepage/save",
                method="POST",
                body={
                    "fid_list": [args["file_id"]],
                    "fid_token_list": [token],
                    "to_pdir_fid": "0" if args["folder_id"] == "root" else args["folder_id"],
                    "pdir_fid": row["pdir_fid"],
                    "pdir_save_all": False,
                    "exclude_fids": [],
                    "scene": "link",
                    **share,
                },
            )
            task = result.get("data", {}).get("task_id")
            if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", task):
                raise MediaError("copy_receipt_missing", uncertain=True)
            return {"task_id": task}
        if action == "copy_status":
            result = await self.request(
                "/task", params={"task_id": identifier(args["task_id"]), "retry_index": 0}
            )
            task = result.get("data", {})
            if task.get("status") == 2:
                saved = task.get("save_as", {})
                ids = saved.get("save_as_select_top_fids") or saved.get("save_as_top_fids")
                if not isinstance(ids, list) or len(ids) != 1:
                    raise MediaError("copy_result_unconfirmed", uncertain=True)
                return {"file_id": identifier(ids[0]), "state": "completed"}
            if task.get("status") == 3:
                raise MediaError("copy_failed", uncertain=True)
            return {"state": "pending"}
        raise MediaError("provider_action_unsupported")

    async def call(self, action, arguments=None):
        self.claim()
        async with self.lock:
            if self.closed:
                raise MediaError("media_stopping")
            if self.session is None:
                self.session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=15),
                    connector=aiohttp.TCPConnector(limit=1),
                    cookie_jar=aiohttp.DummyCookieJar(),
                    trust_env=False,
                )
            self.requests = 0
            try:
                async with asyncio.timeout(35):
                    member = await self.load_auth()
                    result = await self.dispatch(action, arguments or {}, member)
                self.account_changed("ready")
                return result
            except MediaError as exc:
                if exc.code in {"auth_required", "account_mismatch"}:
                    self.account_changed("auth_required")
                raise
            except TimeoutError:
                raise MediaError("provider_network_error", uncertain=action in WRITES) from None
