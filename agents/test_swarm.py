"""Swarm mesh peer contracts: payload hardening, RAG prompt shape, off-loop dispatch."""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from swarm import (
    build_round_prompt,
    build_synthesis_prompt,
    dispatch_mesh,
    dispatch_swarm_round,
    dispatch_swarm_synthesize,
    merge_context,
    normalize_mesh,
    normalize_round,
    trim_context,
)

ROUND = {
    "payload": {
        "session_id": "sess-1",
        "goal": "harden the gateway",
        "model": "llama3",
        "round": 2,
        "max_rounds": 2,
        "peers": ["hermes", "openclaw", "orchestrator"],
        "context": [
            {"id": "m1", "text": "token auth was added in v2.2", "agent": "hermes", "kind": "prompt_result", "score": 0.9},
        ],
        "contributions": {"openclaw": "rotate the token", "orchestrator": "all agents running"},
    }
}


class NormalizeTests(unittest.TestCase):
    def test_round_requires_session_and_goal(self) -> None:
        self.assertIsNone(normalize_round("garbage"))
        self.assertIsNone(normalize_round({"payload": {"goal": "x"}}))
        self.assertIsNone(normalize_round({"payload": {"session_id": "s", "goal": "   "}}))
        self.assertIsNone(normalize_round({"payload": ["not", "a", "dict"]}))

    def test_round_defaults_and_filters(self) -> None:
        rnd = normalize_round({
            "payload": {
                "session_id": " s1 ",
                "goal": "g",
                "round": "two",
                "peers": ["hermes", 7],
                "contributions": {"openclaw": "ok", "bad": 12, "blank": "  "},
                "context": "nope",
            }
        })
        assert rnd is not None
        self.assertEqual(rnd["session_id"], "s1")
        self.assertEqual(rnd["round"], 1)
        self.assertEqual(rnd["max_rounds"], 1)
        self.assertIsNone(rnd["model"])
        self.assertEqual(rnd["peers"], ["hermes"])
        self.assertEqual(rnd["contributions"], {"openclaw": "ok"})
        self.assertEqual(rnd["context"], [])

    def test_mesh_requires_sender_and_text(self) -> None:
        self.assertIsNone(normalize_mesh({"payload": {"text": "x"}}))
        self.assertIsNone(normalize_mesh({"from": "hermes", "payload": {"text": ""}}))
        m = normalize_mesh({"from": "hermes", "payload": {"intent": "ask", "text": " status? ", "session_id": "s"}})
        self.assertEqual(m, {"from": "hermes", "intent": "ask", "text": "status?", "session_id": "s"})
        self.assertEqual(normalize_mesh({"from": "openclaw", "payload": "raw"})["intent"], "tell")

    def test_trim_context_accepts_gateway_and_vault_shapes(self) -> None:
        hits = [
            {"id": "a", "text": "gateway shape", "agent": "hermes", "kind": "prompt_result", "score": 0.5},
            {"id": "b", "text": "vault shape", "metadata": {"agent": "openclaw", "kind": "task_result"}, "score": 0.7},
            {"id": "c", "text": "   "},
            "junk",
        ]
        out = trim_context(hits)
        self.assertEqual([h["id"] for h in out], ["a", "b"])
        self.assertEqual(out[1]["agent"], "openclaw")
        self.assertEqual(out[1]["kind"], "task_result")

    def test_merge_context_dedupes_and_ranks(self) -> None:
        gateway = [{"id": "a", "text": "A", "score": 0.4}]
        local = [{"id": "a", "text": "A", "score": 0.4}, {"id": "b", "text": "B", "score": 0.9}]
        merged = merge_context(gateway, local)
        self.assertEqual([h["id"] for h in merged], ["b", "a"])


