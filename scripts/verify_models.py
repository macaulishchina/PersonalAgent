"""Explicit Pi/API acceptance with synthetic data, a request cap and token limits.

Never restarts the installed assistant or reads its conversations/media. A successful
local/synthetic test is separate from real-provider acceptance. Credentials stay private.
"""

import argparse
import asyncio
import base64
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
from aiohttp import web, ClientSession, ClientTimeout
from assistant_runtime.app.config import Config
from assistant_runtime.core.models.settings import ModelSettings, reasoning_effort
from assistant_runtime.core.models.gateway import gateway_credential
from assistant_runtime.core.models.client import GatewayClient
from assistant_runtime.core.pi.rpc import PiClient
from assistant_runtime.core.pi.accounts import NativeAccounts


async def verify(config, model, *, case="text", effort=None, max_requests=4, max_tokens=1024):
    settings = ModelSettings.parse(config.model_api)
    entry = settings.entry(model)
    native = settings.providers[entry["provider"]]["protocol"] == "pi"
    selected_effort = reasoning_effort(entry, effort)
    key = gateway_credential(config.root, settings)
    submitted = 0
    http_statuses = []
    started = time.monotonic()
    async with ClientSession(timeout=ClientTimeout(total=60), trust_env=False) as transport:

        async def proxy(request):
            nonlocal submitted
            if submitted >= max_requests:
                return web.json_response(
                    {"error": {"message": "Synthetic acceptance request budget exhausted"}}, status=429
                )
            submitted += 1
            payload = await request.json()
            payload["max_output_tokens"] = max_tokens
            headers = {"Authorization": "Bearer " + key}
            if request.headers.get("x-assistant-session"):
                headers["x-assistant-session"] = request.headers["x-assistant-session"]
            async with transport.post(
                settings.gateway_url + "/responses", json=payload, headers=headers, allow_redirects=False
            ) as upstream:
                http_statuses.append(upstream.status)
                response = web.StreamResponse(
                    status=upstream.status,
                    headers={"Content-Type": upstream.headers.get("Content-Type", "application/json")},
                )
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(8192):
                    await response.write(chunk)
                await response.write_eof()
                return response

        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_post("/v1/responses", proxy)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        bounded = replace(
            settings, gateway_url=f"http://127.0.0.1:{port}/v1", execution_max_output_tokens=max_tokens
        )
        for item in bounded.entries.values():
            item["execution_max_output_tokens"] = max_tokens
        evidence = {
            "model": model,
            "reasoning_effort": selected_effort,
            "case": case,
            "state": "unknown",
            "scope": "synthetic_only",
            "max_requests": max_requests,
            "max_output_tokens": max_tokens,
        }
        try:
            if case == "memory":
                if not entry.get("structured"):
                    raise ValueError("Memory acceptance requires an explicit structured-output candidate")
                from assistant_runtime.core.ai_usage.service import UsageService
                from assistant_runtime.api.ai_usage import metered
                from assistant_runtime.core.ai_usage.settings import UsageSettings
                from assistant_runtime.api.ai_usage import UsageScope

                area = tempfile.TemporaryDirectory(prefix="memory-acceptance-")
                usage = UsageService(Path(area.name) / "ai-usage", UsageSettings(max_requests=max_requests))
                await usage.start()
                client = GatewayClient(bounded, key)
                client.accounts = NativeAccounts(config.root, bounded)
                try:
                    await client.start()
                    payload = {
                        "model": model,
                        "input": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "Synthetic memory extraction: return JSON {fact: likes tea, source_id: 1, evidence: likes tea}. Source #1 user text: likes tea.",
                                    }
                                ],
                            }
                        ],
                        "reasoning": {"effort": selected_effort},
                        "max_output_tokens": max_tokens,
                        "text": {
                            "format": {
                                "type": "json_schema",
                                "name": "memory_fact",
                                "strict": True,
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "fact": {"type": "string"},
                                        "source_id": {"type": "integer"},
                                        "evidence": {"type": "string"},
                                    },
                                    "required": ["fact", "source_id", "evidence"],
                                    "additionalProperties": False,
                                },
                            }
                        },
                    }
                    result = await metered(
                        usage,
                        UsageScope("memory:synthetic", "acceptance"),
                        "memory",
                        entry["provider"],
                        entry["model"],
                        lambda: client.complete(payload, session_id="synthetic-memory"),
                        bounds={
                            "input_tokens": entry.get("context_window", 32768),
                            "output_tokens": await client.output_bound(payload),
                        },
                    )
                    data = json.loads(result["text"])
                    if data != {"fact": "likes tea", "source_id": 1, "evidence": "likes tea"}:
                        raise ValueError("Synthetic provenance result mismatch")
                    evidence.update(
                        state="completed",
                        usage=result.get("usage"),
                        ai_usage=await usage.snapshot("memory:synthetic"),
                    )
                finally:
                    await client.close()
                    await usage.close()
                    area.cleanup()
            else:
                with tempfile.TemporaryDirectory(prefix="pi-acceptance-") as folder:
                    root = Path(folder)
                    from assistant_runtime.core.ai_usage.service import UsageService
                    from assistant_runtime.core.ai_usage.settings import UsageSettings

                    usage = UsageService(root / "ai-usage", UsageSettings(max_requests=max_requests))
                    await usage.start()
                    client = PiClient(
                        config.pi_binary,
                        folder,
                        settings=bounded,
                        environment={"ASSISTANT_MODEL_GATEWAY_KEY": key},
                        runtime_dir=root / "runtime",
                        usage=usage,
                        accounts=NativeAccounts(config.root, bounded),
                    )
                    try:
                        await client.start()
                        reply = await client.request(
                            "session.create",
                            {
                                "model": model,
                                "config": {"model_reasoning_effort": selected_effort},
                                "instructions": "Synthetic integration test. Use only this temporary workspace and the supplied content. No external tools, network, personal files or extra tasks. Return a concise factual result.",
                            },
                        )
                        ident = reply["session"]["id"]
                        inputs = [{"type": "text", "text": "Reply exactly SYNTHETIC_OK."}]
                        if case == "tools":
                            inputs = [
                                {
                                    "type": "text",
                                    "text": "Use bash exactly once to write result.txt in the current directory with exactly SYNTHETIC_OK and read it back in that same command. Then report completion briefly. No network or other files.",
                                }
                            ]
                        if case == "image":
                            if not entry.get("images"):
                                raise ValueError("Model has no image capability")
                            path = root / "synthetic.png"
                            path.write_bytes(
                                base64.b64decode(
                                    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAF0lEQVR4nGP8z0AaYCJR/aiGUQ1DSAMAQC4BH2bjRnMAAAAASUVORK5CYII="
                                )
                            )
                            inputs = [
                                {
                                    "type": "text",
                                    "text": "Identify the solid background color in the supplied image. Reply with exactly one word: RED, GREEN, BLUE or UNKNOWN. Do not use tools or read files.",
                                },
                                {"type": "localImage", "path": str(path)},
                            ]
                        await client.request("run.start", {"session_id": ident, "input": inputs})
                        async with asyncio.timeout(90):
                            while True:
                                event = await client.events.get()
                                if event["kind"] == "usage.updated":
                                    evidence["usage"] = event["data"]["usage"]
                                if event["kind"] == "run.completed":
                                    result = event["data"]["run"]
                                    evidence["state"] = result["status"]
                                    evidence["ai_usage"] = result.get("ai_usage")
                                    if result.get("error"):
                                        evidence["provider_error"] = {
                                            k: str(v).replace(key, "<redacted>")[:300]
                                            for k, v in result["error"].items()
                                        }
                                    evidence["tool_activity"] = sum(
                                        i["type"] in {"file", "command", "mcp"} for i in result["items"]
                                    )
                                    break
                        if evidence["state"] != "completed":
                            raise RuntimeError("Execution did not complete")
                        if case == "tools" and (root / "result.txt").read_text().strip() != "SYNTHETIC_OK":
                            raise ValueError("File readback mismatch")
                        if case in {"text", "image"}:
                            answer = "".join(
                                item.get("text", "")
                                for item in result["items"]
                                if item["type"] == "message" and item.get("phase") == "final"
                            ).strip()
                            if answer != ("RED" if case == "image" else "SYNTHETIC_OK"):
                                raise ValueError("Synthetic answer mismatch")
                            if case == "image" and evidence["tool_activity"]:
                                raise ValueError("Image acceptance must use model vision, not file tools")
                    finally:
                        await client.close()
                        await usage.close()
        except Exception as exc:
            evidence.update(state="failed", error=type(exc).__name__)
        finally:
            evidence.update(
                upstream_statuses=http_statuses,
                requests=submitted,
                transport="pi_native" if native else "litellm",
                output_limit="model"
                if native
                and settings.accounts[settings.providers[entry["provider"]]["account"]].get("output_limit")
                == "model"
                else "request",
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
            if native:
                evidence["requests"] = (evidence.get("ai_usage") or {}).get("requests", 0)
            await runner.cleanup()
    return evidence


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--case", choices=["text", "tools", "image", "memory"], default="text")
    p.add_argument("--reasoning-effort")
    p.add_argument("--max-requests", type=int, default=4)
    p.add_argument("--max-output-tokens", type=int, default=1024)
    p.add_argument("--confirm-paid", action="store_true")
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if not a.confirm_paid:
        p.error("Prior data/budget authorization and --confirm-paid are required")
    if not 1 <= a.max_requests <= 8 or not 256 <= a.max_output_tokens <= 4096:
        p.error("Acceptance budget outside bounds")
    result = asyncio.run(
        verify(
            Config.load(a.config),
            a.model,
            case=a.case,
            effort=a.reasoning_effort,
            max_requests=a.max_requests,
            max_tokens=a.max_output_tokens,
        )
    )
    a.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    a.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    a.output.chmod(0o600)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["state"] == "completed" else 1)


if __name__ == "__main__":
    main()
