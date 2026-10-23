"""Strict instance settings; importing configuration needs no HomeKit library."""

from assistant_runtime.api.configuration import configuration_default

from dataclasses import dataclass, field
import ipaddress


@dataclass(frozen=True)
class AppleHomeSettings:
    host: str = field(default_factory=lambda: configuration_default(__file__, "host"))
    port: int = field(default_factory=lambda: configuration_default(__file__, "port"))
    hap_port: int = field(default_factory=lambda: configuration_default(__file__, "hap_port"))
    poll_seconds: int = field(default_factory=lambda: configuration_default(__file__, "poll_seconds"))
    refresh_timeout: int = field(default_factory=lambda: configuration_default(__file__, "refresh_timeout"))
    log_invalid_values: bool = field(
        default_factory=lambda: configuration_default(__file__, "log_invalid_values")
    )

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - cls.__dataclass_fields__.keys():
            raise ValueError("Invalid Apple Home bridge settings")
        s = cls(**data)
        if type(s.log_invalid_values) is not bool:
            raise ValueError("Apple Home log_invalid_values must be boolean")
        address = ipaddress.ip_address(s.host)
        if address.is_unspecified or address.is_multicast:
            raise ValueError("Apple Home bridge requires an explicit interface IP")
        for port in (s.port, s.hap_port):
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("Invalid Apple Home bridge port")
        if s.port == s.hap_port:
            raise ValueError("Receiver and HomeKit ports must differ")
        if type(s.poll_seconds) is not int or not 30 <= s.poll_seconds <= 3600:
            raise ValueError("Apple Home poll interval must be 30..3600 seconds")
        if type(s.refresh_timeout) is not int or not 1 <= s.refresh_timeout <= 10:
            raise ValueError("Apple Home refresh timeout must be 1..10 seconds")
        return s


def endpoint_settings(spec):
    c = spec["connection"]
    if spec["kind"] != "sensor" or set(c) - {"metrics", "trigger_name"}:
        raise ValueError("Apple Home endpoint must be a sensor with metrics/trigger_name settings")
    metrics = c.get("metrics", ["temperature", "humidity"])
    if (
        not isinstance(metrics, list)
        or not metrics
        or any(not isinstance(m, str) or m not in {"temperature", "humidity"} for m in metrics)
        or len(set(metrics)) != len(metrics)
    ):
        raise ValueError("Unsupported Apple Home sensor metrics")
    name = c.get("trigger_name", "读取" + spec["label"])
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        raise ValueError("Invalid Apple Home refresh trigger name")
    return tuple(metrics), name
