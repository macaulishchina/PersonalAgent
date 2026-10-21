"""Durable acquisition receipts, collection facts and replay-safe notification outbox."""

import base64
import hashlib
import json
import time
import uuid
from contextvars import ContextVar

CURRENT_BATCH = ContextVar("media_batch", default=None)

TERMINAL = {"completed", "failed", "cancelled"}
STATES = TERMINAL | {
    "queued",
    "checking",
    "submitting",
    "verifying",
    "uncertain",
    "waiting_auth",
    "downloading",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class MediaRepository:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        db.executescript("""
            CREATE TABLE IF NOT EXISTS media_operations(
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
                identity TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                updated REAL NOT NULL, due REAL NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS media_due ON media_operations(state,due);
            CREATE INDEX IF NOT EXISTS media_page ON media_operations(created DESC,id DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS media_identity ON media_operations(identity)
                WHERE state NOT IN ('failed','cancelled');
            CREATE TABLE IF NOT EXISTS media_account(id TEXT PRIMARY KEY,state TEXT NOT NULL,generation INTEGER);
            CREATE TABLE IF NOT EXISTS media_outbox(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
        """)

    def account(self, account="aliyun"):
        row = self.db.execute("SELECT state,generation FROM media_account WHERE id=?", (account,)).fetchone()
        return {"state": row[0], "generation": row[1]} if row else {"state": "unknown", "generation": 0}

    def account_state(self, state, *, account="aliyun"):
        previous = self.account(account)
        if state == previous["state"]:
            return previous
        generation = previous["generation"] + 1
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO media_account VALUES(?,?,?)", (account, state, generation)
            )
            if state == "auth_required":
                self.enqueue(
                    "media-auth:" + account + ":" + str(generation),
                    account + " 需要重新登录",
                    "该网盘的媒体操作已等待授权，请按账号登录方式更新私有凭据。已有结果和进度保留。",
                )
        return self.account(account)

    def enqueue(self, key, title, body):
        self.db.execute(
            "INSERT OR IGNORE INTO media_outbox VALUES(?,?)",
            (key, canonical({"event_key": key, "title": title, "body": body})),
        )

    def create(self, request_id, intent, owner, caller_run=None, *, initial=None):
        try:
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Media writes require a canonical UUID request_id") from None
        fingerprint = hashlib.sha256(canonical({"intent": intent, "owner": owner}).encode()).hexdigest()
        prior = self.db.execute(
            "SELECT id,fingerprint FROM media_operations WHERE request_id=?", (request_id,)
        ).fetchone()
        if prior:
            if prior[1] != fingerprint:
                raise ValueError("Media request_id conflicts with an earlier request")
            return self.get(prior[0])
        keys = (
            ("kind", "plan_id", "target")
            if intent.get("kind") == "library"
            else ("kind", "file_id", "target", "local_destination")
            if intent.get("kind") == "download"
            else (
                ("collection", "season", "episode", "target")
                if "episode" in intent
                else ("share_id", "file_id", "target")
            )
        )
        identity = hashlib.sha256(
            canonical(
                {k: intent[k] for k in keys}
                | ({"version": intent["version"]} if intent.get("version") else {})
                | {"destination": intent.get("root_destination", intent["destination"])}
            ).encode()
        ).hexdigest()
        prior = self.db.execute(
            "SELECT id FROM media_operations WHERE identity=? AND state NOT IN ('failed','cancelled')",
            (identity,),
        ).fetchone()
        if prior:
            return self.get(prior[0])
        count = self.db.execute("SELECT count(*) FROM media_operations").fetchone()[0]
        pending = self.db.execute(
            "SELECT count(*) FROM media_operations WHERE state NOT IN ('completed','failed','cancelled')"
        ).fetchone()[0]
        if count >= self.settings.max_operations or pending >= self.settings.queue_capacity:
            raise ValueError("Media operation capacity reached")
        now, ident = time.time(), uuid.uuid4().hex
        data = {
            "intent": intent,
            "owner": owner,
            "caller_run": caller_run,
            "attempts": 0,
            "created_at": now,
            "stages": [],
            "batch_id": CURRENT_BATCH.get(),
            **(initial or {}),
        }
        with self.db:
            self.db.execute(
                "INSERT INTO media_operations VALUES(?,?,?,?,?,?,?,?,?)",
                (ident, request_id, fingerprint, identity, "queued", now, now, now, canonical(data)),
            )
        return self.get(ident)

    def get(self, ident):
        row = self.db.execute(
            "SELECT id,state,created,updated,due,data FROM media_operations WHERE id=?", (ident,)
        ).fetchone()
        if not row:
            raise ValueError("Media operation not found")
        return dict(
            zip(("id", "state", "created_at", "updated_at", "due", "data"), (*row[:5], json.loads(row[5])))
        )

    def change(self, ident, state, *, due=None, **changes):
        if state not in STATES:
            raise ValueError("Invalid media state")
        item, now = self.get(ident), time.time()
        if state not in {"failed", "cancelled"} and item["state"] in {"failed", "cancelled"}:
            newer = self.db.execute(
                "SELECT id FROM media_operations WHERE identity=(SELECT identity FROM media_operations WHERE id=?) "
                "AND id!=? AND state NOT IN ('failed','cancelled')",
                (ident, ident),
            ).fetchone()
            if newer:
                raise ValueError("A newer acquisition already owns this resource; query " + newer[0])
        data = item["data"]
        if item["state"] != state:
            data["stages"] = (data["stages"] + [{"state": state, "at": now}])[-16:]
        data.update(changes)
        if state in TERMINAL and item["state"] not in TERMINAL:
            data["duration_seconds"] = round(now - item["created_at"], 3)
            data["finished_at"] = now
        with self.db:
            self.db.execute(
                "UPDATE media_operations SET state=?,updated=?,due=?,data=? WHERE id=?",
                (state, now, now if due is None else due, canonical(data), ident),
            )
            if state == "completed" and item["state"] != "completed" and not data.get("batch_id"):
                managed = data["intent"].get("kind") == "library"
                local = data["intent"].get("kind") == "download"
                self.enqueue(
                    "media-complete:" + ident,
                    "媒体整理已完成" if managed else "影视资源已下载" if local else "影视资源已转存",
                    ("已核验 " + str(len(data["steps"])) + " 项操作。")
                    if managed
                    else data["file"]["name"]
                    + (" 已下载到本地并通过完整校验。耗时 " if local else " 已在目标网盘核验。耗时 ")
                    + str(data["duration_seconds"])
                    + " 秒。",
                )
            elif state in {"failed", "uncertain"} and state != item["state"] and not data.get("batch_id"):
                self.enqueue(
                    "media-attention:" + ident,
                    "媒体操作需要处理",
                    "请查询媒体操作 " + ident + " 的最新状态。",
                )
        return self.get(ident)

    def recover(self):
        # A process may have died after the server accepted copy but before the reply.
        with self.db:
            self.db.execute("UPDATE media_operations SET state='queued' WHERE state='checking'")
            self.db.execute("UPDATE media_operations SET state='verifying' WHERE state='submitting'")
            self.db.execute("UPDATE media_operations SET state='queued' WHERE state='downloading'")

    def due(self, kind="transfer"):
        row = self.db.execute(
            "SELECT id FROM media_operations WHERE state IN ('queued','verifying') AND due<=? "
            "AND coalesce(json_extract(data,'$.intent.kind'),'transfer')=? "
            "ORDER BY due,id LIMIT 1",
            (time.time(), kind),
        ).fetchone()
        return self.get(row[0]) if row else None

    def resume_auth(self, account="aliyun"):
        for (ident,) in self.db.execute(
            "SELECT id FROM media_operations WHERE state='waiting_auth' AND json_extract(data,'$.intent.destination.account')=?",
            (account,),
        ).fetchall():
            item = self.get(ident)
            self.change(ident, "verifying" if item["data"].get("submitted") else "queued")

    def page(self, *, before=None, limit=20, collection=None):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Media page limit must be 1..50")
        where, values = [], []
        if collection is not None:
            where.append("json_extract(data,'$.intent.collection')=?")
            values.append(collection)
        if before:
            try:
                cursor = json.loads(base64.urlsafe_b64decode(before).decode())
                if cursor[2] != collection or len(cursor) != 3:
                    raise ValueError()
                where.append("(created,id)<(?,?)")
                values.extend(cursor[:2])
            except Exception:
                raise ValueError("Invalid media cursor or filter scope") from None
        sql = "SELECT id FROM media_operations" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self.db.execute(
            sql + " ORDER BY created DESC,id DESC LIMIT ?", (*values, limit + 1)
        ).fetchall()
        items = [self.get(row[0]) for row in rows[:limit]]
        last = items[-1] if items else None
        cursor = (
            base64.urlsafe_b64encode(
                canonical([last["created_at"], last["id"], collection]).encode()
            ).decode()
            if last and len(rows) > limit
            else None
        )
        return {"items": [self.public(item) for item in items], "before": cursor}

    @staticmethod
    def public(item):
        data = item["data"]
        return {
            "operation_id": item["id"],
            "state": item["state"],
            "outcome": "partial"
            if item["state"] in {"failed", "cancelled"}
            and any(step["state"] == "completed" for step in data.get("steps", []))
            else item["state"],
            "created_at": item["created_at"],
            "updated_at": item["updated_at"],
            "target": data["intent"]["target"],
            "account": data["intent"]["destination"]["account"],
            "kind": data["intent"].get("kind", "transfer"),
            "origin": {"owner": data["owner"], "run_id": data.get("caller_run")},
            "collection": data["intent"].get("collection"),
            "season": data["intent"].get("season"),
            "episode": data["intent"].get("episode"),
            "version": data["intent"].get("version"),
            **{
                k: data[k]
                for k in (
                    "file",
                    "saved_file_id",
                    "verification",
                    "duration_seconds",
                    "error",
                    "stages",
                    "openlist",
                    "progress",
                    "local_path",
                    "steps",
                )
                if k in data
            },
            "cancel_requested": data.get("cancel_requested", False),
        }
