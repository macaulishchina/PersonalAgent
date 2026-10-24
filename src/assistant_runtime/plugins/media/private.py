"""Atomic private JSON, shared with the isolated synchronous SDK worker."""

import asyncio
import json
from assistant_runtime.api.system import system
from pathlib import Path


async def disk(function, *args):
    """Drain owned filesystem IO before cancellation lets its owner release resources."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def remove(path):
    path = Path(path)
    path.unlink(missing_ok=True)
    system.sync_directory(path.parent)


def save(path, data):
    save_bytes(path, json.dumps(data, ensure_ascii=False).encode())


def save_bytes(path, data):
    system.atomic_write(path, data, prefix=".media-")
