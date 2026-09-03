"""Hermes and Scribe must ground their answers, and degrade honestly."""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hermes_agent
import scribe_agent


def grounded_stub(question: str) -> dict:
    return {
        "query": question,
        "prompt": f"SWARM MEMORY:\n[1] scribe/doc\nthe watchdog trips at 20s\n\nQUESTION: {question}",
        "context": "[1] scribe/doc\nthe watchdog trips at 20s",
        "citations": [{"n": 1, "agent": "scribe", "kind": "doc", "score": 0.77}],
        "hits": [{"id": "doc#0"}],
        "grounded": True,
        "degraded": False,
    }


def degraded_stub(question: str) -> dict:
    return {
        "query": question,
        "prompt": f"SWARM MEMORY: (unavailable)\n\nQUESTION: {question}",
        "context": "",
        "citations": [],
        "hits": [],
        "grounded": False,
        "degraded": True,
    }


class HermesGroundingTests(unittest.TestCase):
    def test_the_model_sees_retrieved_context(self) -> None:
        seen: list[str] = []
        hermes_agent.grounded_reply(
            "how long is the watchdog?",
            generate=lambda prompt, model: seen.append(prompt) or "20 seconds [1]",
            fetch=grounded_stub,
        )
        self.assertIn("SWARM MEMORY", seen[0])
        self.assertIn("the watchdog trips at 20s", seen[0])

    def test_a_grounded_reply_carries_its_sources(self) -> None:
        out = hermes_agent.grounded_reply(
            "how long is the watchdog?",
            generate=lambda prompt, model: "20 seconds [1]",
            fetch=grounded_stub,
        )
        self.assertIn("sources: [1] scribe/doc 0.77", out["result"])
        self.assertEqual(out["confidence"], 0.8)
        self.assertTrue(out["ok"])

    def test_an_ungrounded_reply_claims_no_sources(self) -> None:
        out = hermes_agent.grounded_reply(
            "unknown thing",
            generate=lambda prompt, model: "I do not know",
            fetch=degraded_stub,
        )
        self.assertNotIn("sources:", out["result"])
        self.assertEqual(out["confidence"], 0.5)


class HermesAwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_award_is_answered_and_remembered(self) -> None:
        remembered: list[tuple] = []
        original_retrieve = hermes_agent.retrieve
        original_generate = hermes_agent.ollama_generate
        original_memory = hermes_agent.emit_memory
        hermes_agent.retrieve = grounded_stub
        hermes_agent.ollama_generate = lambda prompt, model=None: "watchdog is 20s"
        hermes_agent.emit_memory = lambda *args: remembered.append(args) or True
        try:
            outcome = hermes_agent.handle_award("how long is the watchdog?", ["reasoning"], "task-9")
        finally:
            hermes_agent.retrieve = original_retrieve
            hermes_agent.ollama_generate = original_generate
            hermes_agent.emit_memory = original_memory

        self.assertIn("watchdog is 20s", outcome["result"])
        self.assertEqual(remembered[0][0], "hermes")
        self.assertEqual(remembered[0][1], "mesh_answer")

    async def test_dispatch_prompt_grounds_when_given_a_retriever(self) -> None:
        ws = AsyncMock()
        reply = await hermes_agent.dispatch_prompt(
            ws,
            {"payload": {"prompt": "how long is the watchdog?", "model": "llama3"}},
            generate=lambda prompt, model: "20 seconds",
            remember=None,
            ground=grounded_stub,
        )
        self.assertIn("sources: [1] scribe/doc", reply)
        frame = json.loads(ws.send.await_args.args[0])
        self.assertEqual(frame["type"], "HERMES_RESPONSE")

    async def test_dispatch_prompt_stays_ungrounded_without_a_retriever(self) -> None:
        """The default path must not silently start calling the vault."""
        ws = AsyncMock()
        reply = await hermes_agent.dispatch_prompt(
            ws,
            {"payload": {"prompt": "ping", "model": "llama3"}},
            generate=lambda prompt, model: f"{model}:{prompt}",
            remember=None,
        )
        self.assertEqual(reply, "llama3:ping")

    async def test_dispatch_prompt_survives_a_broken_retriever(self) -> None:
        ws = AsyncMock()
        reply = await hermes_agent.dispatch_prompt(
            ws,
            {"payload": {"prompt": "ping", "model": "llama3"}},
            generate=lambda prompt, model: f"answered:{prompt}",
            remember=None,
            ground=lambda q: "not-a-dict",
        )
        self.assertEqual(reply, "answered:ping")

    async def test_dispatch_prompt_rejects_a_non_callable_retriever(self) -> None:
        with self.assertRaises(TypeError):
            await hermes_agent.dispatch_prompt(
                AsyncMock(), {"payload": {"prompt": "x"}}, ground="nope"
            )


