"""ADB television owns read-only sampling and power/display interpretation."""

import ipaddress
from ..observations.polling import PollingSource
from ..observations.schema import fields, interval, schema
from .protocol import AdbTV
from .snapshot import Snapshot


class TelevisionEvents:
    max_instances = 64
    description = schema(
        {
            "address": {"type": "string"},
            "interval": {"type": "number", "minimum": 5, "maximum": 3600, "default": 5},
        },
        ["tv.baseline", "tv.changed"],
        ["address"],
    )

    def validate(self, config):
        fields(config, ("address", "interval"))
        host, port = config.get("address", "").rsplit(":", 1)
        ipaddress.ip_address(host)
        if not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError("Invalid ADB port")
        return dict(config, interval=interval(config, 5, 5))

    def create(self, reporter, config):
        return PollingSource(reporter, AdbTV(config, None), Snapshot, config["interval"])
