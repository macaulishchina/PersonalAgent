"""Explicit live stdio Media MCP acceptance. No inference or tracking plan is created.

Default is read-only. --file-id opts into one cloud acquisition in --target and
reuses --request-id on a lost reply. --download-file-id explicitly downloads one
cloud original to its configured local library. Never deletes or overwrites films.
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys
import uuid

from fastmcp import Client

from assistant_runtime.app.config import Config
from assistant_runtime.mcp.catalog import MEDIA_TOOLS


async def verify(args):
    config = Config.load(args.config)
    command = ["-m", "assistant_runtime.mcp", "media", "--socket", str(config.socket)]
    if args.task_run:
        command += ["--task-run", args.task_run]
    async with Client({"mcpServers": {"media": {"command": sys.executable, "args": command}}}) as client:
        tools = {tool.name for tool in await client.list_tools()}
        if tools != set(MEDIA_TOOLS):
            raise RuntimeError("Media tool catalogue mismatch")

        async def call(name, **arguments):
            return (await client.call_tool(name, arguments)).structured_content

        status = await call("media_status")
        print(
            json.dumps(
                {
                    "tools_verified": len(tools),
                    "accounts": status["accounts"],
                    "target_names": list(status["targets"]),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if args.query:
            found = await call(
                "search_media", query=args.query, aliases=args.alias, platforms=args.platform, limit=20
            )
            print(
                json.dumps(
                    {
                        k: found[k]
                        for k in ("search_id", "complete", "counts", "outcome", "diagnostics", "next_actions")
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        if args.sources:
            print(json.dumps(await call("media_search_sources"), ensure_ascii=False), flush=True)
        if args.history:
            history = await call("media_search_history", limit=5)
            print(
                json.dumps({"history_ids": [i["id"] for i in history["items"]], "before": history["before"]}),
                flush=True,
            )
        if args.share_url:
            inspected = await call(
                "inspect_media_share",
                share_url=args.share_url,
                parent_file_id=args.parent_file_id,
                account=args.account,
            )
            print(
                json.dumps(
                    {"share_file_count": len(inspected["items"]), "has_more": bool(inspected.get("marker"))}
                ),
                flush=True,
            )
        if args.download_file_id:
            if not args.target or args.file_id:
                raise ValueError("Download requires --target and cannot combine with a share transfer")
            request_id = args.request_id or str(uuid.uuid4())
            print(json.dumps({"request_id": request_id}), flush=True)
            item = await call(
                "download_media", request_id=request_id, target=args.target, file_id=args.download_file_id
            )
            print(json.dumps(item, ensure_ascii=False), flush=True)
            # Durable work survives this acceptance client's exit. --operation-id
            # reads the receipt again; no long-running model/task is needed.
        elif args.file_id:
            if not args.share_url or not args.target:
                raise ValueError("Transfer requires --share-url and --target")
            request_id = args.request_id or str(uuid.uuid4())
            print(json.dumps({"request_id": request_id}), flush=True)
            item = await call(
                "transfer_media",
                request_id=request_id,
                share_url=args.share_url,
                parent_file_id=args.parent_file_id,
                file_id=args.file_id,
                target=args.target,
            )
            print(json.dumps({"operation_id": item["operation_id"]}), flush=True)
            async with asyncio.timeout(180):
                while item["state"] not in {"completed", "failed", "uncertain", "waiting_auth", "cancelled"}:
                    await asyncio.sleep(1)
                    item = await call("get_media_operation", operation_id=item["operation_id"])
            if item["state"] == "completed":
                item = await call("verify_media_operation", operation_id=item["operation_id"], probe=True)
            print(json.dumps(item, ensure_ascii=False), flush=True)
            if item["state"] != "completed":
                raise RuntimeError("Media acquisition not confirmed: " + item["state"])
        elif args.operation_id:
            item = await call("verify_media_operation", operation_id=args.operation_id, probe=True)
            print(json.dumps(item, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--query")
    parser.add_argument("--alias", action="append", help="Known title alias; at most three")
    parser.add_argument("--platform", action="append", help="Discovery platform; defaults to all")
    parser.add_argument(
        "--sources", action="store_true", help="Inspect configured source inventory/recent evidence"
    )
    parser.add_argument("--history", action="store_true", help="Verify bounded persisted search history")
    parser.add_argument("--share-url")
    parser.add_argument(
        "--account", help="Account alias for share inspection when multiple accounts use one provider"
    )
    parser.add_argument("--parent-file-id", default="root")
    parser.add_argument("--file-id")
    parser.add_argument(
        "--download-file-id", help="Explicit local download of a chosen file already in the cloud target"
    )
    parser.add_argument("--target")
    parser.add_argument("--request-id")
    parser.add_argument("--operation-id")
    parser.add_argument(
        "--task-run", help="Existing active authorized task execution; never borrowed from another task"
    )
    asyncio.run(verify(parser.parse_args()))


if __name__ == "__main__":
    main()
