"""Web access policy without business startup or access to other modules' settings."""

from pathlib import Path
import secrets
from assistant_runtime.api.configuration import ConfigurationResource, ConfigurationError
from assistant_runtime.api.system import system


def registered(root, settings):
    path = root / Path(settings.get("token_file") or "state/web-access-token").expanduser()
    return [ConfigurationResource("web_access", path, "credential", "聊天网页访问口令", True)]


def provision(path, settings, selected):
    if selected and settings.get("enable", True) and not path.exists():
        system.atomic_write(path, (secrets.token_urlsafe(32) + "\n").encode())


def prepare_defaults(value, cleared, writable, settings, selected):
    if selected and settings.get("enable", True) and value is None:
        if cleared:
            raise ConfigurationError(
                ["plugin_settings", "web", "token_file"], "required", "已启用的网页入口需要访问口令"
            )
        if writable:
            return (secrets.token_urlsafe(32) + "\n").encode()
    return value


def validate_candidate(settings, selected, token):
    from assistant_runtime.api.passwords import validate_password

    if selected and settings.get("enable", True) and token is not None:
        try:
            validate_password(token.decode("utf-8").rstrip("\r\n"))
        except ValueError as exc:
            raise ConfigurationError(
                ["plugin_settings", "web", "token_file"], "credential", str(exc)
            ) from exc
