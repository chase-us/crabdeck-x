"""Assistant processing engine for CrabDeck X.

Public surface is intentionally small: load guidelines, validate a tool
call, execute only when context and parameters are complete, and return
structured Markdown.
"""

from orchestrator.processing.engine import AssistantProcessor, ProcessResult
from orchestrator.processing.errors import ErrorReport
from orchestrator.processing.guidelines import GUIDELINES, Guideline
from orchestrator.processing.registry import ToolSpec, default_registry

__all__ = [
    "AssistantProcessor",
    "ErrorReport",
    "GUIDELINES",
    "Guideline",
    "ProcessResult",
    "ToolSpec",
    "default_registry",
]
