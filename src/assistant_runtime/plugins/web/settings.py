from dataclasses import dataclass
from pathlib import Path
import ipaddress
import re


@dataclass(frozen=True)
class WebSettings:
    enable: bool = False
    host: str = "127.0.0.1"
    port: int = 8046
    token_file: str = ""
    approved_short_password_sha256: str = ""

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) - cls.__dataclass_fields__.keys():
            raise ValueError("Invalid web settings")
        settings = cls(**value)
        if (
            type(settings.enable) is not bool
            or type(settings.port) is not int
            or not 1 <= settings.port <= 65535
        ):
            raise ValueError("Invalid web enable or port")
        ipaddress.ip_address(settings.host)
        if not isinstance(settings.token_file, str):
            raise ValueError("web.token_file must be a path")
        fingerprint = settings.approved_short_password_sha256
        if not isinstance(fingerprint, str) or (
            fingerprint and not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
        ):
            raise ValueError("Invalid approved web password fingerprint")
        return settings

    def credential_path(self, root):
        return Path(self.token_file).expanduser() if self.token_file else root / "web-access-token"
