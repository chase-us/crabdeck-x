"""Guideline-enforcing tool execution engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping

from orchestrator.processing.errors import ErrorReport, classify_error, report_from_code
from orchestrator.processing.format import (
    error_message,
    needs_input_message,
    success_message,
)
from orchestrator.processing.guidelines import GUIDELINES
from orchestrator.processing.registry import ToolSpec, merge_registry


Status = Literal["ok", "needs_input", "error"]
Handler = Callable[[Mapping[str, Any], Mapping[str, Any]], Any]


@dataclass(frozen=True)
class ProcessResult:
    """Outcome of a verified tool invocation."""

    status: Status
    message: str
    missing: tuple[str, ...] = ()
    error: ErrorReport | None = None
    data: Any = None
    tool: str | None = None
    applied_guidelines: tuple[str, ...] = field(default_factory=tuple)


class AssistantProcessor:
    """Validate context and parameters, then run a tool handler.

    The handler is never called when required inputs are missing.
    Missing values are requested, never invented.
    """

    def __init__(self, tools: Mapping[str, ToolSpec] | None = None) -> None:
        self._tools = merge_registry(dict(tools) if tools else None)

    def list_guidelines(self) -> tuple[str, ...]:
        """Return guideline ids this processor enforces."""
        return tuple(item.id for item in GUIDELINES)

    def get_tool(self, name: str) -> ToolSpec | None:
        """Look up a registered tool without inventing one."""
        return self._tools.get(name)

    def missing_params(
        self,
        spec: ToolSpec,
        params: Mapping[str, Any] | None,
    ) -> list[str]:
        """Return required parameter names that are absent or empty."""
        return _missing(spec.required_params, params)

    def missing_context(
        self,
        spec: ToolSpec,
        context: Mapping[str, Any] | None,
    ) -> list[str]:
        """Return required context names that are absent or empty."""
        return _missing(spec.required_context, context)

    def execute(
        self,
        tool_name: str,
        params: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        handler: Handler | None = None,
    ) -> ProcessResult:
        """Verify inputs, then execute. Never guess missing fields."""
        spec = self._tools.get(tool_name)
        if spec is None:
            report = report_from_code(
                "NOT_FOUND",
                bottleneck=f"Tool `{tool_name}` is not registered.",
                fallback="List registered tools and pass an exact name. Do not invent one.",
                detail=tool_name,
            )
            return ProcessResult(
                status="error",
                message=error_message(
                    report.code, report.bottleneck, report.fallback, report.detail
                ),
                error=report,
                tool=tool_name,
                applied_guidelines=("verify-before-execute", "error-fallback"),
            )

        params = params or {}
        context = context or {}
        missing_ctx = self.missing_context(spec, context)
        if missing_ctx:
            return ProcessResult(
                status="needs_input",
                message=needs_input_message("context", missing_ctx),
                missing=tuple(missing_ctx),
                tool=tool_name,
                applied_guidelines=("verify-before-execute", "ask-dont-guess"),
            )

        missing_params = self.missing_params(spec, params)
        if missing_params:
            return ProcessResult(
                status="needs_input",
                message=needs_input_message("param", missing_params),
                missing=tuple(missing_params),
                tool=tool_name,
                applied_guidelines=("verify-before-execute", "ask-dont-guess"),
            )

        if handler is None:
            report = report_from_code(
                "VALIDATION",
                bottleneck="No handler was provided for a fully specified tool call.",
                fallback="Register a handler for this tool, then retry with the same parameters.",
            )
            return ProcessResult(
                status="error",
                message=error_message(report.code, report.bottleneck, report.fallback),
                error=report,
                tool=tool_name,
                applied_guidelines=("verify-before-execute", "error-fallback"),
            )

        try:
            data = handler(params, context)
        except Exception as exc:
            report = classify_error(exc, spec.fallback)
            return ProcessResult(
                status="error",
                message=error_message(
                    report.code, report.bottleneck, report.fallback, report.detail
                ),
                error=report,
                tool=tool_name,
                applied_guidelines=("error-fallback", "structured-output"),
            )

        return ProcessResult(
            status="ok",
            message=success_message(tool_name, data),
            data=data,
            tool=tool_name,
            applied_guidelines=("verify-before-execute", "structured-output"),
        )


def _missing(required: tuple[str, ...], values: Mapping[str, Any] | None) -> list[str]:
    if not required:
        return []
    payload = values or {}
    missing: list[str] = []
    for name in required:
        if name not in payload or _is_blank(payload[name]):
            missing.append(name)
    return missing


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False