class PromptTests(unittest.TestCase):
    def test_round_prompt_embeds_rag_and_peers(self) -> None:
        rnd = normalize_round(ROUND)
        assert rnd is not None
        prompt = build_round_prompt("hermes", rnd)
        self.assertIn("harden the gateway", prompt)
        self.assertIn("token auth was added in v2.2", prompt)
        self.assertIn("- openclaw: rotate the token", prompt)
        self.assertNotIn("- hermes:", prompt)
        self.assertIn("ROUND 2 OF 2", prompt)
        self.assertIn("Final round", prompt)

    def test_round_prompt_merges_local_retrieval(self) -> None:
        rnd = normalize_round(ROUND)
        assert rnd is not None
        prompt = build_round_prompt("openclaw", rnd, [{"id": "x", "text": "local vault hit", "score": 0.99}])
        self.assertIn("local vault hit", prompt)
        self.assertIn("do NOT execute", prompt)

    def test_synthesis_prompt_lists_transcript(self) -> None:
        rnd = normalize_round({
            "payload": {
                "session_id": "s",
                "goal": "g",
                "transcript": [{"round": 1, "agent": "openclaw", "text": "rotate"}, "junk"],
            }
        })
        assert rnd is not None
        prompt = build_synthesis_prompt(rnd)
        self.assertIn("[round 1 · openclaw] rotate", prompt)


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_contribution_is_sent_and_offloaded(self) -> None:
        ws = AsyncMock()
        progressed = threading.Event()

        def generate(prompt: str, model: str) -> str:
            progressed.set()
            return f"{model}:contribution"

        text = await dispatch_swarm_round(
            ws, ROUND, agent="openclaw", generate=generate,
            retrieve=lambda q, n: [{"id": "l", "text": "local", "score": 0.3}],
        )
        self.assertEqual(text, "llama3:contribution")
        self.assertTrue(progressed.is_set())
        sent = json.loads(ws.send.await_args.args[0])
        self.assertEqual(sent["type"], "SWARM_CONTRIBUTION")
        self.assertEqual(sent["agent"], "openclaw")
        self.assertEqual(sent["payload"]["session_id"], "sess-1")
        self.assertEqual(sent["payload"]["round"], 2)

    async def test_round_ignores_bad_payload(self) -> None:
        ws = AsyncMock()
        result = await dispatch_swarm_round(ws, {"payload": "bad"}, agent="hermes", generate=lambda p, m: "x")
        self.assertIsNone(result)
        ws.send.assert_not_awaited()

    async def test_round_survives_retrieval_failure(self) -> None:
        ws = AsyncMock()

        def broken(_q: str, _n: int) -> list:
            raise RuntimeError("vault down")

        text = await dispatch_swarm_round(ws, ROUND, agent="hermes", generate=lambda p, m: "ok", retrieve=broken)
        self.assertEqual(text, "ok")
        ws.send.assert_awaited()

    async def test_synthesis_is_sent(self) -> None:
        ws = AsyncMock()
        msg = {"payload": {"session_id": "s", "goal": "g", "transcript": [{"round": 1, "agent": "hermes", "text": "a"}]}}
        text = await dispatch_swarm_synthesize(ws, msg, agent="hermes", generate=lambda p, m: "merged")
        self.assertEqual(text, "merged")
        sent = json.loads(ws.send.await_args.args[0])
        self.assertEqual(sent["type"], "SWARM_SYNTHESIS")
        self.assertEqual(sent["payload"]["text"], "merged")

    async def test_mesh_ask_replies_with_tell(self) -> None:
        ws = AsyncMock()
        reply = await dispatch_mesh(
            ws, {"from": "hermes", "payload": {"intent": "ask", "text": "shell enabled?", "session_id": "s"}},
            agent="openclaw", generate=lambda p, m: "no",
        )
        self.assertEqual(reply, "no")
        sent = json.loads(ws.send.await_args.args[0])
        self.assertEqual(sent["type"], "MESH")
        self.assertEqual(sent["to"], "hermes")
        self.assertEqual(sent["payload"]["intent"], "tell")
        self.assertEqual(sent["payload"]["session_id"], "s")

    async def test_mesh_tell_is_remembered_not_answered(self) -> None:
        ws = AsyncMock()
        remembered: list[tuple] = []
        reply = await dispatch_mesh(
            ws, {"from": "openclaw", "payload": {"intent": "tell", "text": "fyi"}},
            agent="hermes", generate=lambda p, m: "should not run",
            remember=lambda *a: remembered.append(a) or True,
        )
        self.assertIsNone(reply)
        ws.send.assert_not_awaited()
        self.assertEqual(remembered[0][0], "hermes")
        self.assertEqual(remembered[0][1], "mesh_note")
        self.assertIn("fyi", remembered[0][2])

    async def test_dispatch_rejects_non_callables(self) -> None:
        with self.assertRaises(TypeError):
            await dispatch_swarm_round(AsyncMock(), ROUND, agent="hermes", generate="nope")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            await dispatch_mesh(AsyncMock(), {}, agent="hermes", generate=lambda p, m: "", remember="nope")  # type: ignore[arg-type]


class VaultRetrieveTests(unittest.TestCase):
    def test_retrieve_memory_rejects_blank_query(self) -> None:
        from vault_client import retrieve_memory

        self.assertEqual(retrieve_memory("   "), [])
        self.assertEqual(retrieve_memory(42), [])  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
