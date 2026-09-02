"""CLI for verifying and executing a registered tool under the guidelines."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from orchestrator.processing.engine import AssistantProcessor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify context/parameters, then run a CrabDeck tool."
    )
    parser.add_argument("--tool", help="Registered tool name")
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        help="Tool parameter as key=value (repeatable)",
    )
    parser.add_argument(
        "--context",
        action="append",
        default=[],
        help="Context field as key=value (repeatable)",
    )
    parser.add_argument(
        "--list-guidelines",
        action="store_true",
        help="Print the processing guidelines and exit",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Print registered tools and exit",
    )
    args = parser.parse_args(argv)

    processor = AssistantProcessor()

    if args.list_guidelines:
        print("\n".join(f"- `{item}`" for item in processor.list_guidelines()))
        return 0

    if args.list_tools:
        for name in sorted(processor._tools):
            spec = processor._tools[name]
            required = ", ".join(spec.required_params) or "(none)"
            print(f"- `{name}` — params: {required}")
        return 0

    if not args.tool:
        print("- Missing required parameter: `--tool`")
        print("- Provide `--tool` to continue. Do not guess.")
        return 2

    params = _pairs(args.param)
    context = _pairs(args.context)

    def echo_handler(resolved_params: Any, resolved_context: Any) -> dict[str, Any]:
        return {"params": dict(resolved_params), "context": dict(resolved_context)}

    result = processor.execute(
        args.tool,
        params=params,
        context=context,
        handler=echo_handler,
    )
    print(result.message)
    if result.status == "ok":
        return 0
    if result.status == "needs_input":
        return 2
    return 1


def _pairs(items: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(
                f"- Missing required parameter value for `{item}`\n"
                "- Use `key=value`. Do not guess."
            )
        key, value = item.split("=", 1)
        if not key.strip():
            raise SystemExit("- Missing required parameter: key in `--param`/`--context`")
        parsed[key.strip()] = value
    return parsed


if __name__ == "__main__":
    sys.exit(main())
