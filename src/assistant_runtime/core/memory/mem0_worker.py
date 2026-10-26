"""Mem0 in the existing environment; stdio carries memory operations, not an HTTP model API."""

import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from datetime import datetime


# Similarity floor for the fixed multilingual MiniLM embedder, not a probability.
SEARCH_MIN_SCORE = 0.5


class Channel:
    def __init__(self, read, write):
        self.read, self.write = read, write
        self.lock = threading.Lock()
        self.sources = []
        self.provenance = {}
        self.actions = []
        self.error = None

    def emit(self, payload):
        data = json.dumps(payload, ensure_ascii=False)
        if len(data.encode()) > 4 * 1024 * 1024:
            raise ValueError("Memory protocol payload exceeds limit")
        self.write.write(data + "\n")
        self.write.flush()

    def complete(self, messages, schema):
        with self.lock:
            self.emit({"model_request": {"messages": messages, "schema": schema}})
            line = self.read.readline(4 * 1024 * 1024 + 1)
            if not line:
                raise RuntimeError("Memory parent disconnected")
            reply = json.loads(line)
            if reply.get("error"):
                raise RuntimeError(reply["error"])
            return reply["model_result"]


class Engine:
    def __init__(self, root, cache, channel):
        from mem0 import Memory
        from mem0.configs.base import MemoryConfig
        from mem0.utils.factory import LlmFactory, EmbedderFactory
        from assistant_runtime.core.memory import mem0_models
        from assistant_runtime.core.memory.policy import FACT_PROMPT

        self.root, self.channel = root, channel
        mem0_models.CHANNEL = channel
        LlmFactory.register_provider(
            "assistant_models", "assistant_runtime.core.memory.mem0_models.FrameworkLLM"
        )
        EmbedderFactory.provider_to_class["local_fastembed"] = (
            "assistant_runtime.core.memory.mem0_models.LocalFastEmbed"
        )
        config = MemoryConfig(
            vector_store={
                "provider": "qdrant",
                "config": {
                    "path": str(root / "vectors"),
                    "collection_name": "personal_agent",
                    "embedding_model_dims": 384,
                },
            },
            embedder={
                "provider": "fastembed",
                "config": {
                    "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                    "model_kwargs": {"cache_dir": str(cache), "threads": 2, "local_files_only": True},
                },
            },
            history_db_path=str(root / "history.sqlite3"),
            custom_fact_extraction_prompt=FACT_PROMPT,
        )
        # Registered provider names are not included in 1.x's static config whitelist.
        # Assign trusted registered providers through the public config/factory APIs.
        config.llm.provider = "assistant_models"
        config.llm.config = {"model": "memory_binding"}
        config.embedder.provider = "local_fastembed"
        self.memory = Memory(config)
        self.receipt_path = root / "receipts.json"
        self.receipts = (
            json.loads(self.receipt_path.read_text(encoding="utf-8")) if self.receipt_path.exists() else {}
        )

    @staticmethod
    def identity(scope, project=""):
        if scope == "user":
            return "personal_user"
        if scope != "project":
            raise ValueError("Invalid memory scope")
        return "project_" + hashlib.sha256(project.encode()).hexdigest()[:24]

    def points(self):
        return self.memory.get_all(agent_id="personal_agent", limit=10000)["results"]

    @staticmethod
    def record(point):
        metadata = point.get("metadata") or {}
        record = metadata.get("app_record")
        if not record:
            # An interruption can occur after Mem0 committed a fact but before
            # app metadata was attached. Recover that committed source-bounded fact.
            sources = metadata.get("source_batch", [])
            if not metadata.get("capture_job") or not sources:
                return None
            record = {
                "id": point["id"],
                "title": point["memory"][:120],
                "scope": metadata["scope"],
                "revision": 1,
                "updated": datetime.fromisoformat(point["updated_at"]).timestamp(),
                "source": {
                    "kind": "automatic",
                    "job": metadata["capture_job"],
                    "recovered": True,
                    "sources": sources,
                },
            }
            if metadata["scope"] == "project":
                record["project"] = metadata["project"]
        return {**record, "text": point["memory"]}

    def export(self):
        return [r for p in self.points() if (r := self.record(p)) is not None]

    def native(self, ident):
        return next(
            (p for p in self.points() if (self.record(p) or {}).get("id") == ident),
            None,
        )

    def upsert(self, record):
        metadata = {"app_record": record, "scope": record["scope"], "project": record.get("project", "")}
        point = self.native(record["id"])
        if point:
            self.memory.update(point["id"], record["text"], metadata=metadata)
        else:
            self.memory.add(
                record["text"],
                user_id=self.identity(record["scope"], record.get("project", "")),
                agent_id="personal_agent",
                metadata=metadata,
                infer=False,
            )

    def search(self, query, scope, project):
        result = self.memory.search(
            query,
            user_id=self.identity(scope, project),
            agent_id="personal_agent",
            limit=8,
            threshold=SEARCH_MIN_SCORE,
        )
        return [r for p in result["results"] if (r := self.record(p)) is not None]

    def capture(self, sources, scope, project, job_id):
        from assistant_runtime.core.memory.policy import FACT_PROMPT

        key = hashlib.sha256(
            (job_id + json.dumps(sources, sort_keys=True, ensure_ascii=False)).encode()
        ).hexdigest()
        if key in self.receipts:
            return {
                "records": self.export(),
                "deleted": self.receipts[key]["deleted"],
                "events": self.receipts[key]["events"],
                "replayed": True,
            }
        sources = [s for s in sources if s.get("role") == "user"]
        if not sources:
            return {"records": self.export(), "deleted": [], "events": []}
        channel = self.channel
        channel.sources, channel.provenance, channel.actions, channel.error = sources, {}, [], None
        before = {p["id"]: self.record(p) for p in self.points()}
        rule = (
            "Only personal background/preferences. Exclude project-specific decisions."
            if scope == "user"
            else "Only durable decisions explicitly about the current software project. Exclude personal preferences and temporary device state."
        )
        self.memory.config.custom_fact_extraction_prompt = FACT_PROMPT + "\nScope rule: " + rule
        source_metadata = {
            "scope": scope,
            "project": project if scope == "project" else "",
            "capture_job": job_id,
            "source_batch": sources,
        }
        result = self.memory.add(
            [{"role": "user", "content": json.dumps(sources, ensure_ascii=False)}],
            user_id=self.identity(scope, project),
            agent_id="personal_agent",
            metadata=source_metadata,
        )
        if channel.error:
            raise channel.error
        events = result["results"]
        expected = sum(a.get("event") in {"ADD", "UPDATE", "DELETE"} for a in channel.actions)
        if len(events) != expected:
            raise RuntimeError("Mem0 did not confirm all requested writes; result is uncertain")
        deleted = []
        for event in events:
            native_id = event["id"]
            previous = before.get(native_id)
            if event["event"] == "DELETE":
                if previous:
                    deleted.append(previous["id"])
                continue
            point = self.memory.get(native_id)
            if not point or point["memory"] != event["memory"]:
                raise RuntimeError("Mem0 write failed independent readback")
            evidence = channel.provenance.get(event["memory"]) or next(
                iter(channel.provenance.values()), None
            )
            if not evidence:
                raise ValueError("Mem0 result lacks user provenance")
            record = {
                "id": previous["id"] if previous else native_id,
                "title": previous["title"] if previous else event["memory"][:120],
                "text": event["memory"],
                "scope": scope,
                "revision": previous["revision"] + 1 if previous else 1,
                "updated": time.time(),
                "source": {"kind": "automatic", **evidence, "job": job_id},
            }
            if scope == "project":
                record["project"] = project
            self.memory.update(native_id, point["memory"], metadata={"app_record": record})
        receipt = {"deleted": deleted, "events": [{"id": e["id"], "event": e["event"]} for e in events]}
        self.receipts[key] = receipt
        self.receipts = dict(list(self.receipts.items())[-128:])
        temporary = self.receipt_path.with_suffix(".partial")
        temporary.write_text(json.dumps(self.receipts), encoding="utf-8")
        temporary.replace(self.receipt_path)
        return {"records": self.export(), **receipt, "replayed": False}

    def dispatch(self, request):
        action, args = request["action"], request.get("arguments", {})
        if action == "export":
            return self.export()
        if action == "read":
            point = self.native(args["id"])
            return self.record(point) if point else None
        if action == "upsert":
            self.upsert(args["record"])
            return None
        if action == "delete":
            point = self.native(args["id"])
            if point:
                self.memory.delete(point["id"])
            return None
        if action == "search":
            return self.search(args["query"], args["scope"], args.get("project", ""))
        if action == "capture":
            return self.capture(**args)
        raise ValueError("Unknown memory operation")


