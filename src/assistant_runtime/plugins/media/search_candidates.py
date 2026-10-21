"""Public candidate normalization. A discovery link grants no acquisition capability."""

import base64
import hashlib
import re
from urllib.parse import parse_qs, urlsplit

# Exact host/path allowlist; never trust the search backend's platform label.
PLATFORMS = {
    "aliyun": (
        {"www.alipan.com", "alipan.com", "www.aliyundrive.com", "aliyundrive.com"},
        r"/s/([A-Za-z0-9]{5,64})/?",
    ),
    "quark": ({"pan.quark.cn"}, r"/s/([A-Za-z0-9]{5,64})/?"),
    "baidu": ({"pan.baidu.com"}, r"/s/([A-Za-z0-9_-]{5,128})/?"),
    "115": ({"115.com", "115cdn.com", "anxia.com"}, r"/s/([A-Za-z0-9]{5,64})/?"),
    "123": (
        {"www.123pan.com", "123pan.com", "www.123pan.cn", "123pan.cn", "www.123684.com", "www.123865.com"},
        r"/s/([A-Za-z0-9_-]{5,128})(?:\.html)?/?",
    ),
    "uc": ({"drive.uc.cn"}, r"/s/([A-Za-z0-9]{5,64})/?"),
    "tianyi": ({"cloud.189.cn"}, r"/t/([A-Za-z0-9]{5,64})/?"),
    "xunlei": ({"pan.xunlei.com"}, r"/s/([A-Za-z0-9_-]{5,128})/?"),
    "pikpak": ({"mypikpak.com"}, r"/s/([A-Za-z0-9_-]{5,128})/?"),
}
DISCOVERY_PLATFORMS = (*PLATFORMS, "magnet")


def platform_filter(value):
    if value is None:
        return sorted(DISCOVERY_PLATFORMS)
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= len(DISCOVERY_PLATFORMS)
        or any(not isinstance(v, str) or v not in DISCOVERY_PLATFORMS for v in value)
    ):
        raise ValueError("Choose supported discovery platforms; acquisition remains Aliyun-only")
    return sorted(set(value))


def public_link(value):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("Invalid public share link")
    parsed = urlsplit(value)
    query = parse_qs(parsed.query)
    if parsed.scheme == "magnet":
        ids = query.get("xt", [])
        if len(ids) != 1 or not re.fullmatch(r"urn:btih:(?:[A-Fa-f0-9]{40}|[A-Za-z2-7]{32})", ids[0]):
            raise ValueError("Invalid magnet identity")
        # Trackers and arbitrary URLs are not needed for discovery, nor followed by this module.
        ident = ids[0].removeprefix("urn:btih:")
        ident = base64.b32decode(ident.upper()).hex() if len(ident) == 32 else ident.lower()
        return "magnet", ident, "magnet:?xt=urn:btih:" + ident, ""
    if parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ValueError("Expected public HTTPS share")
    for platform, (hosts, pattern) in PLATFORMS.items():
        match = re.fullmatch(pattern, parsed.path)
        if parsed.hostname in hosts and match:
            ident = match[1]
            codes = query.get("pwd", query.get("password", []))
            # 115 puts its share access code in the fragment.
            if platform == "115" and not codes:
                codes = parse_qs(parsed.fragment).get("password", [])
            code = codes[0] if len(codes) == 1 and len(codes[0]) <= 32 else ""
            url = (
                "https://www.alipan.com/s/" + ident
                if platform == "aliyun"
                else f"https://{parsed.hostname}{parsed.path}"
            )
            return platform, ident, url, code
    raise ValueError("Unsupported discovery domain")


def candidate_id(platform, ident):
    return hashlib.sha256((platform + ":" + ident).encode()).hexdigest()[:32]


def acquisition(platform, *, connected=True):
    supported = platform in {"aliyun", "quark"}
    return {
        "inspect": supported and connected,
        "transfer": supported and connected,
        "download": "after_cloud_transfer" if platform == "aliyun" else "unsupported",
        "blocker": (None if connected else "account_not_configured")
        if supported
        else "provider_not_connected",
        "availability": "unverified",
    }
