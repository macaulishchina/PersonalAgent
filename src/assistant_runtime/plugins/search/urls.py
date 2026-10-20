"""Public-page boundaries; provider endpoints are separately administrator configured."""

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit


class SearchError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def public_address(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def public_url(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 8192
        or any(ord(c) <= 32 or ord(c) == 127 for c in value)
    ):
        raise SearchError("invalid_public_url")
    try:
        value.encode("utf-8")
        p = urlsplit(value)
        host = (p.hostname or "").encode("idna").decode().lower()
        port = p.port
        if (
            p.scheme not in {"http", "https"}
            or p.username is not None
            or p.password is not None
            or not host
            or "\\" in value
            or "%" in host
        ):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if (
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
                or "." not in host
                or any(
                    host == s or host.endswith("." + s)
                    for s in ("localhost", "local", "internal", "home", "lan", "test", "invalid")
                )
            ):
                raise ValueError()
        else:
            if not public_address(host):
                raise ValueError()
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        authority = f"[{host}]" if ":" in host else host
        if port is not None:
            authority += ":" + str(port)
        return urlunsplit((p.scheme, authority, p.path or "/", p.query, ""))
    except (ValueError, UnicodeError):
        raise SearchError("invalid_public_url") from None


async def validate_fetch_url(value):
    url = public_url(value)
    p = urlsplit(url)
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            p.hostname, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
    except OSError:
        raise SearchError("page_dns_failed") from None
    if not records or any(not public_address(row[4][0]) for row in records):
        raise SearchError("non_public_page_address")
    return url
