"""Process boundary for the upstream MCP SDK; same Python environment as the app."""

import asyncio
import json
import os
from pathlib import Path
import sys


async def main():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = Path(sys.argv[1]).resolve()
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    notes = root / "notes"
    notes.mkdir(exist_ok=True)
    settings = {
        "projects": {"main": {"path": str(notes), "mode": "local"}},
        "default_project": "main",
        "auto_update": False,
        "logfire_enabled": False,
        "semantic_search_enabled": True,
        "semantic_embedding_provider": "fastembed",
        "semantic_embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "semantic_embedding_dimensions": 384,
        "semantic_embedding_document_prefix": "",
        "semantic_embedding_query_prefix": "",
        "semantic_embedding_threads": 2,
        "semantic_embedding_cache_dir": str(root / "model-cache"),
        "reranker_enabled": False,
    }
    (config_dir / "config.json").write_text(json.dumps(settings))
    # Do not inherit cloud routing, API keys or model overrides from the invoking shell.
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("BASIC_MEMORY_", "OPENAI_", "ANTHROPIC_"))
    }
    env.update(
        BASIC_MEMORY_CONFIG_DIR=str(config_dir),
        BASIC_MEMORY_HOME=str(notes),
        BASIC_MEMORY_FORCE_LOCAL="true",
        BASIC_MEMORY_AUTO_UPDATE="false",
        BASIC_MEMORY_LOG_LEVEL="ERROR",
        TOKENIZERS_PARALLELISM="false",
    )
    params = StdioServerParameters(
        command=str(Path(sys.executable).parent / "basic-memory"), args=["mcp", "--project", "main"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print(json.dumps({"ready": True}), flush=True)
            while line := await asyncio.to_thread(sys.stdin.readline):
                request = json.loads(line)
                try:
                    result = await session.call_tool(request["tool"], request["arguments"])
                    if getattr(result, "is_error", False):
                        raise RuntimeError(str(result.content)[:400])
                    response = {"result": result.model_dump(mode="json", by_alias=True)}
                except Exception as exc:
                    response = {"error": f"{type(exc).__name__}: {exc}"[:500]}
                print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
