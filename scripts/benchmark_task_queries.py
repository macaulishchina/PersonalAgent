"""Measure existing public task list/execution_info cold projections with private SQLite."""

import argparse
import asyncio
import json
import platform
from pathlib import Path
import sqlite3
import statistics
import tempfile
import time
from types import SimpleNamespace
import uuid

from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.core.tasks.plugin import TasksPlugin
from assistant_runtime.core.tasks.repository import TaskRepository


async def benchmark(samples):
    with tempfile.TemporaryDirectory(prefix="pa-task-query-") as area:
        db = sqlite3.connect(Path(area) / "tasks.sqlite3")
        try:
            repo = TaskRepository(db)
            plugin = object.__new__(TasksPlugin)
            plugin.repo, plugin.host = repo, SimpleNamespace(ai_usage=None)
            ids = []
            for index in range(100):
                ident = uuid.uuid4().hex
                spec = {
                    "title": str(index),
                    "prompt": "synthetic",
                    "trigger": {"kind": "immediate"},
                    "execution": {"kind": "agent", "model": "synthetic", "reasoning_effort": "auto"},
                }
                db.execute(
                    "INSERT INTO job_definitions(id,request_id,fingerprint,state,spec,created,updated) VALUES(?,?,?,?,?,?,?)",
                    (
                        ident,
                        str(uuid.uuid4()),
                        "synthetic",
                        "enabled",
                        json.dumps(spec),
                        time.time(),
                        time.time(),
                    ),
                )
                run = repo.enqueue(repo.definition(ident), "synthetic")
                ids.append(run["id"])
            db.commit()
            rows = []
            for count in (1, 20, 100):
                for operation in ("list", "execution_info"):
                    times, query_counts, sizes, calls = [], [], [], []
                    for _ in range(samples):
                        queries = []
                        db.set_trace_callback(queries.append)
                        started = time.perf_counter()
                        results = []
                        if operation == "execution_info":
                            results.append(
                                await plugin.request(TaskRequest(operation, {"run_ids": ids[:count]}))
                            )
                        else:
                            before = None
                            remaining = count
                            while remaining:
                                limit = min(remaining, 50)
                                result = await plugin.request(
                                    TaskRequest(
                                        "list", {"limit": limit, **({"before": before} if before else {})}
                                    )
                                )
                                results.append(result)
                                remaining -= len(result["tasks"])
                                before = result["before"]
                        times.append((time.perf_counter() - started) * 1000)
                        db.set_trace_callback(None)
                        query_counts.append(
                            len([q for q in queries if q.lstrip().upper().startswith("SELECT")])
                        )
                        sizes.append(len(json.dumps(results, ensure_ascii=False).encode()))
                        calls.append(len(results))
                    ordered = sorted(times)
                    rows.append(
                        {
                            "operation": operation,
                            "tasks": count,
                            "public_calls": max(calls),
                            "sql_queries": max(query_counts),
                            "response_bytes": max(sizes),
                            "p50_ms": round(statistics.median(times), 3),
                            "p95_ms": round(ordered[int((len(ordered) - 1) * 0.95)], 3),
                            "p99_ms": round(ordered[int((len(ordered) - 1) * 0.99)], 3),
                        }
                    )
            return {
                "scope": "public_in_process_task_projections_without_plugin_cache_or_inference",
                "platform": platform.platform(),
                "processor": platform.processor(),
                "python": platform.python_version(),
                "samples": samples,
                "concurrency": 1,
                "sqlite": sqlite3.sqlite_version,
                "results": rows,
            }
        finally:
            db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 10 <= args.samples <= 10000:
        parser.error("samples must be 10..10000")
    result = asyncio.run(benchmark(args.samples))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
