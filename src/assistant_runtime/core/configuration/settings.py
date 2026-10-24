"""Independent management bootstrap; parsing never loads business configuration."""

from dataclasses import dataclass
import ipaddress
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class ManagementSettings:
    root: Path
    entry: Path
    host: str = "0.0.0.0"
    port: int = 8047
    startup_seconds: int = 120
    shutdown_seconds: int = 60
    trusted_proxies: tuple[str, ...] = ()
    public_origin: str = ""

    @classmethod
    def load(cls, path):
        path = Path(path).absolute()
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if set(data) - {
            "root",
            "entry",
            "host",
            "port",
            "startup_seconds",
            "shutdown_seconds",
            "trusted_proxies",
            "public_origin",
        }:
            raise ValueError("Unknown management setting")
        for key in ("root", "entry"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ValueError("Management requires explicit root and entry")
            value = Path(data[key]).expanduser()
            data[key] = (path.parent / value).resolve() if not value.is_absolute() else value.resolve()
        proxies = data.get("trusted_proxies", [])
        if not isinstance(proxies, list) or len(proxies) > 16 or any(not isinstance(p, str) for p in proxies):
            raise ValueError("Invalid trusted proxies")
        for proxy in proxies:
            ipaddress.ip_network(proxy, strict=False)
        data["trusted_proxies"] = tuple(proxies)
        origin = data.get("public_origin", "")
        if origin:
            from urllib.parse import urlsplit

            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Public origin must be an HTTP(S) origin without a path")
            if not proxies:
                raise ValueError("Public origin requires explicit trusted proxies")
        result = cls(**data)
        ipaddress.ip_address(result.host)
        if type(result.port) is not int or not 1 <= result.port <= 65535:
            raise ValueError("Invalid management port")
        for value in (result.startup_seconds, result.shutdown_seconds):
            if type(value) is not int or not 10 <= value <= 3600:
                raise ValueError("Management process timeout must be 10..3600 seconds")
        return result
