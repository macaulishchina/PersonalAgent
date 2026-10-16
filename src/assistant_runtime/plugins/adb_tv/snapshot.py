class Snapshot:
    """Advance confirmed state only after the observation batch commits."""

    def __init__(self):
        self.previous, self.at, self.missing = None, None, {}

    def prepare(self, current, now):
        events = []
        start = self.at if self.at is not None else now
        if self.previous is None or current != self.previous:
            events.append(
                (
                    "tv.baseline" if self.previous is None else "tv.changed",
                    "television",
                    {"before": self.previous, "after": current},
                )
            )
        return events, current, {}, start

    def commit(self, prepared, now):
        _, self.previous, self.missing, _ = prepared
        self.at = now
