class Snapshot:
    """Advance confirmed state only after the observation batch commits."""

    def __init__(self):
        self.previous, self.at, self.missing = None, None, {}

    def prepare(self, current, now):
        events, next_missing = [], dict(self.missing)
        start = self.at if self.at is not None else now
        if self.previous is None:
            events = [("lan.baseline", mac, {"after": fields}) for mac, fields in current.items()]
            # Empty baseline must still prove a successful complete observation.
            if not events:
                events.append(("lan.baseline", "network", {"clients": 0}))
            return events, current, {}, start
        confirmed = dict(current)
        for mac, fields in current.items():
            next_missing.pop(mac, None)
            old = self.previous.get(mac)
            keys = ("userIp", "connectType", "sn", "ssid", "band")
            if old is None or any(old[k] != fields[k] for k in keys):
                events.append(
                    ("lan.appeared" if old is None else "lan.changed", mac, {"before": old, "after": fields})
                )
        for mac in self.previous.keys() - current.keys():
            count, first = next_missing.get(mac, (0, start))
            next_missing[mac] = (count + 1, first)
            if count + 1 >= 2:
                events.append(
                    (
                        "lan.disappeared",
                        mac,
                        {"before": self.previous[mac], "first_missing_interval_start": first},
                    )
                )
                start = min(start, first)
                next_missing.pop(mac)
            else:
                confirmed[mac] = self.previous[mac]
        return events, confirmed, next_missing, start

    def commit(self, prepared, now):
        _, self.previous, self.missing, _ = prepared
        self.at = now
