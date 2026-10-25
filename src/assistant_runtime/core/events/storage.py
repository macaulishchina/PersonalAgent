"""Two bounded, connection-owning SQLite workers; commit precedes acknowledgements."""

from assistant_runtime.api.system import system

import asyncio
import concurrent.futures
import hashlib
import json
import queue
import sqlite3
import threading
import time

from assistant_runtime.core.events.records import canonical, identity, identifier, timestamp, validate

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
 id TEXT PRIMARY KEY, source TEXT, event_key TEXT, type TEXT, subject TEXT,
 event_time REAL, received_at REAL, revision INTEGER NOT NULL,
 fingerprint TEXT, payload TEXT, deleted_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS record_identity ON records(source,event_key);
CREATE INDEX IF NOT EXISTS record_time ON records(event_time DESC,id DESC);
CREATE INDEX IF NOT EXISTS record_source ON records(source,event_time DESC,id DESC);
CREATE INDEX IF NOT EXISTS record_type ON records(type,event_time DESC,id DESC);
CREATE INDEX IF NOT EXISTS record_subject ON records(subject,event_time DESC,id DESC);
CREATE TABLE IF NOT EXISTS tombstones (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, deleted_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS revisions (
 id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
 changed_at REAL NOT NULL, PRIMARY KEY(id,revision));
CREATE TABLE IF NOT EXISTS changes (
 cursor INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL,
 action TEXT NOT NULL, revision INTEGER NOT NULL, changed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS coverage (
 source TEXT PRIMARY KEY, started_at REAL NOT NULL, observed_until REAL,
 gap_start REAL, reason TEXT);

"""


class Worker:
    def __init__(self, path, settings, readonly=False):
        self.path, self.settings, self.readonly = path, settings, readonly
        self.queue = queue.Queue(settings.queue_size)
        self.accepting = True
        self.ready = concurrent.futures.Future()
        self.thread = threading.Thread(target=self.run, name="events-reader" if readonly else "events-writer")
        self.thread.start()

    def run(self):
        db = None
        try:
            db = sqlite3.connect(
                f"file:{self.path}?mode=ro" if self.readonly else self.path, uri=self.readonly, timeout=1
            )
            db.row_factory = sqlite3.Row
            if self.readonly:
                db.execute("PRAGMA query_only=ON")
            else:
                system.private(self.path)
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    raise ValueError("Unsupported events database version")
                # Reserve half the budget for WAL plus a small SHM/management margin.
                max_pages = max(64, (self.settings.quota_bytes - 65536) // 8192)
                db.execute(f"PRAGMA max_page_count={max_pages}")
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("PRAGMA secure_delete=ON")
                db.executescript(SCHEMA)
                with db:
                    if version == 1:
                        db.execute(
                            "INSERT OR IGNORE INTO tombstones SELECT id,revision,deleted_at FROM records WHERE deleted_at IS NOT NULL"
                        )
                        db.execute("DELETE FROM records WHERE deleted_at IS NOT NULL")
                    db.execute("PRAGMA user_version=2")
            self.ready.set_result(True)
            while True:
                item = self.queue.get()
                if item is None:
                    break
                action, args, future = item
                try:
                    if action == "configure":
                        settings = args["settings"]
                        if not self.readonly:
                            db.execute(
                                f"PRAGMA max_page_count={max(64, (settings.quota_bytes - 65536) // 8192)}"
                            )
                        self.settings = settings
                        self.queue.maxsize = settings.queue_size
                        future.set_result({"configured": True})
                        continue
                    deadline = time.monotonic() + self.settings.query_seconds
                    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                    if self.readonly:
                        db.execute("BEGIN")
                        try:
                            result = read(db, action, args)
                        finally:
                            db.rollback()
                    else:
                        # Do not accumulate multiple large WAL generations behind readers.
                        wal = self.path.with_name(self.path.name + "-wal")
                        if (
                            action in {"append", "snapshot", "correct"}
                            and wal.exists()
                            and wal.stat().st_size > self.settings.quota_bytes // 4
                        ):
                            db.set_progress_handler(None, 0)
                            if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                                raise RuntimeError("Event storage waiting for bounded reader checkpoint")
                            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                        with db:
                            result = write(db, action, args, self.settings)
                        db.set_progress_handler(None, 0)
                        # Bound WAL growth without blocking behind a slow reader.
                        checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                        if checkpoint[0]:
                            result["checkpoint_pending"] = True
                    future.set_result(result)
                except Exception as exc:
                    db.rollback()
                    future.set_exception(exc)
                finally:
                    db.set_progress_handler(None, 0)
        except Exception as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
        finally:
            if db:
                db.close()

    def submit(self, action, args):
        if not self.accepting or not self.thread.is_alive():
            raise RuntimeError("Event storage is closed")
        future = concurrent.futures.Future()
        try:
            self.queue.put_nowait((action, args, future))
        except queue.Full:
            raise RuntimeError("Event storage queue is full") from None
        return future

    async def close(self):
        self.accepting = False
        await asyncio.to_thread(self.queue.put, None)
        await asyncio.to_thread(self.thread.join)


class Storage:
    def __init__(self, directory, settings):
        self.directory, self.settings = directory, settings
        self.writer = self.reader = None

    async def start(self):
        await asyncio.to_thread(self.directory.mkdir, parents=True, exist_ok=True, mode=0o700)
        await asyncio.to_thread(system.private, self.directory, directory=True)
        path = self.directory / "events.sqlite3"
        self.writer = Worker(path, self.settings)
        await asyncio.wrap_future(self.writer.ready)
        self.reader = Worker(path, self.settings, readonly=True)
        await asyncio.wrap_future(self.reader.ready)

    async def call(self, action, **args):
        worker = self.reader if action in {"query", "get", "changes", "stats", "status"} else self.writer
        if worker is None:
            raise RuntimeError("Event storage unavailable")
        # Cancellation means outcome unknown. The writer still commits and a retry
        # with the same source/key resolves it without inserting a second record.
        future = asyncio.wrap_future(worker.submit(action, args))
        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        try:
            result = await asyncio.shield(future)
        except Exception as exc:
            if action in {"append", "snapshot", "correct"} and isinstance(exc, (RuntimeError, sqlite3.Error)):
                self.write_error = str(exc)[:160]
            raise
        if action in {"append", "correct", "delete"} or (action == "snapshot" and result.get("items")):
            self.write_error = None
        if action == "status":
            result["write_error"] = getattr(self, "write_error", None)
            result["quota_bytes"] = self.settings.quota_bytes
        return result

    async def close(self):
        if self.reader:
            await self.reader.close()
        if self.writer:
            await self.writer.close()


def change(db, ident, action, revision):
    return db.execute(
        "INSERT INTO changes(id,action,revision,changed_at) VALUES(?,?,?,?)",
        (ident, action, revision, time.time()),
    ).lastrowid


def capacity(db, settings, extra):
    # Count occupied database pages, reusable free pages, and WAL on disk. Leave
    # room for bounded management transactions; no time-based data pruning.
    pages = db.execute("PRAGMA page_count").fetchone()[0]
    free = db.execute("PRAGMA freelist_count").fetchone()[0]
    size = db.execute("PRAGMA page_size").fetchone()[0]
    if (pages - free) * size + extra * 4 + 65536 > settings.quota_bytes // 2:
        raise RuntimeError("Event storage quota exceeded; delete records or raise quota")


def write(db, action, args, settings):
    if action == "snapshot":
        source, now, records = identifier(args["source"]), timestamp(args["at"]), args["records"]
        if not isinstance(records, list) or len(records) > 4096:
            raise ValueError("Snapshot batch exceeds limit")
        old = db.execute("SELECT * FROM coverage WHERE source=?", (source,)).fetchone()
        receipts = []
        if old and old["gap_start"] is not None:
            receipts.append(
                write(
                    db,
                    "append",
                    {
                        "source": source,
                        "record": {
                            "key": "gap:" + str(old["gap_start"]) + ":" + str(now),
                            "type": "source.gap",
                            "version": 1,
                            "subject": source,
                            "observed_start": min(old["gap_start"], now),
                            "observed_end": now,
                            "evidence": "gap",
                            "data": {
                                "reason": old["reason"],
                                "meaning": "Observation unavailable; no transitions inferred",
                            },
                        },
                    },
                    settings,
                )
            )
        for record in records:
            receipts.append(write(db, "append", {"source": source, "record": record}, settings))
        write(db, "coverage", {"source": source, "at": now}, settings)
        return {
            "items": receipts,
            "cursor": next((r["cursor"] for r in reversed(receipts) if "cursor" in r), None),
        }
    if action == "coverage":
        source = identifier(args["source"])
        now = timestamp(args["at"])
        db.execute("INSERT OR IGNORE INTO coverage(source,started_at) VALUES(?,?)", (source, now))
        if args.get("reason"):
            db.execute(
                "UPDATE coverage SET gap_start=COALESCE(gap_start,?),reason=? WHERE source=?",
                (now, args["reason"], source),
            )
        else:
            db.execute(
                "UPDATE coverage SET observed_until=?,gap_start=NULL,reason=NULL WHERE source=?",
                (now, source),
            )
        return {"ok": True}
    if action == "append":
        source, record = identifier(args["source"]), validate(args["record"])
        ident = identity(source, record["key"])
        tombstone = db.execute("SELECT revision FROM tombstones WHERE id=?", (ident,)).fetchone()
        if tombstone:
            return {"id": ident, "revision": tombstone[0], "duplicate": True, "deleted": True}
        wire = canonical(record)
        fingerprint = hashlib.sha256(wire.encode()).hexdigest()
        old = db.execute("SELECT * FROM records WHERE id=?", (ident,)).fetchone()
        if old:
            if old["fingerprint"] != fingerprint:
                raise ValueError("Event identity conflicts with different original content")
            return {
                "id": ident,
                "revision": old["revision"],
                "duplicate": True,
                "deleted": False,
            }
        capacity(db, settings, len(wire.encode()))
        event_time = (
            record["occurred_at"] if record.get("occurred_at") is not None else record["observed_end"]
        )
        db.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
            (
                ident,
                source,
                record["key"],
                record["type"],
                record["subject"],
                event_time,
                time.time(),
                1,
                fingerprint,
                wire,
            ),
        )
        cursor = change(db, ident, "append", 1)
        return {"id": ident, "revision": 1, "cursor": cursor, "duplicate": False}
    if action in {"correct", "delete"}:
        old = db.execute("SELECT * FROM records WHERE id=?", (args["id"],)).fetchone()
        if old is None:
            tombstone = db.execute("SELECT revision FROM tombstones WHERE id=?", (args["id"],)).fetchone()
            if tombstone and action == "delete":
                return {"id": args["id"], "revision": tombstone[0], "deleted": True}
            raise ValueError("Deleted or unknown event")
        if (
            action == "correct"
            and args["revision"] == old["revision"] - 1
            and canonical(validate(args["record"])) == old["payload"]
        ):
            return {"id": old["id"], "revision": old["revision"], "duplicate": True}
        if type(args["revision"]) is not int or args["revision"] != old["revision"]:
            raise ValueError("Event revision conflict")
        revision = old["revision"] + 1
        if action == "correct":
            record = validate(args["record"])
            if record["key"] != old["event_key"]:
                raise ValueError("Correction cannot change event identity")
            wire = canonical(record)
            capacity(db, settings, len(wire.encode()))
            db.execute(
                "INSERT INTO revisions VALUES(?,?,?,?)",
                (old["id"], old["revision"], old["payload"], time.time()),
            )
            db.execute(
                "UPDATE records SET revision=?,payload=?,type=?,subject=?,event_time=? WHERE id=?",
                (
                    revision,
                    wire,
                    record["type"],
                    record["subject"],
                    record["occurred_at"]
                    if record.get("occurred_at") is not None
                    else record["observed_end"],
                    old["id"],
                ),
            )
        else:
            db.execute("DELETE FROM revisions WHERE id=?", (old["id"],))
            db.execute("DELETE FROM records WHERE id=?", (old["id"],))
            db.execute("INSERT INTO tombstones VALUES(?,?,?)", (old["id"], revision, time.time()))
        cursor = change(db, old["id"], action, revision)
        return {"id": old["id"], "revision": revision, "cursor": cursor, "deleted": action == "delete"}
    raise ValueError("Unknown event write operation")


def item(row):
    value = dict(row)
    value.pop("fingerprint", None)
    value["record"] = json.loads(value.pop("payload")) if value.get("payload") else None
    return value


def filters(args):
    terms, values = ["deleted_at IS NULL"], []
    for key in ("source", "type", "subject"):
        if args.get(key) is not None:
            terms.append(key + "=?")
            values.append(identifier(args[key]))
    for key in ("evidence", "entrance"):
        if args.get(key) is not None:
            path = "$.evidence" if key == "evidence" else "$.data.entrance"
            terms.append("json_extract(payload,?)=?")
            values.extend([path, identifier(args[key])])
    for key, operator in (("since", ">="), ("until", "<=")):
        if args.get(key) is not None:
            terms.append(
                (
                    "event_time"
                    if key == "since"
                    else "COALESCE(json_extract(payload,'$.observed_start'),event_time)"
                )
                + operator
                + "?"
            )
            values.append(timestamp(args[key]))
    if args.get("since") is not None and args.get("until") is not None and args["since"] > args["until"]:
        raise ValueError("Reversed query interval")
    return terms, values


def read(db, action, args):
    limit = args.get("limit", 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Event page limit must be 1..100")
    if action == "get":
        row = db.execute("SELECT * FROM records WHERE id=?", (args["id"],)).fetchone()
        if row is None:
            tombstone = db.execute("SELECT * FROM tombstones WHERE id=?", (args["id"],)).fetchone()
            if tombstone:
                return {
                    **dict(tombstone),
                    "source": None,
                    "record": None,
                    "revisions": [],
                    "next_revision": None,
                }
            raise ValueError("Unknown event")
        result = item(row)
        # Revision history is independently paginated, never an unbounded detail.
        revisions = db.execute(
            "SELECT revision,payload,changed_at FROM revisions WHERE id=? AND revision>? ORDER BY revision LIMIT ?",
            (args["id"], args.get("after_revision", 0), limit + 1),
        ).fetchall()
        result["revisions"] = [
            {"revision": r[0], "record": json.loads(r[1]), "changed_at": r[2]} for r in revisions[:limit]
        ]
        result["next_revision"] = revisions[limit - 1][0] if len(revisions) > limit else None
        return result
    if action == "changes":
        after = args.get("after", 0)
        if type(after) is not int or after < 0:
            raise ValueError("Invalid change cursor")
        rows = db.execute(
            "SELECT * FROM changes WHERE cursor>? ORDER BY cursor LIMIT ?", (after, limit + 1)
        ).fetchall()
        return {
            "head": db.execute("SELECT COALESCE(MAX(cursor),0) FROM changes").fetchone()[0],
            "items": [dict(r) for r in rows[:limit]],
            "next": rows[min(limit, len(rows)) - 1]["cursor"] if rows else after,
            "has_more": len(rows) > limit,
        }
    terms, values = filters(args)
    coverage = [
        dict(r)
        for r in db.execute(
            "SELECT * FROM coverage"
            + (" WHERE source=?" if args.get("source") else "")
            + " ORDER BY source LIMIT 101",
            (identifier(args["source"]),) if args.get("source") else (),
        )
    ]
    coverage_truncated = len(coverage) > 100
    coverage = coverage[:100]
    gap_terms, gap_values = filters({key: args[key] for key in ("source", "since", "until") if key in args})
    gap_terms.append("type='source.gap'")
    gaps = [
        item(r)
        for r in db.execute(
            "SELECT * FROM records WHERE " + " AND ".join(gap_terms) + " ORDER BY event_time DESC LIMIT 101",
            gap_values,
        )
    ]
    deleted_records_present = db.execute("SELECT 1 FROM tombstones LIMIT 1").fetchone() is not None
    coverage_complete = (
        bool(coverage)
        and not coverage_truncated
        and not deleted_records_present
        and not gaps
        and not any(r["gap_start"] is not None for r in coverage)
    )
    if args.get("since") is not None and any(args["since"] < r["started_at"] for r in coverage):
        coverage_complete = False
    if args.get("until") is not None and any(
        r["observed_until"] is None or args["until"] > r["observed_until"] for r in coverage
    ):
        coverage_complete = False
    where = " AND ".join(terms)
    if action == "status":
        return {
            "coverage": coverage,
            "coverage_truncated": coverage_truncated,
            "deleted_records_present": deleted_records_present,
            "cursor": db.execute("SELECT COALESCE(MAX(cursor),0) FROM changes").fetchone()[0],
            "database_bytes": db.execute("PRAGMA page_count").fetchone()[0]
            * db.execute("PRAGMA page_size").fetchone()[0],
        }
    if action == "stats":
        group = args.get("group_by", "type")
        if group not in {"source", "type", "subject"}:
            raise ValueError("Invalid event grouping")
        rows = db.execute(
            f"SELECT {group},COUNT(*) FROM records WHERE {where} GROUP BY {group} ORDER BY COUNT(*) DESC LIMIT ?",
            [*values, limit + 1],
        ).fetchall()
        return {
            "groups": [{"value": r[0], "count": r[1]} for r in rows[:limit]],
            "complete": len(rows) <= limit,
            "coverage_complete": coverage_complete,
            "gaps": gaps[:100],
            "gaps_truncated": len(gaps) > 100,
            "coverage": coverage,
            "coverage_truncated": coverage_truncated,
            "deleted_records_present": deleted_records_present,
        }
    if action != "query":
        raise ValueError("Unknown event query")
    if args.get("before"):
        cursor = args["before"]
        if not isinstance(cursor, list) or len(cursor) != 2:
            raise ValueError("Invalid history cursor")
        terms.append("(event_time,id)<(?,?)")
        values.extend([timestamp(cursor[0]), identifier(cursor[1])])
    rows = db.execute(
        "SELECT * FROM records WHERE " + " AND ".join(terms) + " ORDER BY event_time DESC,id DESC LIMIT ?",
        [*values, limit + 1],
    ).fetchall()
    selected = rows[:limit]
    return {
        "items": [item(r) for r in selected],
        "next": [selected[-1]["event_time"], selected[-1]["id"]] if len(rows) > limit else None,
        "coverage": coverage,
        "coverage_truncated": coverage_truncated,
        "deleted_records_present": deleted_records_present,
        "complete": coverage_complete,
        "gaps": gaps[:100],
        "gaps_truncated": len(gaps) > 100,
        "coverage_note": "Only observed periods; inspect source.gap records for historical interruptions. Pagination is not a frozen snapshot; use changes cursor for synchronization.",
    }
