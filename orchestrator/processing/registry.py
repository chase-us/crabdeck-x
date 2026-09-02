"""Tool specifications the processor can validate against."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    """Declared contract for a single tool or workflow step."""

    name: str
    required_params: tuple[str, ...] = ()
    required_context: tuple[str, ...] = ()
    description: str = ""
    fallback: str = "Retry with complete parameters, or skip this step."


def default_registry() -> dict[str, ToolSpec]:
    """CrabDeck tools that must pass guideline checks before dispatch."""
    return {
        "dispatch_prompt": ToolSpec(
            name="dispatch_prompt",
            required_params=("prompt",),
            required_context=("gateway_url",),
            description="Send a PROMPT event to Hermes via the gateway.",
            fallback="Queue the prompt locally and retry when the gateway is reachable.",
        ),
        "dispatch_task": ToolSpec(
            name="dispatch_task",
            required_params=("task",),
            required_context=("gateway_url",),
            description="Send a TASK event to OpenClaw via the gateway.",
            fallback="Hold the task in the workflow queue until OpenClaw heartbeats.",
        ),
        "tool_request": ToolSpec(
            name="tool_request",
            required_params=("tool",),
            required_context=("gateway_url",),
            description="Route a TOOL_REQUEST through Hermes.",
            fallback="Return needs_input if the tool name is unknown; do not invent one.",
        ),
    }


def merge_registry(
    extra: dict[str, ToolSpec] | None = None,
) -> dict[str, ToolSpec]:
    """Return the default registry plus optional extra tools."""
    registry = dict(default_registry())
    if extra:
        registry.update(extra)
    return registry


def required_keys(spec: ToolSpec) -> tuple[str, ...]:
    """Flatten required param and context names for prompts."""
    return spec.required_params + spec.required_context
