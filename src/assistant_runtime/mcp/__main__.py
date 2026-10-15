"""One explicit stdio MCP service; no model or hardware process is started here."""

import argparse
from pathlib import Path

from .client import LocalRuntimeClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("memory", "device", "tasks", "notifications"))
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--task-run")
    args = parser.parse_args()
    if args.service == "notifications":
        from .notifications import create_server

        create_server(LocalRuntimeClient(args.socket), args.task_run).run(
            transport="stdio", show_banner=False
        )
        return
    if args.service == "tasks":
        from .tasks import create_server

        create_server(LocalRuntimeClient(args.socket), args.task_run).run(
            transport="stdio", show_banner=False
        )
        return
    if args.service == "memory":
        from .memory import create_server
    else:
        from .device import create_server
    create_server(LocalRuntimeClient(args.socket)).run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
