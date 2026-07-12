"""Compact command-line access to the Luxembourg MCP tool registry."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .providers import LuxembourgData
from .server import McpServer


def compact(result: Any, limit: int, summary_only: bool = False) -> Any:
    """Limit top-level result collections without mutating nested structures."""
    if isinstance(result, list):
        return result[:limit]
    if not isinstance(result, dict):
        return result

    output = dict(result)
    list_keys = [key for key, value in result.items() if isinstance(value, list)]
    for key in list_keys:
        output[key] = result[key][:limit]
    if list_keys:
        output["returned_items"] = {
            key: min(len(result[key]), limit) for key in list_keys
        }
        output["available_items"] = {key: len(result[key]) for key in list_keys}
    if summary_only:
        for key in list_keys:
            output.pop(key, None)
    return output


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: Sequence[str] | None = None, data: LuxembourgData | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="luxembourg-data",
        description="Query official Luxembourg public data without running an MCP client.",
    )
    parser.add_argument("tool", help="Registered tool name, or 'list'")
    parser.add_argument(
        "arguments",
        nargs="?",
        default="{}",
        help="JSON object containing the tool arguments",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum items retained in each list (default: 20)",
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--full", action="store_true", help="Print the complete provider response"
    )
    output_group.add_argument(
        "--summary-only",
        action="store_true",
        help="Print metadata and counts without top-level list rows",
    )
    parser.add_argument(
        "--save",
        metavar="PATH",
        help="Save the complete response as JSON and print a compact receipt",
    )
    args = parser.parse_args(argv)

    if not 1 <= args.limit <= 500:
        parser.error("--limit must be between 1 and 500")

    server = McpServer(data)
    if args.tool == "list":
        _print_json({
            "count": len(server.tools),
            "tools": [tool.definition() for tool in server.tools.values()],
        })
        return 0
    if args.tool not in server.tools:
        parser.error(f"unknown tool: {args.tool}")

    try:
        arguments = json.loads(args.arguments)
    except json.JSONDecodeError as exc:
        parser.error(f"arguments must be valid JSON: {exc}")
    if not isinstance(arguments, dict):
        parser.error("arguments must be a JSON object")

    response = server.handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": args.tool, "arguments": arguments},
    })
    if response is None:
        print("Tool returned no response", file=sys.stderr)
        return 2
    result = response["result"]
    if result.get("isError"):
        print(result["content"][0]["text"], file=sys.stderr)
        return 2
    value = result["structuredContent"]

    if args.save:
        destination = Path(os.path.expanduser(args.save)).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        receipt = compact(value, args.limit, summary_only=True)
        receipt.update({
            "tool": args.tool,
            "saved": str(destination),
            "bytes": destination.stat().st_size,
        })
        _print_json(receipt)
        return 0

    _print_json(value if args.full else compact(value, args.limit, args.summary_only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
