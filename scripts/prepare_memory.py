"""Prepare local embedding files and Mem0 indexes; no facts or cloud model inference."""

import argparse
import asyncio
from assistant_runtime.config import Config
from assistant_runtime.memory.mem0 import Mem0Memory


async def prepare(config):
    from fastembed import TextEmbedding

    cache = config.models / "memory-embedding"
    cache.mkdir(parents=True, exist_ok=True)
    model = await asyncio.to_thread(
        TextEmbedding,
        model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        cache_dir=str(cache),
        threads=2,
    )
    await asyncio.to_thread(lambda: list(model.embed(["初始化本地记忆模型"])))
    memory = Mem0Memory(config.state_dir / "mem0", cache, project=config.workspace)
    try:
        await memory.start()
        await memory.search("记忆服务初始化", "user")
        print("Local Mem0 and embedding model ready; no model inference performed")
    finally:
        await memory.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    asyncio.run(prepare(Config.load(parser.parse_args().config)))
