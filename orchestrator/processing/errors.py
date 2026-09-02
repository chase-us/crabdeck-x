"""Map failures to an error code, bottleneck, and fallback."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import socket
from typing import Any


@dataclass(frozen=True)
class ErrorReport:
    """Structured failure analysis. Never includes guessed recovery data."""

    code: str
    bottleneck: str
    fallback: str
    detail: str


_TIMEOUT_TYPES = (TimeoutError,)
try:
    import asyncio

    _TIMEOUT_TYPES = (TimeoutError, asyncio.TimeoutError)
except Exception:  # pragma: no cover - asyncio always present
    pass


def classify_error(exc: BaseException, fallback: str | None = None) -> ErrorReport:
    """Turn an exception into a guideline-compliant error report."""
    message = str(exc).strip() or exc.__class__.__name__
    default_fallback = fallback or (
        "Retry once with verified parameters, or abort the step and report status."
    )

    if isinstance(exc, _TIMEOUT_TYPES):
        return ErrorReport(
            code="TIMEOUT",
            bottleneck="The operation exceeded its time budget before a response arrived.",
            fallback="Retry once with a shorter payload, or fail the step and continue the workflow.",
            detail=message,
        )

    if isinstance(exc, ConnectionError) or isinstance(exc, socket.error):
        return ErrorReport(
            code="CONNECTION",
            bottleneck="The remote service was unreachable or dropped the connection.",
            fallback="Check the gateway URL, wait for a heartbeat, then retry.",
            detail=message,
        )

    if isinstance(exc, PermissionError):
        return ErrorReport(
            code="AUTH",
            bottleneck="The caller is not authorized for this operation.",
            fallback="Supply valid credentials or skip the privileged step.",
            detail=message,
        )

    if isinstance(exc, FileNotFoundError) or isinstance(exc, KeyError):
        return ErrorReport(
            code="NOT_FOUND",
            bottleneck="A required resource or key was not present.",
            fallback="Ask for the missing identifier instead of substituting a default.",
            detail=message,
        )

    if isinstance(exc, ValueError) or isinstance(exc, TypeError):
        return ErrorReport(
            code="VALIDATION",
            bottleneck="An argument failed type or value checks after dispatch.",
            fallback="Return needs_input for the invalid field; do not coerce unknown values.",
            detail=message,
        )

    err_no = getattr(exc, "errno", None)
    if err_no in {errno.ETIMEDOUT, errno.EAGAIN}:
        return ErrorReport(
            code="TIMEOUT",
            bottleneck="The operating system reported a blocking I/O timeout.",
            fallback="Backoff and retry once, then mark the step failed.",
            detail=message,
        )
    if err_no in {errno.ECONNREFUSED, errno.ECONNRESET, errno.EHOSTUNREACH}:
        return ErrorReport(
            code="CONNECTION",
            bottleneck="The operating system reported a connection failure.",
            fallback="Verify the service is listening, then retry.",
            detail=message,
        )

    lowered = message.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return ErrorReport(
            code="TIMEOUT",
            bottleneck="The operation exceeded its time budget before a response arrived.",
            fallback="Retry once with a shorter payload, or fail the step and continue the workflow.",
            detail=message,
        )
    if "unauthorized" in lowered or "forbidden" in lowered or "401" in lowered:
        return ErrorReport(
            code="AUTH",
            bottleneck="The callee rejected the request as unauthorized.",
            fallback="Refresh credentials or stop the workflow until auth is restored.",
            detail=message,
        )

    return ErrorReport(
        code="UNKNOWN",
        bottleneck="The operation raised an unclassified exception.",
        fallback=default_fallback,
        detail=message,
    )


def report_from_code(
    code: str,
    *,
    bottleneck: str,
    fallback: str,
    detail: str = "",
) -> ErrorReport:
    """Build a report when the caller already knows the error code."""
    return ErrorReport(
        code=code,
        bottleneck=bottleneck,
        fallback=fallback,
        detail=detail,
    )


def as_dict(report: ErrorReport) -> dict[str, Any]:
    """Serialize an error report without extra fields."""
    return {
        "code": report.code,
        "bottleneck": report.bottleneck,
        "fallback": report.fallback,
        "detail": report.detail,
    }
