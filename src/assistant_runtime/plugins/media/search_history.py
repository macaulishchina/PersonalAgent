"""Bounded search receipts and short-lived provider observations, never model conclusions."""

import asyncio
import hashlib
import json
import time
import weakref

from .errors import MediaError


class SearchHistory:
    def __init__(self, db):
        self.db = db
        self.locks = weakref.WeakValueDictionary()
        db.executescript("""
        CREATE TABLE IF NOT EXISTS media_search_history(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, observed REAL NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS media_share_checks(
          identity TEXT PRIMARY KEY, share_id TEXT NOT NULL, observed REAL NOT NULL,
          data TEXT NOT NULL);
        """)

    def record(self, receipt):
        with self.db:
            self.db.execute(
                "INSERT INTO media_search_history(observed,data) VALUES(?,?)",
                (time.time(), json.dumps(receipt, ensure_ascii=False)),
            )
            self.db.execute(
                "DELETE FROM media_search_history WHERE seq NOT IN "
                "(SELECT seq FROM media_search_history ORDER BY seq DESC LIMIT 200)"
            )

    def list(self, before=None, limit=20):
        if (
            type(limit) is not int
            or not 1 <= limit <= 50
            or (before is not None and (type(before) is not int or before < 1))
        ):
            raise ValueError("Invalid search history page")
        rows = self.db.execute(
            "SELECT seq,observed,data FROM media_search_history WHERE (? IS NULL OR seq<?) "
            "ORDER BY seq DESC LIMIT ?",
            (before, before, limit + 1),
        ).fetchall()
        return {
            "items": [{"id": seq, "observed_at": at, **json.loads(data)} for seq, at, data in rows[:limit]],
            "before": rows[limit - 1][0] if len(rows) > limit else None,
            "retention": "latest_200_searches",
            "note": "Historical observations, not current availability or proof of absence.",
        }

    async def inspect(self, provider, values, refresh=False):
        if type(refresh) is not bool:
            raise ValueError("Invalid inspection refresh")
        # Password changes and directory/page scopes must never reuse a different observation.
        identity = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        requested = time.time()
        async with self.locks.setdefault(identity, asyncio.Lock()):
            row = self.db.execute(
                "SELECT observed,data FROM media_share_checks WHERE identity=?", (identity,)
            ).fetchone()
            if row and requested - row[0] < 120 and (not refresh or row[0] >= requested):
                data = json.loads(row[1])
                if data.get("error"):
                    raise MediaError(data["error"])
                return {**data["result"], "inspection": {"observed_at": row[0], "cached": True}}
            try:
                result = await provider.call("inspect", values)
                data = {"result": result}
            except MediaError as exc:
                # Connectivity, account state and uncertain outcomes must not poison candidates.
                if (
                    exc.code not in {"share_cancelled", "share_expired", "share_password_invalid"}
                    or exc.uncertain
                ):
                    raise
                self.save_check(identity, values["share_id"], time.time(), {"error": exc.code})
                raise
            observed = time.time()
            self.save_check(identity, values["share_id"], observed, data)
            return {**result, "inspection": {"observed_at": observed, "cached": False}}

    def observation(self, share, password, *, account="aliyun"):
        values = {
            "account": account,
            "share_id": share,
            "password": password,
            "parent_file_id": "root",
            "marker": "",
        }
        identity = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        row = self.db.execute(
            "SELECT observed,data FROM media_share_checks WHERE identity=?", (identity,)
        ).fetchone()
        if not row:
            return None
        data = json.loads(row[1])
        return {
            "state": data.get("error", "readable"),
            "observed_at": row[0],
            "expired": time.time() - row[0] >= 120,
            "scope": "root_first_page",
            "content_identity_verified": False,
        }

    def save_check(self, identity, share, observed, data):
        raw = json.dumps(data, ensure_ascii=False)
        # Public share metadata only; never store credentials, signed download URLs or unbounded pages.
        if len(raw.encode()) > 65536:
            # A fresh oversized response invalidates older evidence for this exact scope.
            with self.db:
                self.db.execute("DELETE FROM media_share_checks WHERE identity=?", (identity,))
            return
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO media_share_checks VALUES(?,?,?,?)", (identity, share, observed, raw)
            )
            self.db.execute(
                "DELETE FROM media_share_checks WHERE identity NOT IN "
                "(SELECT identity FROM media_share_checks ORDER BY observed DESC,identity LIMIT 128)"
            )
