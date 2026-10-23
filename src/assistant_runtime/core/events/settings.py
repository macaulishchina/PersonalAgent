from dataclasses import dataclass


@dataclass(frozen=True)
class EventSettings:
    quota_bytes: int = 2 * 1024**3
    queue_size: int = 128
    query_seconds: float = 2.0

    @classmethod
    def parse(cls, values):
        if not isinstance(values, dict) or set(values) - cls.__dataclass_fields__.keys():
            raise ValueError("Unknown events settings")
        settings = cls(**values)
        if type(settings.quota_bytes) is not int or not 1024**2 <= settings.quota_bytes <= 1024**4:
            raise ValueError("events.quota_bytes must be 1 MiB..1 TiB")
        if type(settings.queue_size) is not int or not 8 <= settings.queue_size <= 1024:
            raise ValueError("events.queue_size must be 8..1024")
        if type(settings.query_seconds) not in (int, float) or not 0.1 <= settings.query_seconds <= 10:
            raise ValueError("events.query_seconds must be 0.1..10")
        return settings
