"""APScheduler supplies calendar arithmetic; SQLite owns schedule cursors."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger


def trigger(spec, zone, created):
    kind = spec.get("kind", "immediate")
    tz = ZoneInfo(spec.get("timezone", zone))
    if kind in {"immediate", "event"}:
        return None
    if kind == "date":
        dt = datetime.fromisoformat(spec["at"])
        if dt.tzinfo is None:
            raise ValueError("Task date must include timezone")
        return DateTrigger(run_date=dt)
    if kind == "interval":
        seconds = spec["seconds"]
        if type(seconds) not in (int, float) or not 1 <= seconds <= 366 * 86400:
            raise ValueError("Task interval must be 1 second..366 days")
        return IntervalTrigger(
            seconds=seconds, start_date=datetime.fromtimestamp(created, timezone.utc), timezone=tz
        )
    if kind == "cron":
        return CronTrigger.from_crontab(spec["expression"], timezone=tz)
    raise ValueError("Unsupported trigger kind")


def next_time(spec, zone, created, after=None):
    t = trigger(spec, zone, created)
    if t is None:
        return created if spec.get("kind", "immediate") == "immediate" and after is None else None
    now = datetime.fromtimestamp(created if after is None else after + 0.001, timezone.utc)
    previous = None if after is None else datetime.fromtimestamp(after, timezone.utc)
    value = t.get_next_fire_time(previous, now)
    return value.timestamp() if value else None
