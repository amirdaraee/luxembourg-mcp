from __future__ import annotations

import argparse

from .server import McpServer


def main() -> None:
    parser = argparse.ArgumentParser(description="Official Luxembourg public data over MCP")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = McpServer()
    if args.transport == "stdio":
        server.run_stdio()
    else:
        server.run_http(args.host, args.port)


if __name__ == "__main__":
    main()

