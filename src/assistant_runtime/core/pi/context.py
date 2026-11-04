"""Private native context format and SDK projection; never a second agent loop."""

import asyncio
import json
import os
from pathlib import Path

from assistant_runtime.api.system import system
from assistant_runtime.platforms.executables import pi_node
from .accounts import installation


def result_references(bundle):
    """Only adapter-generated result projections carry retained-content references."""
    references = {}
    for message in bundle["messages"]:
        if message.get("role") != "toolResult":
            continue
        content = message.get("content")
        for part in content if isinstance(content, list) else []:
            text = part.get("text", "")
            marker = "[Framework result delivery] "
            if not text.startswith(marker):
                continue
            try:
                projection, _ = json.JSONDecoder().raw_decode(text[len(marker) :])
                reference = projection.get("reference")
                if reference:
                    references[reference["id"]] = {key: reference[key] for key in ("id", "version")}
            except (KeyError, TypeError, ValueError):
                raise ValueError("snapshot_dependency_invalid") from None
    return list(references.values())


async def native_context(binary, **arguments):
    package = await installation(binary)
    process = await system.spawn(
        pi_node(binary),
        str(Path(__file__).with_suffix(".mjs")),
        str(package),
        stdin=-1,
        stdout=-1,
        stderr=-1,
        env={key: value for key, value in os.environ.items() if key in {"PATH", "SystemRoot", "TEMP", "TMP"}},
        limit=64 * 1024 * 1024,
    )
    try:
        out, error = await asyncio.wait_for(
            process.communicate(json.dumps(arguments, ensure_ascii=False).encode()), 30
        )
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode:
        # Native errors can include paths/content; keep the public outcome bounded.
        raise ValueError("context_native_projection_unavailable")
    return json.loads(out)
