"""Search-owned receipt storage. No queries, URLs or response bodies are retained."""

import json


class SearchRepository:
    def __init__(self, db):
        self.db = db
        db.execute(
            "CREATE TABLE IF NOT EXISTS search_calls(seq INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)"
        )

    def record(self, raw):
        with self.db:
            self.db.execute("INSERT INTO search_calls(data) VALUES(?)", (raw,))
            self.db.execute(
                "DELETE FROM search_calls WHERE seq NOT IN (SELECT seq FROM search_calls ORDER BY seq DESC LIMIT 1000)"
            )

    def history(self, before, limit):
        rows = self.db.execute(
            "SELECT seq,data FROM search_calls WHERE (? IS NULL OR seq<?) ORDER BY seq DESC LIMIT ?",
            (before, before, limit + 1),
        ).fetchall()
        return {
            "items": [{"id": seq, **json.loads(data)} for seq, data in rows[:limit]],
            "before": rows[limit - 1][0] if len(rows) > limit else None,
        }
