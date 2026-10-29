from assistant_runtime.api.configuration import configuration_default

from dataclasses import dataclass, field
from pathlib import Path
import ipaddress


@dataclass(frozen=True)
class WebSettings:
    enable: bool = field(default_factory=lambda: configuration_default(__file__, "enable"))
    host: str = field(default_factory=lambda: configuration_default(__file__, "host"))
    port: int = field(default_factory=lambda: configuration_default(__file__, "port"))
    token_file: str = field(default_factory=lambda: configuration_default(__file__, "token_file"))

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) - cls.__dataclass_fields__.keys():
            raise ValueError("Invalid web settings")
        settings = cls(**value)
        if (
            type(settings.enable) is not bool
            or type(settings.port) is not int
            or not 0 <= settings.port <= 65535
        ):
            raise ValueError("Invalid web enable or port")
        address = ipaddress.ip_address(settings.host)
        if settings.enable and not address.is_loopback:
            raise ValueError("聊天网页内部传输仅允许本机地址；对外地址和端口统一在实例管理中设置")
        if not isinstance(settings.token_file, str):
            raise ValueError("web.token_file must be a path")
        return settings

    def credential_path(self, root):
        return Path(self.token_file).expanduser() if self.token_file else root / "web-access-token"
