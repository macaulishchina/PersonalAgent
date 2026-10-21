"""Source-scoped associations and bounded inbox projections; never infer business success."""

import base64
import hashlib
import json
import math
import re
import time

from .policy import ALERT_SQL


def association(source, key, payload):
    explicit = payload.get("group", {})
    origin = payload.get("origin", {})
    identity = (
        "topic:" + explicit["key"]
        if explicit.get("key")
        else "run:" + origin["run_id"]
        if origin.get("run_id")
        else "event:" + key
    )
    ident = hashlib.sha256(json.dumps([source, identity]).encode()).hexdigest()
    title = explicit.get("title") or origin.get("task_title") or payload["title"]
    return ident, title


def page_cursor(scope, before, limit):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Group page limit must be 1..50")
    if not before:
        return time.time(), None
    try:
        if not isinstance(before, str) or len(before) > 1000:
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(before))
        stamp, created, ident = data["at"], data["created"], data["id"]
        if data["scope"] != scope or any(
            type(v) not in (int, float) or not math.isfinite(v) for v in (stamp, created)
        ):
            raise ValueError()
        if (
            not 0 <= created <= stamp <= time.time()
            or not isinstance(ident, str)
            or not re.fullmatch(r"[0-9a-f]{32,64}", ident)
        ):
            raise ValueError()
        return stamp, (created, ident)
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("Invalid notification group cursor") from None


def cursor(scope, stamp, created, ident):
    return base64.urlsafe_b64encode(
        json.dumps({"scope": scope, "at": stamp, "created": created, "id": ident}).encode()
    ).decode()


