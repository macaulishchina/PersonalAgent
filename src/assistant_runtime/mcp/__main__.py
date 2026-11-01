"""Explicit MCP projection, using the owner's single tool declaration factory."""

import argparse
from assistant_runtime.api.deadlines import LOCAL_TRANSPORT_SECONDS
from importlib import import_module
from pathlib import Path
from .client import LocalRuntimeClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--task-run")
    parser.add_argument("--execution-session")
    parser.add_argument("--manage-integrations", action="store_true")
    args = parser.parse_args()
    client = LocalRuntimeClient(
        args.socket,
        timeout=LOCAL_TRANSPORT_SECONDS,
        execution_session=args.execution_session,
    )
    if args.service == "extensions":
        from .extensions import create_remote_server

        server = create_remote_server(client)
    else:
        from assistant_runtime.core.plugin_catalog import services

        owner = "devices" if args.service == "device" else args.service
        available = services((owner,))
        if args.service not in available:
            raise ValueError("Unknown declared MCP service")
        spec = available[args.service]
        module, attribute = spec.tools_factory.split(":")
        factory = getattr(import_module(module), attribute)
        kwargs = {"caller_run": args.task_run} if spec.task_context else {}
        if args.service == "integrations":
            kwargs["can_manage"] = args.manage_integrations
        server = factory(client, **kwargs)
    from .outcomes import ToolOutcomes

    server.add_middleware(ToolOutcomes())
    server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