def main():
    root, cache = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        MEM0_DIR=str(root / "settings"),
        MEM0_TELEMETRY="false",
        TOKENIZERS_PARALLELISM="false",
        HF_HUB_OFFLINE="1",
    )
    logging.basicConfig(level=logging.ERROR)
    channel = Channel(sys.stdin, sys.stdout)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            engine = Engine(root, cache, channel)
    except Exception as exc:
        logging.exception("Mem0 startup failed")
        message = "记忆引擎启动失败，请检查 worker.log。"
        if isinstance(exc, ValueError) and "Could not load model" in str(exc):
            message = "本地记忆嵌入模型缺失或损坏；请运行 python scripts/prepare_memory.py --config <实例配置文件> 准备模型。"
        elif isinstance(exc, ImportError):
            message = "记忆插件依赖未安装完整，请同步包含 memory 的安装环境。"
        channel.emit({"ready": False, "error": message})
        return
    channel.emit({"ready": True, "engine": "mem0", "model_auth": "chatgpt"})
    try:
        while line := channel.read.readline(4 * 1024 * 1024 + 1):
            try:
                request = json.loads(line)
                with contextlib.redirect_stdout(sys.stderr):
                    result = engine.dispatch(request)
                channel.emit({"result": result})
            except Exception as exc:
                channel.emit({"error": f"{type(exc).__name__}: {exc}"[:400]})
    finally:
        engine.memory.close()


if __name__ == "__main__":
    main()
