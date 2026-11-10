"""Bounded, operator-initiated metadata queries; never perform inference or retry."""

import asyncio
import json
import uuid
from urllib.parse import urlsplit

import aiohttp
from .capabilities import facts

LIMIT = 500
MAX_BYTES = 1024 * 1024


def result(ok, code, message, **values):
    return {"ok": ok, "code": code, "message": message, **values}


# OpenCode's public directory identifies the advertised API by key.
DIRECTORY_APIS = {
    "responses": "openai_responses",
    "chat-completions": "openai_chat",
    "messages": "anthropic",
    "generate-content": "gemini",
}


def models(rows, *, gemini=False):
    if not isinstance(rows, list):
        raise ValueError("Invalid model directory")
    unique = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ident = row.get("name") if gemini else row.get("id")
        if gemini and isinstance(ident, str):
            ident = ident.removeprefix("models/")
        if (
            not isinstance(ident, str)
            or not 1 <= len(ident) <= 200
            or any(ord(c) < 32 or ord(c) == 127 for c in ident)
        ):
            continue
        label = row.get("displayName") or row.get("display_name") or ident
        unique[ident] = {"id": ident, "label": label[:200] if isinstance(label, str) else ident}
        # Only carry an explicit, recognized recommendation from the directory.
        protocol = row.get("protocol")
        if protocol not in DIRECTORY_APIS.values():
            apis = row.get("api")
            candidates = (
                [kind for key, kind in DIRECTORY_APIS.items() if key in apis]
                if isinstance(apis, dict)
                else []
            )
            protocol = candidates[0] if len(candidates) == 1 else None
        if protocol:
            unique[ident]["protocol"] = protocol
        metadata = facts(
            {
                **row,
                **(
                    {
                        "context_window": row.get("inputTokenLimit"),
                        "output_limit": row.get("outputTokenLimit"),
                    }
                    if gemini
                    else {}
                ),
            }
        )
        if metadata:
            unique[ident]["capabilities"] = metadata
        if len(unique) >= LIMIT:
            break
    return sorted(unique.values(), key=lambda row: row["id"].casefold())


async def query(provider, key):
    protocol = provider.get("protocol")
    catalog = provider.get("catalog", {})
    if protocol is None and not catalog:
        return result(False, "catalog_unavailable", "请在连接中填写共享模型目录地址，或手工填写模型 ID。")
    base = provider["base_url"].rstrip("/")
    endpoint = catalog.get("url") or base + (
        "/v1/models" if protocol == "anthropic" and not urlsplit(base).path.endswith("/v1") else "/models"
    )
    format = catalog.get("format", protocol)
    auth = protocol or format
    headers = {"Accept": "application/json", "User-Agent": "Personal-Agent/model-catalog"}
    if provider.get("auth_scheme") == "bearer" or auth in {"openai", "openai_chat", "openai_responses"}:
        headers["Authorization"] = "Bearer " + key
    elif auth == "anthropic":
        headers["x-api-key"] = key
    elif auth == "gemini":
        headers["x-goog-api-key"] = key
    else:
        return result(False, "unsupported", "此连接不支持远程模型目录查询，可手工填写模型 ID。")
    if provider.get("session_header"):
        headers[provider["session_header"]] = "pa-catalog-" + uuid.uuid4().hex
    if format == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    params = (
        {"limit": "100"} if format == "anthropic" else {"pageSize": "500"} if format == "gemini" else None
    )
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), trust_env=False) as session:
            async with session.get(
                endpoint, headers=headers, params=params, allow_redirects=False
            ) as response:
                status = response.status
                if status in {401, 403}:
                    return result(
                        False,
                        "authentication",
                        "认证未通过，请检查 API Key 和模型目录访问权限。",
                        status=status,
                    )
                if status in {404, 405}:
                    return result(
                        False,
                        "catalog_unavailable",
                        "此地址未提供模型目录接口，请核对地址；也可手工填写模型 ID。",
                        status=status,
                    )
                if status == 429:
                    return result(False, "rate_limited", "供应商请求过于频繁，请稍后再试。", status=status)
                if 300 <= status < 400:
                    return result(
                        False, "redirect", "接口返回了重定向，请配置最终接口地址后重试。", status=status
                    )
                if status != 200:
                    return result(
                        False,
                        "http_error",
                        f"目录请求失败（HTTP {status}），请检查供应商服务状态。",
                        status=status,
                    )
                content = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    content.extend(chunk)
                    if len(content) > MAX_BYTES:
                        return result(
                            False, "response_limit", "模型目录响应过大，请缩小供应商目录或手工填写模型 ID。"
                        )
                payload = json.loads(content)
                rows = payload.get("models" if format == "gemini" else "data")
                values = models(rows, gemini=format == "gemini")
                return result(
                    True,
                    "reachable",
                    "模型目录接口已连通；尚未验证模型调用权限、额度或推理。",
                    models=values,
                    source="remote",
                    truncated=bool(
                        payload.get("has_more") or payload.get("nextPageToken") or len(rows) > LIMIT
                    ),
                )
    except asyncio.TimeoutError:
        return result(False, "timeout", "连接超时，请检查接口地址和服务器网络。")
    except aiohttp.ClientError:
        return result(False, "network", "无法连接供应商，请检查地址、网络及 TLS 证书。")
    except (ValueError, TypeError, AttributeError):
        return result(False, "invalid_response", "接口未返回可识别的模型目录，可手工填写模型 ID。")
