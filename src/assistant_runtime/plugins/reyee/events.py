"""Reyee owns polling, complete snapshots and client transition semantics."""

import ipaddress
import re
from urllib.parse import urlsplit
from ..observations.polling import PollingSource
from ..observations.schema import fields, interval, schema
from .protocol import Reyee
from .snapshot import Snapshot


class ReyeeEvents:
    max_instances = 64
    description = schema(
        {
            "url": {"type": "string"},
            "credential": {"type": "string"},
            "interval": {"type": "number", "minimum": 10, "maximum": 3600, "default": 30},
        },
        ["lan.baseline", "lan.appeared", "lan.disappeared", "lan.changed"],
        ["url", "credential"],
    )

    def __init__(self, directory):
        self.directory = directory

    def validate(self, config):
        fields(config, ("url", "credential", "interval"))
        url = urlsplit(config.get("url", ""))
        if (
            url.scheme not in {"http", "https"}
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("Router URL must contain only scheme and IP authority")
        ipaddress.ip_address(url.hostname)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", config.get("credential", "")):
            raise ValueError("Credential must reference a private credential filename")
        return dict(config, interval=interval(config, 30, 10))

    def create(self, reporter, config):
        return PollingSource(reporter, Reyee(config, self.directory), Snapshot, config["interval"])
