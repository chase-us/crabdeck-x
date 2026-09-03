"""Tests for RAG and swarm mesh collaboration."""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rag import format_context, inject_rag, query_memory
from swarm_mesh import (
    build_delegate_message,
    build_peer_response,
    build_swarm_result,
    decompose_goal,
    enrich_with_rag,
    new_session_id,
    parse_delegate_payload,
    synthesize_results,
    SwarmTask,
)


class RagTests(unittest.TestCase):
    def test_inject_rag_prepends_context(self) -> None:
        ctx = "Relevant prior swarm context:\n[1] (hermes/prompt) hello"
        out = inject_rag("What happened?", ctx)
        self.assertIn("What happened?", out)
        self.assertIn("hello", out)

    def test_inject_rag_without_context(self) -> None:
        self.assertEqual(inject_rag("plain", ""), "plain")

    def test_format_context_empty(self) -> None:
        self.assertEqual(format_context([]), "")

    def test_format_context_filters_low_score(self) -> None:
        hits = [{"text": "a", "metadata": {"agent": "hermes", "kind": "x"}, "score": 0.9}]
        out = format_context(hits)
        self.assertIn("hermes", out)

    @patch("rag.urllib.request.urlopen")
    def test_query_memory_fail_open(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = OSError("down")
        self.assertEqual(query_memory("test"), [])


class SwarmMeshTests(unittest.TestCase):
    def test_decompose_goal_splits_agents(self) -> None:
        subs = decompose_goal("analyze disk usage")
        agents = {s["agent"] for s in subs}
        self.assertEqual(agents, {"hermes", "openclaw"})

    def test_decompose_empty_goal(self) -> None:
        self.assertEqual(decompose_goal("  "), [])

    def test_build_delegate_message(self) -> None:
        msg = build_delegate_message(
            task_id="t1",
            session_id="s1",
            target="hermes",
            instruction="plan",
            rag_context="ctx",
            model="llama3",
        )
        self.assertEqual(msg["type"], "SWARM_DELEGATE")
        self.assertEqual(msg["target"], "hermes")

    def test_build_delegate_rejects_unknown_target(self) -> None:
        with self.assertRaises(ValueError):
            build_delegate_message(
                task_id="t1",
                session_id="s1",
                target="alien",
                instruction="x",
                rag_context="",
                model="llama3",
            )

    def test_parse_delegate_payload(self) -> None:
        msg = {
            "session_id": "s1",
            "payload": {"instruction": "do it", "model": "m1", "rag_context": "ctx"},
        }
        instruction, model, rag, sid = parse_delegate_payload(msg)
        self.assertEqual(instruction, "do it")
        self.assertEqual(model, "m1")
        self.assertEqual(rag, "ctx")
        self.assertEqual(sid, "s1")

    def test_build_peer_response(self) -> None:
        msg = build_peer_response(
            task_id="t1",
            session_id="s1",
            answer="done",
            from_agent="hermes",
        )
        self.assertEqual(msg["type"], "SWARM_PEER_RESPONSE")
        self.assertEqual(msg["payload"]["answer"], "done")

    def test_synthesize_results(self) -> None:
        task = SwarmTask(
            task_id="t1",
            goal="test goal",
            session_id="s1",
            results={"hermes": "plan", "openclaw": "exec"},
        )
        out = synthesize_results(task)
        self.assertIn("test goal", out)
        self.assertIn("plan", out)
        self.assertIn("exec", out)

    def test_new_session_id_prefix(self) -> None:
        self.assertTrue(new_session_id().startswith("swarm-"))

    def test_build_swarm_result_shape(self) -> None:
        task = SwarmTask(task_id="t1", goal="g", session_id="s1", results={"hermes": "x"})
        msg = build_swarm_result(task, "final")
        self.assertEqual(msg["type"], "SWARM_RESULT")
        self.assertEqual(msg["payload"]["synthesis"], "final")

    @patch("swarm_mesh.retrieve_context", return_value=("ctx", []))
    def test_enrich_with_rag_uses_provided_context(self, _mock) -> None:
        out = enrich_with_rag("task", "provided ctx")
        self.assertIn("task", out)

    @patch("swarm_mesh.retrieve_context", return_value=("fresh", []))
    def test_enrich_with_rag_fetches_when_missing(self, _mock) -> None:
        out = enrich_with_rag("task", "")
        self.assertIn("task", out)


if __name__ == "__main__":
    unittest.main()
