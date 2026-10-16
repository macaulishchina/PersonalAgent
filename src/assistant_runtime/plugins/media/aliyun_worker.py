"""Isolated upstream SDK auth and bounded Aliyun protocol operations.

One JSON request on stdin, sanitized JSON replies on stdout. No model, shell, global
Codex settings or OpenList database access. All application writes are sent once.
"""

import base64
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import sys

from .errors import MediaError
from .private import save

HOST = "https://api.aliyundrive.com"
FIELDS = (
    "file_id",
    "name",
    "type",
    "size",
    "category",
    "file_extension",
    "content_hash",
    "content_hash_name",
)


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def identity(token):
    if not token.user_id:
        raise MediaError("account_identity_missing")
    return hashlib.sha256(token.user_id.encode()).hexdigest()


def sdk(directory, *, login=False):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # aligo creates ~/.aligo at import time. Confine that legacy default to this
    # worker's private directory without changing HOME or the host interpreter.
    original = Path.home
    Path.home = classmethod(lambda cls: directory)
    try:
        from aligo import Auth, set_config_folder
    finally:
        Path.home = original
    set_config_folder(str(directory))

    def atomic_save(auth):
        save(auth._name, auth.token.to_dict())

    Auth._save = atomic_save

    def qr(link):
        import qrcode

        image = io.BytesIO()
        qrcode.make(link).save(image, format="PNG")
        emit({"qr_base64": base64.b64encode(image.getvalue()).decode()})

    if not login and not (directory / "account.json").is_file():
        raise MediaError("auth_required")
    return Auth(
        name="account",
        show=qr,
        level=logging.CRITICAL,
        requests_timeout=12,
        login_timeout=180,
        re_login=False,
    )


def request(auth, path, body, *, share_token=None, write=False):
    import requests

    headers = {"x-share-token": share_token} if share_token else {}
    try:
        with auth.session.post(HOST + path, json=body, headers=headers, timeout=12, stream=True) as response:
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > 2 * 1024 * 1024:
                    raise MediaError("provider_response_limit", uncertain=write)
            try:
                result = json.loads(data)
            except (ValueError, UnicodeError):
                raise MediaError("provider_invalid_response", uncertain=write) from None
            if not isinstance(result, dict):
                raise MediaError("provider_invalid_response", uncertain=write)
            code = result.get("code", "")
            if code in {"AccessTokenInvalid", "AccessTokenExpired", "InvalidAccessToken", "TokenExpired"}:
                raise MediaError("access_expired")
            if code in {"UserDeviceOffline", "DeviceSessionSignatureInvalid"}:
                raise MediaError("device_session_expired")
            if response.status_code == 429:
                raise MediaError("rate_limited")
            if response.status_code >= 500:
                raise MediaError("provider_unavailable", uncertain=write)
            if response.status_code >= 400 or code:
                known = {
                    "ShareLink.Cancelled": "share_cancelled",
                    "ShareLink.Forbidden": "share_forbidden",
                    "ShareLink.Expired": "share_expired",
                    "InvalidResource.SharePwd": "share_password_invalid",
                    "NotFound.File": "file_not_found",
                    "ForbiddenNoPermission.File": "permission_denied",
                    "QuotaExhausted.Drive": "capacity_exhausted",
                }
                raise MediaError(known.get(code, "provider_rejected"))
            return result
    except (requests.RequestException, OSError):
        raise MediaError("provider_network_error", uncertain=write) from None


def share_token(auth, args):
    result = request(
        auth,
        "/v2/share_link/get_share_token",
        {
            "share_id": args["share_id"],
            "share_pwd": args.get("password", ""),
        },
    )
    token = result.get("share_token")
    if not isinstance(token, str) or not token:
        raise MediaError("share_token_missing")
    return token


def file_record(item):
    if not isinstance(item, dict):
        raise MediaError("provider_invalid_file")
    for field, maximum in (
        ("file_id", 160),
        ("name", 500),
        ("type", 20),
        ("category", 40),
        ("file_extension", 40),
        ("content_hash", 128),
        ("content_hash_name", 40),
    ):
        value = item.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise MediaError("provider_invalid_file")
    if item.get("size") is not None and (type(item["size"]) is not int or item["size"] < 0):
        raise MediaError("provider_invalid_file")
    return {key: item.get(key) for key in FIELDS}


def files(result):
    items = result.get("items")
    if not isinstance(items, list) or len(items) > 100 or any(not isinstance(x, dict) for x in items):
        raise MediaError("provider_invalid_listing")
    return [file_record(item) for item in items]


