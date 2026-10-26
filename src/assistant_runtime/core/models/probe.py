"""Operator-only, single-call text probes using shared adapters and durable usage."""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from copy import deepcopy

from assistant_runtime.api.ai_usage import UsageError, UsageScope, metered
from assistant_runtime.api.models import ModelCallError
from assistant_runtime.core.pi.accounts import NativeAccounts
from .client import ModelClient
from .settings import ModelSettings, reasoning_effort

log = logging.getLogger(__name__)
TIMEOUT = 45
MAX_TOKENS = 1024
PROMPT = "This is a text connectivity test. Please reply briefly with OK."


def response(ok, code, message, **values):
    return {"ok": ok, "code": code, "message": message, **values}


class ModelProbes:
    def __init__(self, owner):
        self.owner = owner
        self.lock = asyncio.Lock()
        self.db = owner.db
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS model_probes "
            "(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)"
        )

    async def test(self, arguments):
        # A persisted unknown receipt prevents replay after restart or lost HTTP replies.
        ident = str(uuid.UUID(arguments["request_id"]))
        if ident != arguments["request_id"]:
            raise ValueError("Canonical request ID required")
        fingerprint = hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()
        previous = self.db.execute(
            "SELECT fingerprint,result FROM model_probes WHERE id=?", (ident,)
        ).fetchone()
        if previous:
            if previous[0] != fingerprint:
                return response(False, "request_conflict", "测试编号已用于另一份设置，请重新点击测试。")
            return json.loads(previous[1])
        if self.lock.locked():
            return response(False, "busy", "另一个模型正在测试，请等待完成后再试。")
        async with self.lock:
            return await self.run(ident, fingerprint, arguments)

    async def run(self, ident, fingerprint, arguments):
        started = time.monotonic()
        client = None
        result = response(False, "unknown", "测试结果未知；不会自动重试。", request_id=ident)
        try:
            name = arguments["name"]
            entry = deepcopy(arguments["entry"])
            provider = deepcopy(arguments["provider"])
            accounts = deepcopy(arguments.get("accounts", {}))
            # This is an explicit test of one draft, not activation of the connection.
            provider["enabled"] = True
            if provider.get("account"):
                provider.pop("protocol", None)  # The parser normalizes native bindings itself.
            entry["fallbacks"] = []
            settings = ModelSettings.parse(
                {
                    "enabled": True,
                    "providers": {entry["provider"]: provider},
                    "entries": {name: entry},
                    "accounts": accounts,
                    "timeout_seconds": TIMEOUT,
                    "max_output_tokens": MAX_TOKENS,
                }
            )
            selected = settings.entry(name)
            effort = reasoning_effort(selected)
            payload = {
                "model": name,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": PROMPT}]}],
                "reasoning": {"effort": effort},
                "max_output_tokens": min(MAX_TOKENS, selected.get("output_limit", MAX_TOKENS)),
            }
            key_env = provider.get("key_env") or accounts.get(provider.get("account"), {}).get("key_env")
            key = arguments.get("key")
            if key is not None and (
                not isinstance(key, str)
                or not key
                or len(key) > 32768
                or any(ord(c) < 32 or ord(c) == 127 for c in key)
            ):
                raise ValueError("Invalid private credential")
            config = self.owner.host.config
            client = ModelClient(
                settings,
                accounts=NativeAccounts(
                    config.root,
                    settings,
                    binary=getattr(config, "pi_binary", "pi"),
                    credentials={key_env: key} if key_env and key is not None else {},
                ),
            )
            with self.db:
                self.db.execute(
                    "INSERT INTO model_probes VALUES (?,?,?)", (ident, fingerprint, json.dumps(result))
                )
            async with asyncio.timeout(TIMEOUT):
                async with self.owner.semaphore:
                    await client.start()
                    bound = await client.output_bound(payload)
                    if bound > MAX_TOKENS:
                        result = response(
                            False, "output_limit", "此账号不支持单次输出上限，暂不能进行有界文本测试。"
                        )
                    else:
                        reply = await metered(
                            self.owner.host.ai_usage.service,
                            UsageScope("model-test:" + ident, "configuration"),
                            "inference",
                            selected["provider"],
                            selected["model"],
                            lambda: client.complete(payload, session_id="model-test:" + ident),
                            bounds={"input_tokens": 1024, "output_tokens": bound},
                            body=payload,
                        )
                        result = response(
                            True,
                            "completed",
                            "文本推理可用；未验证图片、工具调用或结构化输出。",
                            reply=reply["text"][:240],
                            first_text_ms=reply.get("first_text_ms"),
                            reasoning_effort=effort,
                        )
        except TimeoutError:
            result = response(False, "timeout", "测试超时；上游是否完成未知，不会自动重试。")
        except UsageError:
            result = response(False, "usage_limit", "用量限制或计量服务阻止了本次测试，请查看用量状态。")
        except ModelCallError as exc:
            messages = {
                "model_http_401": "认证失败，请检查此连接的凭据和认证方式。",
                "model_http_403": "供应商拒绝访问，请检查账号权限。",
                "model_http_404": "模型或推理接口不存在，请检查模型 ID、协议与地址。",
                "model_http_400": "推理参数被拒绝（HTTP 400），请检查协议、模型 ID 和思考模式。",
                "model_http_402": "供应商余额或额度不足（HTTP 402）。",
                "model_http_429": "供应商限流或配额不足，请稍后再试。",
            }
            result = response(
                False, exc.code, messages.get(exc.code, "供应商拒绝或未能完成推理请求，请检查服务状态。")
            )
        except (ValueError, KeyError, TypeError):
            result = response(
                False, "configuration", "模型或连接设置不完整或不匹配，请检查模型 ID、思考模式和能力资料。"
            )
        except (RuntimeError, OSError) as exc:
            log.warning("model_probe_incomplete type=%s", type(exc).__name__)
            result = response(
                False, "incomplete", "未获得完整文本回复；可能是网络、协议或输出上限问题，不会自动重试。"
            )
        finally:
            if client:
                await client.close()
        result.update(request_id=ident, elapsed_ms=round((time.monotonic() - started) * 1000))
        with self.db:
            self.db.execute("UPDATE model_probes SET result=? WHERE id=?", (json.dumps(result), ident))
        return result