class ScribeTests(unittest.TestCase):
    def test_the_digest_returns_passages_verbatim(self) -> None:
        """With no model available, the passages themselves are the answer."""
        digest = scribe_agent.digest("watchdog?", grounded_stub("watchdog?"))
        self.assertIn("the watchdog trips at 20s", digest)
        self.assertIn("sources: [1] scribe/doc", digest)

    def test_the_digest_names_the_reason_for_an_empty_answer(self) -> None:
        self.assertIn("vault unreachable", scribe_agent.digest("x", degraded_stub("x")))
        empty = {"grounded": False, "degraded": False, "citations": [], "context": ""}
        self.assertIn("no matching passages", scribe_agent.digest("x", empty))

    def test_answer_synthesizes_when_a_model_is_supplied(self) -> None:
        out = scribe_agent.answer(
            "how long is the watchdog?",
            generate=lambda prompt, model: "20 seconds [1]",
            fetch=grounded_stub,
        )
        self.assertEqual(out["confidence"], 0.85)
        self.assertIn("sources:", out["result"])
        self.assertEqual(out["citations"][0]["agent"], "scribe")

    def test_a_degraded_retrieval_lowers_confidence(self) -> None:
        out = scribe_agent.answer(
            "x", generate=lambda prompt, model: "guess", fetch=degraded_stub
        )
        self.assertEqual(out["confidence"], 0.45)

    def test_an_empty_question_is_refused(self) -> None:
        out = scribe_agent.answer("   ")
        self.assertFalse(out["ok"])
        self.assertEqual(out["confidence"], 0.0)

    def test_a_direct_retrieve_returns_context_not_prose(self) -> None:
        original = scribe_agent.retrieve
        scribe_agent.retrieve = grounded_stub
        try:
            reply = scribe_agent.handle_message("retrieve", {"query": "watchdog"}, "hermes")
        finally:
            scribe_agent.retrieve = original
        self.assertIn("the watchdog trips at 20s", reply["context"])
        self.assertTrue(reply["grounded"])
        self.assertNotIn("result", reply)

    def test_a_direct_request_without_a_query_errors(self) -> None:
        self.assertIn("error", scribe_agent.handle_message("retrieve", {}, "hermes"))
        self.assertIn("error", scribe_agent.handle_message("retrieve", None, "hermes"))

    def test_an_unsupported_intent_is_reported(self) -> None:
        reply = scribe_agent.handle_message("delete_everything", {"query": "x"}, "hermes")
        self.assertIn("unsupported intent", reply["error"])

    def test_gossip_is_ingested_for_later_recall(self) -> None:
        captured: list[tuple] = []
        original = scribe_agent.ingest
        scribe_agent.ingest = lambda *args, **kwargs: captured.append((args, kwargs)) or {}
        try:
            scribe_agent.handle_gossip("ollama", {"status": "degraded"})
            scribe_agent.handle_gossip("", {"ignored": True})
            scribe_agent.handle_gossip("empty", "")
        finally:
            scribe_agent.ingest = original
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0][0], "scribe")
        self.assertIn("degraded", captured[0][0][2])


class CapabilityWiringTests(unittest.TestCase):
    def test_each_peer_owns_a_distinct_role(self) -> None:
        """Overlapping capabilities would make contract-net allocation moot."""
        import openclaw_agent

        self.assertEqual(hermes_agent.peer.name, "hermes")
        self.assertEqual(scribe_agent.peer.name, "scribe")
        self.assertEqual(openclaw_agent.peer.name, "openclaw")
        self.assertIn("reasoning", hermes_agent.CAPABILITIES)
        self.assertIn("retrieval", scribe_agent.CAPABILITIES)
        self.assertIn("system", openclaw_agent.CAPABILITIES)
        self.assertNotIn("system", hermes_agent.CAPABILITIES)
        self.assertNotIn("reasoning", openclaw_agent.CAPABILITIES)

    def test_shell_exec_stays_off_by_default(self) -> None:
        import openclaw_agent

        self.assertFalse(openclaw_agent.ENABLE_SHELL_EXEC)


if __name__ == "__main__":
    unittest.main()
