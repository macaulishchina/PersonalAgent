"""Warm the local memory process/model without creating user facts or producing audio."""

import argparse
import asyncio
from assistant_runtime.config import Config
from assistant_runtime.memory.basic import BasicMemory


async def prepare(config):
    memory = BasicMemory(config.state_dir / "memory")
    try:
        from fastembed import TextEmbedding

        model = await asyncio.to_thread(
            TextEmbedding,
            model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            cache_dir=str(config.state_dir / "memory/model-cache"),
            threads=2,
        )
        await asyncio.to_thread(lambda: list(model.embed(["初始化本地记忆模型"])))
        await memory.start()
        await memory.search("记忆服务初始化", "user")
        print("Local memory and embedding model ready")
    finally:
        await memory.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    asyncio.run(prepare(Config.load(p.parse_args().config)))
