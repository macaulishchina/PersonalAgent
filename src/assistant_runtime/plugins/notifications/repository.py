"""SQLite inbox/outbox. Transactions contain no network, model or hardware work."""

import hashlib
import math
import json
import time
import uuid


class Notifications:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        db.executescript("""
        CREATE TABLE IF NOT EXISTS notifications (
          id TEXT PRIMARY KEY, source TEXT NOT NULL, event_key TEXT NOT NULL,
          fingerprint TEXT NOT NULL, payload TEXT NOT NULL, revision INTEGER NOT NULL,
          read_revision INTEGER NOT NULL DEFAULT 0,
          created REAL NOT NULL, updated REAL NOT NULL, expires REAL,
          UNIQUE(source,event_key));
        CREATE TABLE IF NOT EXISTS notification_deliveries (
          notification_id TEXT NOT NULL, revision INTEGER NOT NULL, channel TEXT NOT NULL,
          state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL,
          lease_until REAL, error TEXT, updated REAL NOT NULL,
          PRIMARY KEY(notification_id,revision,channel));
        CREATE INDEX IF NOT EXISTS notification_delivery_due ON notification_deliveries(channel,state,next_at);
        CREATE INDEX IF NOT EXISTS notifications_page ON notifications(created DESC,id DESC);
        CREATE INDEX IF NOT EXISTS notifications_unread_page ON notifications(created DESC,id DESC)
          WHERE read_revision<revision;
        """)
        if "archived" in {row[1] for row in db.execute("PRAGMA table_info(notifications)")}:
            # Old dismissed notices become visible read history. Preserve IDs,
            # content revisions and delivery receipts; never enqueue them again.
            with db:
                db.execute("UPDATE notifications SET read_revision=revision WHERE archived=1")
                db.execute("ALTER TABLE notifications DROP COLUMN archived")

    def get(self, ident):
        row = self.db.execute(
            "SELECT id,source,event_key,fingerprint,payload,revision,read_revision,created,updated,expires FROM notifications WHERE id=?",
            (ident,),
        ).fetchone()
        if not row:
            raise ValueError("Notification not found")
        keys = (
            "id",
            "source",
            "event_key",
            "fingerprint",
            "payload",
            "revision",
            "read_revision",
            "created",
            "updated",
            "expires",
        )
        item = dict(zip(keys, row))
        item["payload"] = json.loads(item["payload"])
        item["unread"] = item["read_revision"] < item["revision"]
        item["expired"] = item["expires"] is not None and item["expires"] <= time.time()
        item["deliveries"] = [
            dict(zip(("channel", "state", "attempts", "error"), r))
            for r in self.db.execute(
                "SELECT channel,state,attempts,error FROM notification_deliveries WHERE notification_id=? AND revision=?",
                (ident, item["revision"]),
            )
        ]
        return item

    def unread_count(self):
        return self.db.execute(
            "SELECT count(*) FROM notifications WHERE read_revision<revision AND (expires IS NULL OR expires>?)",
            (time.time(),),
        ).fetchone()[0]

    def list(self, *, unread=False, before=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Notification limit must be 1..100")
        read_before = time.time()
        cursor = self.get(before) if before else None
        rows = self.db.execute(
            """SELECT id FROM notifications WHERE (?=0 OR (read_revision<revision AND (expires IS NULL OR expires>?)))
            AND (? IS NULL OR created<? OR (created=? AND id<?))
            ORDER BY created DESC,id DESC LIMIT ?""",
            (
                bool(unread),
                time.time(),
                before,
                cursor["created"] if cursor else 0,
                cursor["created"] if cursor else 0,
                before,
                limit,
            ),
        ).fetchall()
        return {
            "read_before": read_before,
            "items": [self.get(r[0]) for r in rows],
            "unread": self.unread_count(),
            "before": rows[-1][0] if len(rows) == limit else None,
        }

    def publish(self, source, key, payload, channels, *, ident=None, revision=None):
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        fingerprint = hashlib.sha256(raw.encode()).hexdigest()
        now = time.time()
        with self.db:
            if ident:
                old = self.get(ident)
                if old["source"] != source:
                    raise ValueError("Cannot update another producer's notification")
                # A retried correction is idempotent, including after a lost reply.
                if old["fingerprint"] == fingerprint:
                    return old
                if type(revision) is not int or old["revision"] != revision:
                    raise ValueError("Notification changed; read latest revision")
                new_revision = revision + 1
                self.db.execute(
                    "UPDATE notifications SET payload=?,fingerprint=?,revision=?,updated=?,expires=? WHERE id=?",
                    (raw, fingerprint, new_revision, now, payload.get("expires_at"), ident),
                )
                self.db.execute(
                    "UPDATE notification_deliveries SET state='superseded',updated=? WHERE notification_id=? AND state IN ('pending','inflight')",
                    (now, ident),
                )
            else:
                old = self.db.execute(
                    "SELECT id,fingerprint FROM notifications WHERE source=? AND event_key=?", (source, key)
                ).fetchone()
                if old:
                    if old[1] != fingerprint:
                        raise ValueError("Notification key already exists with different content; use update")
                    return self.get(old[0])
                if (
                    self.db.execute("SELECT count(*) FROM notifications").fetchone()[0]
                    >= self.settings.max_notifications
                ):
                    raise ValueError("Notification inbox capacity reached; increase max_notifications")
                ident, new_revision = uuid.uuid4().hex, 1
                self.db.execute(
                    "INSERT INTO notifications(id,source,event_key,fingerprint,payload,revision,read_revision,created,updated,expires) VALUES (?,?,?,?,?,?,0,?,?,?)",
                    (ident, source, key, fingerprint, raw, new_revision, now, now, payload.get("expires_at")),
                )
            for channel in channels:
                self.db.execute(
                    "INSERT INTO notification_deliveries VALUES (?,?,?,'pending',0,?,NULL,NULL,?)",
                    (ident, new_revision, channel, now, now),
                )
        return self.get(ident)

    def mark_read(self, ident, revision):
        item = self.get(ident)
        if type(revision) is not int or revision != item["revision"]:
            raise ValueError("Notification changed; read latest revision")
        with self.db:
            self.db.execute("UPDATE notifications SET read_revision=revision WHERE id=?", (ident,))
        return self.get(ident)

    def read_all(self, before):
        if type(before) not in (int, float) or not math.isfinite(before) or before > time.time():
            raise ValueError("read_all requires the inbox snapshot timestamp")
        with self.db:
            count = self.db.execute(
                """UPDATE notifications SET read_revision=revision
                WHERE read_revision<revision AND updated<=?
                AND (expires IS NULL OR expires>?)""",
                (before, time.time()),
            ).rowcount
        return {"marked_read": count, "unread": self.unread_count()}

    def recover(self, channel, replay_safe):
        # One application process owns this database (host instance lock).
        with self.db:
            self.db.execute(
                "UPDATE notification_deliveries SET state=?,lease_until=NULL,error=?,updated=? WHERE channel=? AND state='inflight'",
                ("pending" if replay_safe else "unknown", "服务重启，投递确认中断", time.time(), channel),
            )

    def claim(self, channel):
        now = time.time()
        with self.db:
            self.db.execute(
                """UPDATE notification_deliveries SET state='expired',updated=?
              WHERE state='pending' AND notification_id IN (SELECT id FROM notifications WHERE expires<=?)""",
                (now, now),
            )
            row = self.db.execute(
                """SELECT notification_id,revision FROM notification_deliveries
              WHERE channel=? AND state='pending' AND next_at<=? ORDER BY next_at LIMIT 1""",
                (channel, now),
            ).fetchone()
            if not row:
                return None
            self.db.execute(
                """UPDATE notification_deliveries SET state='inflight',attempts=attempts+1,
              lease_until=?,updated=? WHERE notification_id=? AND revision=? AND channel=?""",
                (now + self.settings.delivery_timeout + 5, now, *row, channel),
            )
        return self.get(row[0])

    def finish(self, item, channel, state, error=None, delay=0, deferred=False):
        with self.db:
            self.db.execute(
                """UPDATE notification_deliveries SET state=?,error=?,next_at=?,lease_until=NULL,
              attempts=attempts-?,updated=? WHERE notification_id=? AND revision=? AND channel=? AND state='inflight'""",
                (
                    state,
                    error,
                    time.time() + delay,
                    int(deferred),
                    time.time(),
                    item["id"],
                    item["revision"],
                    channel,
                ),
            )

    def retry(self, ident, revision, channel):
        item = self.get(ident)
        if item["revision"] != revision or item["expired"]:
            raise ValueError("Notification is outdated or expired")
        with self.db:
            changed = self.db.execute(
                """UPDATE notification_deliveries SET state='pending',attempts=0,next_at=?,error=NULL
                WHERE notification_id=? AND revision=? AND channel=? AND state IN ('failed','unknown')""",
                (time.time(), ident, revision, channel),
            ).rowcount
        if not changed:
            raise ValueError("Delivery is not retryable")
        return self.get(ident)
