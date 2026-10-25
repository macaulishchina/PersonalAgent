"""Operator-only native account actions. Pi owns credentials and OAuth, never backups."""

import asyncio
from urllib.parse import urlsplit
import uuid

from assistant_runtime.core.pi.accounts import NativeAccounts


class Accounts:
    def __init__(self, business):
        self.business = business
        self.flows = {}

    def configured(self):
        from types import SimpleNamespace
        import tomllib
        from .fields import merge

        with self.business.files.locked():
            snapshot = self.business.files.snapshot()
            raw = {}
            for content in snapshot.contents.values():
                merge(raw, tomllib.loads(content.decode()))
        from assistant_runtime.core.models.settings import ModelSettings

        config = SimpleNamespace(root=self.business.settings.root, pi_binary=raw.get("pi_binary", "pi"))
        return config, ModelSettings.parse({"accounts": raw.get("model_api", {}).get("accounts", {})})

    def public(self):
        _, settings = self.configured()
        return {
            "accounts": [
                {
                    "name": name,
                    "provider": spec["provider"],
                    "auth": spec["auth"],
                    "environment_key": bool(spec.get("key_env")),
                }
                for name, spec in settings.accounts.items()
            ],
            "flows": [
                {k: v for k, v in flow.items() if k not in {"worker", "accounts", "task", "call_id"}}
                for flow in self.flows.values()
            ],
        }

    async def action(self, name, action, confirmed):
        if action not in {"status", "catalog", "login", "logout"}:
            raise ValueError("Unsupported account operation")
        if action in {"login", "logout"} and confirmed is not True:
            raise ValueError("Account changes require explicit confirmation")
        config, settings = self.configured()
        if name not in settings.accounts:
            raise ValueError("Account is not configured")
        if any(f["name"] == name and f["state"] == "pending" for f in self.flows.values()):
            raise ValueError("An account action is already pending")
        accounts = NativeAccounts(config.root, settings, binary=config.pi_binary or None)
        if action != "login":
            try:
                worker = await accounts.account(name)
                return await worker.call(action)
            finally:
                await accounts.close()
        if len([f for f in self.flows.values() if f["state"] == "pending"]) >= 4:
            raise ValueError("Too many account logins")
        # Retain only a bounded, transient flow history; never persist callbacks or answers.
        self.flows = {k: v for k, v in self.flows.items() if v["state"] == "pending"}
        ident = uuid.uuid4().hex
        flow = {"id": ident, "name": name, "state": "pending", "accounts": accounts}
        self.flows[ident] = flow
        flow["task"] = asyncio.create_task(self.login(flow), name="configuration-account-login")
        return {"id": ident, "state": "pending"}

    async def login(self, flow):
        try:
            async with asyncio.timeout(300):
                worker = flow["worker"] = await flow["accounts"].account(flow["name"])
                async for item in worker.events("login"):
                    if "result" in item:
                        flow.update(state="completed", connected=bool(item["result"].get("connected")))
                        break
                    event = item.get("event", {})
                    data = event.get("data", {})
                    if event.get("type") == "auth":
                        url = data.get("url") or data.get("authorizationUrl") or data.get("verificationUri")
                        if url and urlsplit(url).scheme in {"https", "http"}:
                            flow["authorization_url"] = url
                        # Protocol messages are provider supplied, displayed as plain text.
                        flow["message"] = str(data.get("instructions") or data.get("message") or "")[:2000]
                    elif event.get("type") == "prompt":
                        flow.update(
                            call_id=item["id"],
                            prompt={
                                "id": event["prompt_id"],
                                "type": data.get("type", "secret"),
                                "message": str(data.get("message") or data.get("label") or "请输入授权信息")[
                                    :1000
                                ],
                            },
                        )
        except asyncio.CancelledError:
            flow["state"] = "cancelled"
        except Exception:
            flow["state"] = "failed"
            flow["error"] = "account_authorization_failed_or_expired"
        finally:
            flow.pop("prompt", None)
            flow.pop("authorization_url", None)
            await flow["accounts"].close()

    async def answer(self, ident, prompt, value):
        flow = self.flows.get(ident)
        if not flow or flow["state"] != "pending" or flow.get("prompt", {}).get("id") != prompt:
            raise ValueError("Authorization prompt expired")
        if not isinstance(value, str) or len(value) > 32768:
            raise ValueError("Invalid authorization input")
        await flow["worker"].send({"action": "answer", "prompt_id": prompt, "value": value})
        flow.pop("prompt", None)
        return {"submitted": True}

    async def cancel(self, ident):
        flow = self.flows.get(ident)
        if not flow or flow["state"] != "pending":
            raise ValueError("Authorization is no longer pending")
        flow["task"].cancel()
        await asyncio.gather(flow["task"], return_exceptions=True)
        return {"state": flow["state"]}

    async def close(self):
        tasks = [f["task"] for f in self.flows.values() if not f["task"].done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
