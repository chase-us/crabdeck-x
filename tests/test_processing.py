"""Guideline checks for the assistant processing engine."""

from __future__ import annotations

import unittest
from pathlib import Path

from orchestrator.processing.engine import AssistantProcessor
from orchestrator.processing.errors import classify_error
from orchestrator.processing.guidelines import GUIDELINES, guideline_ids
from orchestrator.processing.registry import ToolSpec
from orchestrator.processing.workflow import load_workflow, tools_from_workflow

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "workflows" / "assistant-processing.yaml"


class GuidelineContractTests(unittest.TestCase):
    def test_four_guidelines_in_order(self) -> None:
        self.assertEqual(
            guideline_ids(),
            (
                "verify-before-execute",
                "ask-dont-guess",
                "structured-output",
                "error-fallback",
            ),
        )

    def test_rules_match_operating_contract(self) -> None:
        text = " ".join(item.rule for item in GUIDELINES)
        self.assertIn("verify available context and parameters", text)
        self.assertIn("ask the user concisely instead of guessing", text)
        self.assertIn("Markdown bullets or code blocks", text)
        self.assertIn("propose a fallback option", text)


class ProcessorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.processor = AssistantProcessor()
        self.calls: list[tuple[object, object]] = []

    def _handler(self, params: object, context: object) -> dict[str, str]:
        self.calls.append((params, context))
        return {"echo": "ok"}

    def test_missing_param_asks_and_does_not_run_handler(self) -> None:
        result = self.processor.execute(
            "dispatch_prompt",
            params={},
            context={"gateway_url": "ws://localhost:8765"},
            handler=self._handler,
        )
        self.assertEqual(result.status, "needs_input")
        self.assertEqual(result.missing, ("prompt",))
        self.assertIn("Missing required parameter: `prompt`", result.message)
        self.assertIn("Do not guess", result.message)
        self.assertEqual(self.calls, [])

    def test_blank_param_is_treated_as_missing(self) -> None:
        result = self.processor.execute(
            "dispatch_task",
            params={"task": "   "},
            context={"gateway_url": "ws://localhost:8765"},
            handler=self._handler,
        )
        self.assertEqual(result.status, "needs_input")
        self.assertEqual(result.missing, ("task",))
        self.assertEqual(self.calls, [])

    def test_missing_context_asks_before_params(self) -> None:
        result = self.processor.execute(
            "dispatch_prompt",
            params={"prompt": "hello"},
            context={},
            handler=self._handler,
        )
        self.assertEqual(result.status, "needs_input")
        self.assertEqual(result.missing, ("gateway_url",))
        self.assertIn("Missing required context field: `gateway_url`", result.message)
        self.assertEqual(self.calls, [])

    def test_success_returns_structured_markdown(self) -> None:
        result = self.processor.execute(
            "dispatch_prompt",
            params={"prompt": "status"},
            context={"gateway_url": "ws://localhost:8765"},
            handler=self._handler,
        )
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.message.startswith("- Tool `dispatch_prompt` completed."))
        self.assertIn("```", result.message)
        self.assertEqual(len(self.calls), 1)

    def test_unknown_tool_is_error_not_guessed(self) -> None:
        result = self.processor.execute("not_a_real_tool", handler=self._handler)
        self.assertEqual(result.status, "error")
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertEqual(result.error.code, "NOT_FOUND")
        self.assertIn("Error code: `NOT_FOUND`", result.message)
        self.assertIn("Fallback:", result.message)
        self.assertEqual(self.calls, [])

    def test_timeout_is_classified_with_fallback(self) -> None:
        def boom(_params: object, _context: object) -> None:
            raise TimeoutError("ollama generate exceeded 120s")

        result = self.processor.execute(
            "dispatch_prompt",
            params={"prompt": "hello"},
            context={"gateway_url": "ws://localhost:8765"},
            handler=boom,
        )
        self.assertEqual(result.status, "error")
        assert result.error is not None
        self.assertEqual(result.error.code, "TIMEOUT")
        self.assertIn("Bottleneck:", result.message)
        self.assertIn("Fallback:", result.message)

    def test_connection_error_uses_tool_fallback(self) -> None:
        def boom(_params: object, _context: object) -> None:
            raise ConnectionError("gateway refused")

        result = self.processor.execute(
            "dispatch_task",
            params={"task": "ping"},
            context={"gateway_url": "ws://localhost:8765"},
            handler=boom,
        )
        assert result.error is not None
        self.assertEqual(result.error.code, "CONNECTION")
        self.assertIn("heartbeat", result.error.fallback.lower())


class ErrorClassifierTests(unittest.TestCase):
    def test_permission_is_auth(self) -> None:
        report = classify_error(PermissionError("denied"))
        self.assertEqual(report.code, "AUTH")

    def test_key_error_is_not_found(self) -> None:
        report = classify_error(KeyError("model"))
        self.assertEqual(report.code, "NOT_FOUND")
        self.assertIn("Ask for the missing identifier", report.fallback)

    def test_unknown_uses_provided_fallback(self) -> None:
        report = classify_error(RuntimeError("weird"), fallback="Skip the step.")
        self.assertEqual(report.code, "UNKNOWN")
        self.assertEqual(report.fallback, "Skip the step.")


class WorkflowLoadTests(unittest.TestCase):
    def test_workflow_file_loads_tools_and_rules(self) -> None:
        document = load_workflow(WORKFLOW)
        self.assertEqual(document["name"], "assistant-processing")
        self.assertEqual(document["version"], 1)
        rule_ids = [item["id"] for item in document["rules"]]
        self.assertEqual(list(guideline_ids()), rule_ids)

        specs = tools_from_workflow(document)
        self.assertIn("dispatch_prompt", specs)
        self.assertEqual(specs["dispatch_prompt"].required_params, ("prompt",))
        self.assertEqual(specs["dispatch_prompt"].required_context, ("gateway_url",))

    def test_processor_accepts_workflow_tools(self) -> None:
        specs = tools_from_workflow(load_workflow(WORKFLOW))
        extra = {
            "custom_step": ToolSpec(
                name="custom_step",
                required_params=("name",),
            )
        }
        processor = AssistantProcessor({**specs, **extra})
        result = processor.execute("custom_step", params={})
        self.assertEqual(result.status, "needs_input")
        self.assertEqual(result.missing, ("name",))


if __name__ == "__main__":
    unittest.main()
