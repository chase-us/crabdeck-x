"""Canonical assistant processing guidelines for CrabDeck X."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Guideline:
    """One immutable operating rule."""

    id: str
    title: str
    rule: str


GUIDELINES: tuple[Guideline, ...] = (
    Guideline(
        id="verify-before-execute",
        title="Verify context and parameters",
        rule=(
            "Always verify available context and parameters before executing a tool."
        ),
    ),
    Guideline(
        id="ask-dont-guess",
        title="Ask instead of guessing",
        rule=(
            "If a required parameter for a tool is missing, ask the user concisely "
            "instead of guessing."
        ),
    ),
    Guideline(
        id="structured-output",
        title="Structured responses",
        rule=(
            "Output clean, structured responses using Markdown bullets or code "
            "blocks. Avoid fluff."
        ),
    ),
    Guideline(
        id="error-fallback",
        title="Analyze failures and propose a fallback",
        rule=(
            "If an operation fails, analyze the error code, explain the bottleneck "
            "briefly, and propose a fallback option."
        ),
    ),
)

GUIDELINE_BY_ID = {item.id: item for item in GUIDELINES}


def guideline_ids() -> tuple[str, ...]:
    """Return guideline identifiers in declaration order."""
    return tuple(item.id for item in GUIDELINES)
