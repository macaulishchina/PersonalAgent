"""Owner-scoped private HTTP entrances advertised only on the operator channel."""

import ipaddress
import secrets


class HttpFrontend:
    def __init__(self):
        self.key = secrets.token_urlsafe(32)
        self.endpoint = None
        self.management = None

    def publish(self, host, port):
        address = ipaddress.ip_address(host)
        if not address.is_loopback or not 1 <= port <= 65535:
            raise ValueError("HTTP frontend must use a local transport")
        self.endpoint = {"host": host, "port": port, "key": self.key}

    def register_management(self, callback):
        self.management = callback

    def withdraw(self):
        self.endpoint = None
        self.management = None


def scoped(host, owner):
    if not hasattr(host, "http_frontends"):
        host.http_frontends = {}
    return host.http_frontends.setdefault(owner, HttpFrontend())
