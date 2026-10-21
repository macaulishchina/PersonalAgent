"""Account routing and explicit adapter capabilities; no execution policy or retries."""

from functools import partial

from .aliyun import Aliyun
from .errors import MediaError
from .quark import Quark

CAPABILITIES = {
    "aliyun": frozenset(
        {
            "status",
            "inspect",
            "source",
            "list",
            "folder",
            "file",
            "locate",
            "capacity",
            "copy",
            "download_url",
            "mkdir",
            "rename",
            "move",
            "trash",
            "restore",
            "trash_list",
            "login",
        }
    ),
    "quark": frozenset(
        {
            "status",
            "inspect",
            "source",
            "list",
            "folder",
            "file",
            "locate",
            "capacity",
            "copy",
            "copy_status",
            "trash",
            "restore",
            "trash_list",
            "mkdir",
            "download_url",
            "rename",
            "move",
        }
    ),
}


class Providers:
    def __init__(self, root, settings, repository, adapters=None):
        self.accounts = settings.accounts
        self.adapters = {}
        for account, definition in self.accounts.items():
            changed = partial(repository.account_state, account=account)
            self.adapters[account] = (
                adapters[account]
                if adapters is not None
                else {"aliyun": Aliyun, "quark": Quark}[definition["provider"]](
                    root, changed, account=account
                )
            )

    def claim(self):
        for adapter in self.adapters.values():
            adapter.claim()

    async def close(self):
        for adapter in self.adapters.values():
            await adapter.close()

    def resolve(self, account=None, *, provider=None):
        candidates = [k for k, v in self.accounts.items() if provider is None or v["provider"] == provider]
        if account is None and len(candidates) == 1:
            return candidates[0]
        if account not in candidates:
            raise ValueError("Choose a configured media account matching the share provider")
        return account

    def supports(self, account, action):
        return action in CAPABILITIES[self.accounts[account]["provider"]]

    def require(self, account, action):
        if not self.supports(account, action):
            raise MediaError("provider_action_unsupported")

    async def call(self, action, arguments=None):
        values = dict(arguments or {})
        account = self.resolve(values.pop("account", None))
        self.require(account, action)
        return await self.adapters[account].call(action, values)