class Groups:
    def __init__(self, repo):
        self.repo, self.db = repo, repo.db
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(notifications)")}
        with self.db:
            for name in ("group_id", "group_title"):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE notifications ADD COLUMN {name} TEXT")
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS notification_group_page ON notifications(group_id,created DESC,id DESC)"
            )
        while True:
            rows = self.db.execute(
                "SELECT id,source,event_key,payload FROM notifications WHERE group_id IS NULL LIMIT 100"
            ).fetchall()
            if not rows:
                break
            with self.db:
                for ident, source, key, raw in rows:
                    group, title = association(source, key, json.loads(raw))
                    self.db.execute(
                        "UPDATE notifications SET group_id=?,group_title=? WHERE id=?", (group, title, ident)
                    )

    def project_chat(self, messages):
        """Enrich a bounded timeline page without rewriting messages or delivery state."""
        references = {}
        for message in messages:
            if message.get("role") != "assistant" or message.get("kind") != "notification":
                continue
            match = re.fullmatch(r"notification:([A-Za-z0-9_-]{1,128})", message.get("turn_id") or "")
            if match:
                references.setdefault(match[1], []).append(message)
        if not references:
            return
        placeholders = ",".join("?" for _ in references)
        rows = self.db.execute(
            f"SELECT id,source,group_id,group_title,payload FROM notifications WHERE id IN ({placeholders})",
            tuple(references),
        ).fetchall()
        for ident, source, group_id, title, raw in rows:
            payload = json.loads(raw)
            origin = payload.get("origin", {})
            task = origin.get("task_id")
            trusted_task = task if task and source == "task:" + task else None
            for message in references[ident]:
                message["report_group"] = {
                    "id": group_id,
                    **({"kind": payload["kind"]} if "kind" in payload else {}),
                    "title": title,
                    "run_id": origin.get("run_id") if trusted_task else None,
                    "task_id": trusted_task,
                    "important": payload.get("priority") == "important",
                }
                if trusted_task:
                    message["task_id"] = trusted_task

    def list(self, unread=False, before=None, limit=20):
        if type(unread) is not bool:
            raise ValueError("unread must be boolean")
        scope = ["groups", unread]
        stamp, last = page_cursor(scope, before, limit)
        created, ident = last or (0, "")
        rows = self.db.execute(
            f"""
            SELECT group_id,max(created) AS latest,count(*) AS total,
              sum(CASE WHEN {ALERT_SQL} AND read_revision<revision AND (expires IS NULL OR expires>?) THEN 1 ELSE 0 END) AS unread,
              sum(CASE WHEN json_extract(payload,'$.priority')='important' AND (expires IS NULL OR expires>?) THEN 1 ELSE 0 END) AS important
            FROM notifications WHERE created<=? GROUP BY group_id
            HAVING (?=0 OR unread>0) AND (?=0 OR latest<? OR (latest=? AND group_id<?))
            ORDER BY latest DESC,group_id DESC LIMIT ?
            """,
            (stamp, stamp, stamp, unread, bool(last), created, created, ident, limit + 1),
        ).fetchall()
        items = []
        for group, latest, total, count, important in rows[:limit]:
            row = self.db.execute(
                f"""SELECT id FROM notifications WHERE group_id=? AND created<=?
                ORDER BY CASE
                  WHEN {ALERT_SQL} AND read_revision<revision AND (expires IS NULL OR expires>?) THEN 2
                  WHEN json_extract(payload,'$.kind')='result' THEN 1 ELSE 0 END DESC,
                  created DESC,id DESC LIMIT 1""",
                (group, stamp, stamp),
            ).fetchone()
            notice = self.repo.get(row[0])
            items.append(
                {
                    "id": group,
                    "title": notice["group_title"],
                    "total": total,
                    "unread": count,
                    "important": important,
                    "latest": notice,
                    "created": latest,
                    "read_before": stamp,
                }
            )
        return {
            "items": items,
            "unread": self.repo.unread_count(),
            "read_before": stamp,
            "before": cursor(scope, stamp, rows[limit - 1][1], rows[limit - 1][0])
            if len(rows) > limit
            else None,
        }

    def members(self, group_id, before=None, limit=20, snapshot=None):
        self.validate_id(group_id)
        scope = ["members", group_id]
        stamp, last = page_cursor(scope, before, limit)
        if snapshot is not None:
            if (
                type(snapshot) not in (int, float)
                or not math.isfinite(snapshot)
                or not 0 <= snapshot <= time.time()
            ):
                raise ValueError("Invalid notification group snapshot")
            if before and snapshot != stamp:
                raise ValueError("Group cursor snapshot mismatch")
            stamp = snapshot
        created, ident = last or (0, "")
        rows = self.db.execute(
            """SELECT id,created FROM notifications WHERE group_id=? AND created<=?
            AND (?=0 OR created<? OR (created=? AND id<?)) ORDER BY created DESC,id DESC LIMIT ?""",
            (group_id, stamp, bool(last), created, created, ident, limit + 1),
        ).fetchall()
        return {
            "items": [self.repo.get(row[0]) for row in rows[:limit]],
            "read_before": stamp,
            "before": cursor(scope, stamp, rows[limit - 1][1], rows[limit - 1][0])
            if len(rows) > limit
            else None,
        }

    def read(self, group_id, before):
        self.validate_id(group_id)
        if type(before) not in (int, float) or not math.isfinite(before) or not 0 <= before <= time.time():
            raise ValueError("Group read requires its snapshot timestamp")
        with self.db:
            count = self.db.execute(
                f"""UPDATE notifications SET read_revision=revision WHERE group_id=?
                AND {ALERT_SQL} AND read_revision<revision AND updated<=? AND (expires IS NULL OR expires>?)""",
                (group_id, before, time.time()),
            ).rowcount
        return {"marked_read": count, "unread": self.repo.unread_count()}

    @staticmethod
    def validate_id(group_id):
        if not isinstance(group_id, str) or not re.fullmatch(r"[0-9a-f]{64}", group_id):
            raise ValueError("Invalid notification group")

    def link(self, source, event_key, target, title):
        row = self.db.execute(
            "SELECT id FROM notifications WHERE source=? AND event_key=?", (source, event_key)
        ).fetchone()
        if not row:
            return {"linked": False}
        item = self.repo.get(row[0])
        if item["group_id"] != target:
            # Trusted owner may enrich a previously unassociated event, never move a known association.
            fallback, _ = association(source, event_key, {"title": item["payload"]["title"]})
            if item["group_id"] != fallback:
                raise ValueError("Notification already has a different association")
            with self.db:
                self.db.execute(
                    "UPDATE notifications SET group_id=?,group_title=? WHERE id=?",
                    (target, title or item["payload"]["title"], item["id"]),
                )
        return {"linked": True, "id": item["id"]}
