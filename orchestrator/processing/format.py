"""Render processor results as compact Markdown."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def bullets(items: Iterable[str]) -> str:
    """Join lines as Markdown bullets, skipping empties."""
    lines = [f"- {item}" for item in items if item]
    return "\n".join(lines)


def code_block(value: Any, language: str = "") -> str:
    """Wrap a value in a fenced code block."""
    text = value if isinstance(value, str) else _pretty(value)
    fence = language
    return f"```{fence}\n{text}\n```"


def _pretty(value: Any) -> str:
    if isinstance(value, Mapping):
        parts = [f"{key}: {value[key]}" for key in value]
        return "\n".join(parts)
    return str(value)


def needs_input_message(kind: str, names: list[str]) -> str:
    """Ask for missing values without suggesting guesses."""
    label = "parameter" if kind == "param" else "context field"
    asked = [f"Missing required {label}: `{name}`" for name in names]
    asked.append("Provide the missing value(s) to continue. Do not guess.")
    return bullets(asked)


def success_message(tool_name: str, data: Any) -> str:
    """Format a successful tool result."""
    heading = [f"Tool `{tool_name}` completed."]
    body = code_block(data) if data not in (None, "") else ""
    if body:
        return "\n".join([bullets(heading), body])
    return bullets(heading)


def error_message(code: str, bottleneck: str, fallback: str, detail: str = "") -> str:
    """Format a failure with code, bottleneck, and fallback."""
    items = [
        f"Error code: `{code}`",
        f"Bottleneck: {bottleneck}",
        f"Fallback: {fallback}",
    ]
    if detail:
        items.append(f"Detail: {detail}")
    return bullets(items)
