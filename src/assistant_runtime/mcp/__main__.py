"""One explicit stdio MCP service; no model or hardware process is started here."""

import argparse
from pathlib import Path

from .client import LocalRuntimeClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("memory", "device"))
    parser.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args()
    if args.service == "memory":
        from .memory import create_server
    else:
        from .device import create_server
    create_server(LocalRuntimeClient(args.socket)).run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
