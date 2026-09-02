"""Load a YAML-lite workflow definition without extra dependencies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.processing.registry import ToolSpec


def load_workflow(path: str | Path) -> dict[str, Any]:
    """Parse the assistant-processing workflow file.

    The format is a constrained subset of YAML used by this repo so the
    processor stays stdlib-only. Unknown keys are ignored.
    """
    text = Path(path).read_text(encoding="utf-8")
    return _parse_simple_yaml(text)


def tools_from_workflow(document: dict[str, Any]) -> dict[str, ToolSpec]:
    """Build ToolSpec entries from a parsed workflow document."""
    specs: dict[str, ToolSpec] = {}
    for raw in document.get("tools", []):
        if not isinstance(raw, dict) or "name" not in raw:
            continue
        name = str(raw["name"])
        specs[name] = ToolSpec(
            name=name,
            required_params=_as_tuple(raw.get("required_params")),
            required_context=_as_tuple(raw.get("required_context")),
            description=str(raw.get("description", "")),
            fallback=str(
                raw.get(
                    "fallback",
                    "Retry with complete parameters, or skip this step.",
                )
            ),
        )
    return specs


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return (str(value),)


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the small YAML subset used in workflows/."""
    root: dict[str, Any] = {}
    current_list: list[Any] | None = None
    current_list_key: str | None = None
    current_item: dict[str, Any] | None = None
    current_nested_key: str | None = None
    current_nested_list: list[str] | None = None

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        if indent == 0 and line.endswith(":") and ":" == line[-1] and line.count(":") == 1:
            key = line[:-1].strip()
            current_list = []
            root[key] = current_list
            current_list_key = key
            current_item = None
            current_nested_key = None
            current_nested_list = None
            continue

        if indent == 0 and ":" in line:
            key, value = _split_kv(line)
            root[key] = _scalar(value)
            current_list = None
            current_list_key = None
            current_item = None
            continue

        if current_list is None or current_list_key is None:
            continue

        if indent == 2 and line.startswith("- "):
            rest = line[2:].strip()
            current_nested_key = None
            current_nested_list = None
            if ":" in rest:
                key, value = _split_kv(rest)
                current_item = {key: _scalar(value) if value else None}
                if value == "" or value is None:
                    # `- id:` with nested scalars on following lines, or `- name: foo`
                    if value == "":
                        current_item[key] = None
                current_list.append(current_item)
            else:
                current_item = None
                current_list.append(_scalar(rest))
            continue

        if current_item is None:
            continue

        if indent == 4 and line.endswith(":") and line.count(":") == 1:
            key = line[:-1].strip()
            current_nested_list = []
            current_item[key] = current_nested_list
            current_nested_key = key
            continue

        if indent == 4 and ":" in line:
            key, value = _split_kv(line)
            current_item[key] = _scalar(value)
            current_nested_key = None
            current_nested_list = None
            continue

        if indent >= 6 and line.startswith("- ") and current_nested_list is not None:
            current_nested_list.append(_scalar(line[2:].strip()))

    _drop_null_fields(root)
    return root


def _drop_null_fields(root: dict[str, Any]) -> None:
    tools = root.get("tools")
    if not isinstance(tools, list):
        return
    for item in tools:
        if isinstance(item, dict):
            for key in list(item):
                if item[key] is None:
                    del item[key]


def _split_kv(line: str) -> tuple[str, str]:
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def _scalar(value: str) -> Any:
    if value == "" or value is None:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value.isdigit():
        return int(value)
    return value
