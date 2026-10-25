"""Operator draft probes: validate selected inputs without saving configuration or credentials."""

import asyncio
import tomllib

from assistant_runtime.core.models.catalog import models, query, result
from assistant_runtime.core.models.gateway import credential
from assistant_runtime.core.models.settings import ModelSettings
from .fields import merge
from .files import Conflict


class ProbeInput(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


def validation_message(message):
    # Match owner validation codes, never echo submitted values or parser messages.
    if "session header" in message or "Session header" in message:
        return (
            "session_header",
            "会话请求头：请留空或填写 x- 开头的小写请求头名称，例如 x-session-id；不能使用认证请求头。",
        )
    if "Endpoint" in message or "URL" in message or "endpoint" in message or "Port" in message:
        return (
            "base_url",
            "接口地址：请填写完整的 HTTPS 地址；本机测试支持回环 HTTP，地址不能包含密码、查询参数或片段。",
        )
    if "protocol" in message:
        return "protocol", "接口协议：请选择支持的协议。"
    if "preset" in message:
        return "preset", "接口类型：请选择有效的供应商类型。"
    if "credential environment" in message:
        return "key_env", "凭据引用名称：请使用大写字母、数字和下划线，并以大写字母开头。"
    if "authentication scheme" in message:
        return "auth_scheme", "鉴权方式：请选择 native 或 bearer。"
    if "account" in message:
        return "account", "原生账号：请选择已配置的账号并完成授权。"
    return "provider", "连接字段组合无效：预设连接使用接口类型，自定义连接使用协议和地址，请检查是否混填。"


class ProviderQueries:
    def __init__(self, business, accounts):
        self.business, self.accounts = business, accounts
        self.slots = asyncio.Semaphore(2)

    async def describe(self, data):
        from assistant_runtime.core.models.capabilities import (
            profile,
            public_catalog,
            search_references,
            supplier,
        )
        from assistant_runtime.core.pi.accounts import describe_model

        version = data.get("version")
        with self.business.files.locked():
            snapshot = self.business.files.snapshot()
            if snapshot.version != version:
                raise Conflict()
            if data.get("action") == "search":
                query = data.get("query", "")
                if not isinstance(query, str) or len(query) > 200 or any(ord(c) < 32 for c in query):
                    raise ProbeInput("query", "请输入 200 字符以内的模型名称或服务商。")
                return search_references(query.strip())
            raw = {}
            for content in snapshot.contents.values():
                merge(raw, tomllib.loads(content.decode()))
            registry = raw.get("model_api", {})
            provider = data.get("provider")
            if not isinstance(provider, dict):
                raise ValueError("请选择模型连接")
            account_name = provider.get("account")
            account = data.get("account", registry.get("accounts", {}).get(account_name))
            settings = ModelSettings.parse(
                {
                    "providers": {"probe": provider},
                    "accounts": {account_name: account} if account_name and account else {},
                }
            )
            provider = settings.providers["probe"]
            model, reference = data.get("model"), data.get("reference", "")
            if not isinstance(model, str) or not 1 <= len(model) <= 200 or any(ord(c) < 32 for c in model):
                raise ValueError("请填写有效的模型 ID")
            if not isinstance(reference, str) or (reference and reference not in public_catalog()["models"]):
                raise ProbeInput("reference", "该参考资料已不在目录中，请重新搜索并选择。")
            binary = raw.get("pi_binary", "pi")
        native = None
        owner = supplier(provider, account)
        if owner and provider["protocol"] == "pi":
            async with self.slots:
                try:
                    native = await describe_model(owner, model, binary=binary)
                except (OSError, ValueError, RuntimeError, TimeoutError):
                    # Public metadata remains useful offline without Pi installed.
                    native = None
        observed = data.get("observed")
        snapshot = profile(
            provider,
            model,
            account=account,
            native=native,
            remote=observed if isinstance(observed, dict) else None,
            reference=reference,
        )
        await asyncio.to_thread(self.check_version, version)
        return {"capabilities": snapshot, "native_available": native is not None}

    def capture(self, name, version, draft=None):
        with self.business.files.locked():
            snapshot = self.business.files.snapshot()
            if version != snapshot.version:
                raise Conflict()
            raw = {}
            for content in snapshot.contents.values():
                merge(raw, tomllib.loads(content.decode()))
            registry = raw.get("model_api", {})
            if not isinstance(name, str):
                raise ProbeInput("name", "请选择要测试的连接。")
            if draft is not None and (
                not isinstance(draft, dict) or set(draft) - {"provider", "account", "key"}
            ):
                raise ProbeInput("provider", "连接测试参数格式错误。")
            provider = (draft or {}).get("provider", registry.get("providers", {}).get(name))
            if not isinstance(provider, dict):
                raise ProbeInput("provider", "连接不存在，请先填写连接信息。")
            account = provider.get("account")
            saved_account = registry.get("accounts", {}).get(account) if isinstance(account, str) else None
            account_spec = (draft or {}).get("account", saved_account)
            if account and account_spec != saved_account:
                raise ProbeInput(
                    "account",
                    "原生账号认证设置尚未建立；请保存该账号并完成授权。API 连接可直接使用草稿测试。",
                )
            accounts = {account: account_spec} if isinstance(account, str) and account_spec else {}
            try:
                # A draft's display name is irrelevant to endpoint testing.
                settings = ModelSettings.parse({"providers": {"probe": provider}, "accounts": accounts})
            except (ValueError, TypeError, KeyError) as exc:
                field, message = validation_message(str(exc))
                raise ProbeInput(field, message) from None
            selected = settings.providers["probe"]
            key = None
            if not account:
                if draft is not None and "key" in draft:
                    key = draft["key"]
                    if (
                        not isinstance(key, str)
                        or not key
                        or len(key) > 32768
                        or any(ord(c) < 32 or ord(c) == 127 for c in key)
                    ):
                        raise ProbeInput("key", "API Key 为空或包含无效字符，请重新填写。")
                else:
                    try:
                        key = credential(self.business.settings.root, selected["key_env"])
                    except (ValueError, OSError):
                        raise ProbeInput(
                            "key",
                            "API Key 未填写，且此连接没有可读取的已保存凭据。请填写 API Key 后重试，无需保存。",
                        ) from None
            elif draft is not None and "key" in draft:
                raise ProbeInput("account", "原生账号授权使用账号管理入口；当前输入尚未应用到该账号。")
            return selected, key

    def check_version(self, version):
        with self.business.files.locked():
            if self.business.files.snapshot().version != version:
                raise Conflict()

    async def action(self, name, action, version, draft=None):
        if action not in {"test", "models"}:
            raise ValueError("Unknown provider operation")
        try:
            async with asyncio.timeout(18):
                async with self.slots:
                    try:
                        provider, key = await asyncio.to_thread(self.capture, name, version, draft)
                    except Conflict:
                        raise
                    except ProbeInput as exc:
                        return result(False, "configuration", str(exc), field=exc.field)
                    except (ValueError, KeyError, TypeError, OSError):
                        return result(
                            False, "configuration", "无法读取连接配置，请检查配置文件格式。", field="provider"
                        )
                    if provider["protocol"] == "pi":
                        data = await self.accounts.action(
                            provider["account"], "status" if action == "test" else "catalog", False
                        )
                        if action == "test":
                            connected = data.get("connected") is True
                            response = result(
                                connected,
                                "local_auth" if connected else "authentication",
                                "原生账号已配置认证；此检查不验证供应商网络或模型推理。"
                                if connected
                                else "账号尚未授权，请先登录或配置 API Key。",
                                source="native",
                            )
                        else:
                            response = result(
                                True,
                                "native_catalog",
                                "Pi 内置模型目录；是否可用仍取决于账号权限与实际验收。",
                                models=models(data),
                                source="native",
                                truncated=len(data) > 500,
                            )
                    else:
                        response = await query(provider, key)
                    await asyncio.to_thread(self.check_version, version)
                    if action == "test":
                        response.pop("models", None)
                    return response
        except asyncio.TimeoutError:
            return result(False, "timeout", "查询超时，请稍后重试。")
        except Conflict:
            raise
        except (ValueError, RuntimeError, OSError, KeyError, TypeError):
            return result(False, "account", "账号检查未完成，请检查 Pi 安装、账号配置及授权状态。")
