"""Independent management bootstrap; parsing never loads business configuration."""

from dataclasses import dataclass
import ipaddress
from pathlib import Path
import ssl
import tomllib

from assistant_runtime.api.configuration import ConfigurationError


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
    scheme: str = "http"

    @property
    def tls_dir(self):
        return self.root / "config/tls"

    def _private_ca_context(self, *, include_system_roots=False):
        if self.scheme != "https":
            return None
        try:
            ca = self.tls_dir / "ca.crt"
            if not ca.exists():
                return None
            context = (
                ssl.create_default_context()
                if include_system_roots
                else ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            )
            context.load_verify_locations(cafile=ca)
        except (OSError, ValueError) as exc:
            raise ConfigurationError(
                ["management", "scheme"],
                "invalid_tls_ca",
                "无法加载 HTTPS 信任证书，请检查 ca.crt 的格式和权限。",
            ) from exc
        return context

    def client_ssl_context(self):
        return self._private_ca_context(include_system_roots=True)

    def ca_certificate(self):
        context = self._private_ca_context()
        if context is None:
            return b""
        return "".join(
            ssl.DER_cert_to_PEM_cert(cert) for cert in context.get_ca_certs(binary_form=True)
        ).encode("ascii")

    def server_ssl_context(self):
        if self.scheme != "https":
            return None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            context.load_cert_chain(self.tls_dir / "server.crt", self.tls_dir / "server-key.pem", password="")
        except FileNotFoundError as exc:
            raise ConfigurationError(
                ["management", "scheme"], "tls_files_required", "请先在服务器安装 HTTPS 证书和私钥。"
            ) from exc
        except (OSError, ValueError) as exc:
            raise ConfigurationError(
                ["management", "scheme"],
                "invalid_tls_files",
                "无法加载 HTTPS 证书和私钥，请检查文件格式、读取权限及配对关系。",
            ) from exc
        return context

    @classmethod
    def load(cls, path):
        path = Path(path).absolute()
        return cls.from_text(path.read_text(encoding="utf-8"), directory=path.parent)

    @classmethod
    def from_text(cls, text, *, directory):
        data = tomllib.loads(text)
        if set(data) - {
            "root",
            "entry",
            "host",
            "port",
            "startup_seconds",
            "shutdown_seconds",
            "trusted_proxies",
            "public_origin",
            "scheme",
        }:
            raise ValueError("Unknown management setting")
        for key in ("root", "entry"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ValueError("Management requires explicit root and entry")
            value = Path(data[key]).expanduser()
            data[key] = (directory / value).resolve()
        if data.get("scheme", "http") not in ("http", "https"):
            raise ConfigurationError(["management", "scheme"], "invalid_scheme", '请选择 "http" 或 "https"。')
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
