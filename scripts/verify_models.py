"""Explicit paid synthetic model acceptance; never changes a running assistant.

Requires an already prepared local gateway and prior user budget/data authorization.
Three bounded auxiliary calls; optional isolated Codex file/tool probe. Real provider
feature equivalence still requires separate search/MCP/Goal/compression/voice acceptance.
"""

import argparse
import asyncio
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import uuid

from assistant_runtime.config import Config
from assistant_runtime.kernel.models import ModelRequest
from assistant_runtime.kernel.runtime import Runtime
from assistant_runtime.store import Store
from assistant_runtime.conversation.store import ConversationStore
from assistant_runtime.conversation.media import MediaAttachments
from assistant_runtime.plugins.models.plugin import ModelsPlugin
from assistant_runtime.plugins.models.settings import ModelSettings, reasoning_effort as resolve_effort
from assistant_runtime.plugins.models.gateway import gateway_credential
from assistant_runtime.plugins.codex.rpc import CodexClient
from assistant_runtime.plugins.codex.api_catalog import write_catalog


async def codex_probe(config, model, settings, *, effort=None):
    """A new temporary workspace, no household MCP/skills/history, maximum 90 seconds."""
    effort = resolve_effort(settings.entry(model), effort)
    with tempfile.TemporaryDirectory(prefix="assistant-api-probe-") as area:
        provider = {
            "name": "Synthetic API acceptance",
            "base_url": settings.gateway_url,
            "wire_api": "responses",
            "env_key": "ASSISTANT_CODEX_GATEWAY_KEY",
            "requires_openai_auth": False,
            "request_max_retries": 0,
            "stream_max_retries": 0,
            "stream_idle_timeout_ms": 30000,
        }
        client = CodexClient(
            config.codex_binary,
            area,
            overrides={
                "model_provider": "assistant_api",
                "model_catalog_json": write_catalog(Path(area), settings),
                "model_providers.assistant_api": provider,
                "features.remote_control": False,
                "features.multi_agent": False,
                "features.apps": False,
                "project_doc_max_bytes": 0,
                "model_reasoning_effort": effort,
            },
            environment={"ASSISTANT_CODEX_GATEWAY_KEY": gateway_credential(config.root, settings)},
        )
        thread, turn = None, None
        events, final, tools = [], [], 0
        usage = None
        started = time.monotonic()
        try:
            async with asyncio.timeout(90):
                await client.start()
                inherited = (await client.request("config/read", {"includeLayers": False, "cwd": area}))[
                    "config"
                ].get("mcp_servers", {})
                result = await client.request(
                    "thread/start",
                    {
                        "cwd": area,
                        "model": model,
                        "modelProvider": "assistant_api",
                        "allowProviderModelFallback": False,
                        "sandbox": config.codex_sandbox,
                        "approvalPolicy": config.approval_policy,
                        "baseInstructions": "You are performing a synthetic protocol test. Use only tools necessary for this temporary directory. Do not access network, credentials, other paths or personal data. Never spawn agents. Maximum four tool calls.",
                        "developerInstructions": "Write and edit only probe.txt in the working directory, then read it back. Do not inspect any other files. Stop on failure.",
                        "config": {
                            "mcp_servers": {k: {"enabled": False} for k in inherited},
                            "skills": {"config": []},
                            "web_search": "disabled",
                        },
                    },
                )
                thread = result["thread"]["id"]
                if result.get("modelProvider") != "assistant_api" or result.get("model") != model:
                    raise RuntimeError("Unexpected provider/model resolution")
                response = await client.request(
                    "turn/start",
                    {
                        "threadId": thread,
                        "effort": effort,
                        "input": [
                            {
                                "type": "text",
                                "text": "Create probe.txt containing alpha, use the file edit tool to replace alpha with beta, then read it back with a shell command. Report the exact final file text. Do nothing else.",
                            }
                        ],
                    },
                )
                turn = response["turn"]["id"]
                while True:
                    event = await client.events.get()
                    method, params = event.get("method", ""), event.get("params", {})
                    if "id" in event:
                        await client.send(
                            {
                                "id": event["id"],
                                "error": {
                                    "code": -32601,
                                    "message": "Synthetic probe rejects additional permissions/input",
                                },
                            }
                        )
                    if method == "thread/tokenUsage/updated":
                        usage = params.get("tokenUsage")
                    if method == "error":
                        raise RuntimeError(
                            "Provider turn reported an error; inspect private acceptance trace"
                        )
                    if method in {"item/completed", "turn/completed"}:
                        events.append(event)
                    if method == "item/completed":
                        item = params.get("item", {})
                        if item.get("type") == "agentMessage":
                            final.append(item.get("text", ""))
                        elif item.get("type") in {"commandExecution", "fileChange", "mcpToolCall"}:
                            tools += 1
                            if tools > 4:
                                raise RuntimeError("Synthetic tool-call budget exhausted")
                    if method == "turn/completed" and params.get("turn", {}).get("id") == turn:
                        if params["turn"].get("status") != "completed":
                            raise RuntimeError("Codex test turn did not complete")
                        break
                actual = (Path(area) / "probe.txt").read_text().strip()
                patched = any(e.get("params", {}).get("item", {}).get("type") == "fileChange" for e in events)
                if actual != "beta" or not tools or not patched:
                    raise RuntimeError("Tool/file acceptance failed")
                return {
                    "passed": True,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "tool_items": tools,
                    "usage": usage,
                    "file_text": actual,
                    "final": final,
                    "events": events,
                }
        except Exception as exc:
            return {
                "passed": False,
                "error_type": type(exc).__name__,
                "tool_items": tools,
                "final": final,
                "events": events,
            }
        finally:
            if thread and turn:
                try:
                    await client.request("turn/interrupt", {"threadId": thread, "turnId": turn}, timeout=5)
                except Exception:
                    pass
            if thread:
                try:
                    await client.request("thread/archive", {"threadId": thread}, timeout=5)
                except Exception:
                    pass
            await client.close()


