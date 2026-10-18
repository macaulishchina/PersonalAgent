"""Explicit synthetic paid acceptance: local 402 rejection followed by real model work.

Uses an isolated acceptance configuration and loopback fault injector. No production
service/config changes, no household inputs, and no intentionally invalid real keys.
"""

import argparse
import asyncio
import copy
import json
import tempfile
import uuid

import aiohttp
from aiohttp import web

from assistant_runtime.config import Config
from assistant_runtime.kernel.models import ModelRequest
from assistant_runtime.mcp.client import LocalRuntimeClient
from assistant_runtime.plugins.models.settings import ModelSettings
from assistant_runtime.plugins.models.gateway import gateway_credential
from scripts.model_acceptance_host import AcceptanceHost
from scripts.prepare_model_gateway import private_write
from scripts.verify_model_workflows import create, finished, wait_for


async def verify(config, target):
    if "acceptance" not in config.root.parts:
        raise ValueError("Use a separate acceptance instance")
    config.model_api = copy.deepcopy(config.model_api)
    entry = config.model_api["entries"][target]
    entry["execution_validated"] = True  # In-memory test gate only.
    config.model_api["entries"]["synthetic-rejected"] = {**entry, "fallbacks": [target]}
    upstream = config.model_api["gateway_url"]
    key = gateway_credential(config.root, ModelSettings.parse(config.model_api))
    calls = []
    output = config.state_dir / "acceptance/fallbacks" / str(uuid.uuid4())
    output.mkdir(parents=True, mode=0o700)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=80)) as session:

        async def dispatch(request):
            if request.headers.get("Authorization") != "Bearer " + key:
                return web.Response(status=401)
            if request.method == "GET":
                async with session.get(
                    upstream + "/models", headers={"Authorization": "Bearer " + key}
                ) as reply:
                    return web.Response(
                        status=reply.status, body=await reply.read(), content_type="application/json"
                    )
            payload = await request.json()
            alias = payload.get("model")
            calls.append(alias)
            if alias == "synthetic-rejected":
                return web.json_response(
                    {
                        "error": {
                            "message": "Synthetic insufficient balance",
                            "type": "insufficient_quota",
                            "code": "insufficient_quota",
                        }
                    },
                    status=402,
                )
            headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower()
                in {"authorization", "user-agent", "thread-id", "x-assistant-session", "x-opencode-session"}
            }
            async with session.post(upstream + "/responses", json=payload, headers=headers) as reply:
                response = web.StreamResponse(
                    status=reply.status,
                    headers={"Content-Type": reply.headers.get("Content-Type", "application/json")},
                )
                await response.prepare(request)
                async for chunk in reply.content.iter_chunked(65536):
                    await response.write(chunk)
                return response

        app = web.Application()
        app.router.add_route("*", "/v1/{path:.*}", dispatch)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        config.model_api["gateway_url"] = f"http://127.0.0.1:{port}/v1"
        try:
            with tempfile.TemporaryDirectory(prefix="pa-fallback-") as directory:
                host = AcceptanceHost(config, directory)
                try:
                    await host.start()
                    host.tasks.model_policy.fallbacks = {
                        "synthetic-rejected": [{"model": target, "reasoning_effort": "none"}]
                    }
                    request = ModelRequest(
                        "infer",
                        {
                            "request_id": str(uuid.uuid4()),
                            "model": "synthetic-rejected",
                            "text": "Synthetic code check: what integer is returned by 17 + 25? Reply 42 only.",
                        },
                    )
                    auxiliary = await host.runtime.invoke(request)
                    if auxiliary.get("selected_model") != target or auxiliary.get("text", "").strip() != "42":
                        raise RuntimeError("Auxiliary fallback did not produce the expected result")
                    count = len(calls)
                    assert await host.runtime.invoke(request) == auxiliary
                    assert len(calls) == count
                    task = await create(
                        LocalRuntimeClient(host.config.socket),
                        "synthetic-rejected",
                        "Synthetic fallback file proof",
                        "Synthetic coding acceptance. Use apply_patch to create proof.txt in your scratch directory containing exactly FALLBACK_OK. Use a shell command to read proof.txt and verify it. Call tasks get_task for your own task once to verify MCP identity. Finish with FALLBACK_OK. Do not access other files, network, devices or credentials. At most four tool calls.",
                    )
                    ended = await wait_for(lambda: finished(host, task["id"]), timeout=180)
                    trace = host.tasks.trace.read({"run_id": ended["id"]})
                    private_write(
                        output / "task.json",
                        json.dumps({"run": ended, "trace": trace}, ensure_ascii=False, indent=2),
                    )
                    private_write(output / "events.json", json.dumps(host.api_events, ensure_ascii=False))
                    if ended["state"] != "completed" or ended["data"]["spec"]["execution"]["model"] != target:
                        raise RuntimeError("Task fallback did not complete; inspect private task receipt")
                    proof = host.workfiles.area(host.tasks.owner(ended)) / "scratch/proof.txt"
                    if proof.read_text().strip() != "FALLBACK_OK":
                        raise RuntimeError("Missing actual file proof")
                    if calls.count("synthetic-rejected") != 2:
                        raise RuntimeError("Rejected model was unexpectedly retried")
                    result = {
                        "passed": True,
                        "target": target,
                        "synthetic_only": True,
                        "auxiliary": auxiliary,
                        "run_id": ended["id"],
                        "calls": calls,
                        "tool_trace": trace["items"],
                    }
                    private_write(output / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
                    print("Fallback acceptance passed:", output, flush=True)
                finally:
                    await host.close()
        finally:
            await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--confirm-paid", action="store_true")
    args = parser.parse_args()
    if not args.confirm_paid:
        parser.error("Explicit paid synthetic acceptance authorization is required")
    asyncio.run(verify(Config.load(args.config), args.model))


if __name__ == "__main__":
    main()