def execute(auth, action, args):
    if action == "status":
        request(auth, "/v2/user/get", {})
        return {"identity": identity(auth.token), "state": "ready"}
    if action == "refresh":
        from aligo.types import Token

        try:
            result = request(
                auth,
                "/v2/account/token",
                {
                    "refresh_token": auth.token.refresh_token,
                    "grant_type": "refresh_token",
                },
                write=True,
            )
            replacement = Token(**result)
            if identity(replacement) != identity(auth.token):
                raise MediaError("account_mismatch")
            auth.token = replacement
            auth.token.x_device_id = auth._x_device_id
            auth._save()
        except MediaError as exc:
            # An uncertain rotation must not retry the old refresh token.
            raise MediaError("auth_required") from exc
        return {"state": "ready"}
    if action == "session":
        request(
            auth,
            "/users/v1/users/device/create_session",
            {
                "deviceName": "personal-agent media",
                "modelName": "Linux",
                "pubKey": auth._X_PUBLIC_KEY,
            },
        )
        return {"state": "ready"}
    if action in {"inspect", "source", "copy"}:
        token = share_token(auth, args)
        if action == "inspect":
            result = request(
                auth,
                "/adrive/v2/file/list_by_share",
                {
                    "share_id": args["share_id"],
                    "parent_file_id": args.get("parent_file_id", "root"),
                    "limit": 100,
                    "marker": args.get("marker", ""),
                    "order_by": "name",
                    "order_direction": "ASC",
                },
                share_token=token,
            )
            return {"items": files(result), "marker": result.get("next_marker") or None}
        if action == "source":
            result = request(
                auth,
                "/adrive/v2/file/get_by_share",
                {
                    "share_id": args["share_id"],
                    "file_id": args["file_id"],
                },
                share_token=token,
            )
            if result.get("file_id") != args["file_id"]:
                raise MediaError("provider_file_identity_mismatch")
            return file_record(result)
        result = request(
            auth,
            "/v2/file/copy",
            {
                "share_id": args["share_id"],
                "file_id": args["file_id"],
                "to_drive_id": args["drive_id"],
                "to_parent_file_id": args["folder_id"],
                "auto_rename": False,
                "overwrite": False,
            },
            share_token=token,
            write=True,
        )
        return {key: result.get(key) for key in ("file_id", "async_task_id")}
    if action == "folder":
        if args["folder_id"] == "root":
            request(auth, "/v2/drive/get", {"drive_id": args["drive_id"]})
            return {"file_id": "root", "type": "folder"}
        result = request(
            auth,
            "/v2/file/get",
            {
                "drive_id": args["drive_id"],
                "file_id": args["folder_id"],
            },
        )
        if result.get("file_id") != args["folder_id"]:
            raise MediaError("provider_folder_identity_mismatch")
        return file_record(result)
    if action == "capacity":
        data = request(auth, "/adrive/v1/user/driveCapacityDetails", {})
        return {"total": data.get("drive_total_size"), "used": data.get("drive_used_size")}
    if action in {"file", "download_url"}:
        body = {"drive_id": args["drive_id"], "file_id": args["file_id"]}
        result = request(auth, "/v2/file/get", body)
        if result.get("file_id") != args["file_id"] or result.get("parent_file_id") != args["folder_id"]:
            raise MediaError("file_outside_configured_target")
        file = file_record(result)
        if action == "file":
            return file
        result = request(auth, "/v2/file/get_download_url", {**body, "expire_sec": 14400})
        if result.get("size") != file.get("size") or not result.get("url"):
            raise MediaError("original_download_unavailable")
        # Short-lived capability URL stays inside the adapter/download worker.
        # Never request video preview/transcode URLs or expose this URL through MCP.
        return {"file": file, "url": result["url"]}
    if action == "drives":
        result = request(auth, "/v2/drive/list_my_drives", {})
        return {
            "items": [
                {key: drive.get(key) for key in ("drive_id", "drive_name", "drive_type")}
                for drive in result.get("items", [])[:32]
            ]
        }
    if action == "mkdir":
        # Trusted onboarding only; deliberately absent from MediaRequest/MCP.
        result = request(
            auth,
            "/adrive/v2/file/createWithFolders",
            {
                "drive_id": args["drive_id"],
                "parent_file_id": args["folder_id"],
                "name": args["name"],
                "type": "folder",
                "check_name_mode": "refuse",
            },
            write=True,
        )
        return {key: result.get(key) for key in FIELDS}
    if action == "list":
        result = request(
            auth,
            "/adrive/v3/file/list",
            {
                "drive_id": args["drive_id"],
                "parent_file_id": args["folder_id"],
                "limit": 100,
                "marker": args.get("marker", ""),
                "order_by": "name",
                "order_direction": "ASC",
            },
        )
        return {"items": files(result), "marker": result.get("next_marker") or None}
    raise MediaError("unsupported_provider_action")


def main():
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    try:
        message = json.loads(sys.stdin.buffer.readline(65537))
        directory = Path(message["directory"])
        action, args = message["action"], message.get("arguments", {})
        auth = sdk(directory, login=action == "login")
        fingerprint = identity(auth.token)
        if message.get("expected_identity") and message["expected_identity"] != fingerprint:
            raise MediaError("account_mismatch")
        result = (
            {"identity": fingerprint, "state": "ready"} if action == "login" else execute(auth, action, args)
        )
        emit({"result": result})
    except MediaError as exc:
        emit({"error": exc.code, "uncertain": exc.uncertain})
    except Exception:
        # SDK exceptions/logs may embed HTTP response tokens or personal identity.
        emit({"error": "sdk_failed", "uncertain": True})


if __name__ == "__main__":
    main()