async def verify(config, model, execution, cases=None, *, effort=None):
    explicit_cases = bool(cases)
    cases = cases or {"text", "structured", "image"}
    settings = ModelSettings.parse(config.model_api)
    entry = settings.entry(model)
    effort = resolve_effort(entry, effort)
    for capability, flag in (("image", "images"), ("structured", "structured")):
        if explicit_cases and capability in cases and not entry.get(flag):
            raise ValueError("Requested acceptance capability is not enabled in test config: " + capability)
    if "execution-only" in cases and not execution:
        raise ValueError("execution-only requires --execution")
    output = config.state_dir / "acceptance/models" / str(uuid.uuid4())
    output.mkdir(parents=True, mode=0o700)
    store = Store(output / "receipts.sqlite3")
    ConversationStore(store)
    runtime = Runtime(store)
    host = SimpleNamespace(
        config=config,
        store=store,
        runtime=runtime,
        conversation=SimpleNamespace(media=MediaAttachments(output, store.db)),
    )
    plugin = ModelsPlugin(host)
    runtime.add(plugin)
    results = {
        "model": model,
        "configuration_revision": settings.fingerprint(),
        "synthetic_only": True,
        "cases": [],
    }

    async def call(case, text, **extra):
        result = await runtime.invoke(
            ModelRequest(
                "infer",
                {
                    "request_id": str(uuid.uuid4()),
                    "model": model,
                    "text": text,
                    "reasoning_effort": effort,
                    **extra,
                },
            ),
            timeout=settings.timeout_seconds + 2,
        )
        results["cases"].append({"case": case, **result})
        print(case, result["state"], result.get("elapsed_ms"), result.get("usage"), flush=True)
        return result

    try:
        await runtime.start()
        if "text" in cases:
            answer = await call(
                "text",
                "Synthetic coding acceptance: evaluate the Python expression 17 + 25. Reply with only the integer.",
            )
            if answer["text"].strip() != "42":
                raise RuntimeError("Incorrect synthetic arithmetic result")
        if "structured" in cases and entry.get("structured"):
            schema = {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            }
            answer = await call("structured", "Return JSON with integer value equal to 42.", schema=schema)
            if answer["data"] != {"value": 42}:
                raise RuntimeError("Incorrect structured result")
        if "image" in cases and entry.get("images"):
            from PIL import Image

            buffer = io.BytesIO()
            Image.new("RGB", (128, 128), (255, 0, 0)).save(buffer, format="PNG")
            ref = await host.conversation.media.add_image(buffer.getvalue(), "synthetic-red.png")
            answer = await call(
                "image",
                "Name the solid color in the image. Answer with a single English color word.",
                images=[ref["id"]],
            )
            if answer["text"].strip().lower().strip(".") != "red":
                raise RuntimeError("Incorrect synthetic image result")
        if execution:
            results["codex"] = await codex_probe(config, model, settings, effort=effort)
            if not results["codex"]["passed"]:
                raise RuntimeError("Codex file/tool acceptance failed")
            print("codex file/tools passed", flush=True)
    except Exception as exc:
        results["error_type"] = type(exc).__name__
        # Provider bodies/exception strings can contain credentials: keep console/report generic.
        print("Acceptance stopped:", type(exc).__name__, flush=True)
    finally:
        await runtime.close()
        store.db.close()
        (output / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
        (output / "result.json").chmod(0o600)
        print(output / "result.json")
    return 1 if "error_type" in results else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--reasoning-effort", help="An exact supported level; defaults to the model's configured default"
    )
    parser.add_argument(
        "--confirm-paid", action="store_true", help="User has authorized synthetic data and test budget"
    )
    parser.add_argument(
        "--execution", action="store_true", help="Also test isolated Codex file/tool execution"
    )
    parser.add_argument(
        "--case",
        choices=["text", "structured", "image", "execution-only"],
        action="append",
        help="Run only selected auxiliary cases; repeat to combine",
    )
    args = parser.parse_args()
    if not args.confirm_paid:
        parser.error("Paid testing requires explicit data/budget authorization and --confirm-paid")
    raise SystemExit(
        asyncio.run(
            verify(
                Config.load(args.config),
                args.model,
                args.execution,
                set(args.case or []),
                effort=args.reasoning_effort,
            )
        )
    )


if __name__ == "__main__":
    main()
