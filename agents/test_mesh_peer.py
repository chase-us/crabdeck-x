"""MeshPeer protocol handling, plus the RAG client's fail-open contract."""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rag
from mesh import MeshPeer


def sent_frames(ws: AsyncMock) -> list[dict]:
    return [json.loads(call.args[0]) for call in ws.send.await_args_list]


class HelloTests(unittest.TestCase):
    def test_hello_advertises_capabilities(self) -> None:
        peer = MeshPeer("hermes", ["Reasoning", "llm"])
        hello = peer.hello_payload("tok")
        self.assertEqual(hello["client"], "hermes")
        self.assertEqual(hello["capabilities"], ["reasoning", "llm"])
        self.assertEqual(hello["token"], "tok")

    def test_hello_omits_absent_token(self) -> None:
        self.assertNotIn("token", MeshPeer("hermes", []).hello_payload(None))

    def test_rejects_bad_construction(self) -> None:
        with self.assertRaises(ValueError):
            MeshPeer("  ", [])
        with self.assertRaises(TypeError):
            MeshPeer("hermes", [], healthy="yes")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            MeshPeer("hermes", [], max_load=0)


class BidTests(unittest.TestCase):
    def test_bids_on_a_matching_cfp(self) -> None:
        peer = MeshPeer("scribe", ["retrieval", "memory"])
        bid = peer.bid_for({"taskId": "t1", "capabilities": ["retrieval"]})
        self.assertEqual(bid["taskId"], "t1")
        self.assertGreater(bid["confidence"], 0.5)

    def test_declines_a_cfp_it_cannot_serve(self) -> None:
        peer = MeshPeer("scribe", ["retrieval"])
        self.assertIsNone(peer.bid_for({"taskId": "t1", "capabilities": ["system"]}))

    def test_declines_when_saturated(self) -> None:
        peer = MeshPeer("scribe", ["retrieval"], max_load=1)
        peer.load = 1
        self.assertIsNone(peer.bid_for({"taskId": "t1", "capabilities": ["retrieval"]}))

    def test_ignores_a_cfp_without_a_task_id(self) -> None:
        peer = MeshPeer("scribe", ["retrieval"])
        self.assertIsNone(peer.bid_for({"capabilities": ["retrieval"]}))
        self.assertIsNone(peer.bid_for("not-a-dict"))

    def test_unhealthy_backend_lowers_the_bid_and_says_so(self) -> None:
        healthy = MeshPeer("hermes", ["reasoning"], healthy=lambda: True)
        broken = MeshPeer("hermes", ["reasoning"], healthy=lambda: False)
        cfp = {"taskId": "t", "capabilities": ["reasoning"]}
        self.assertGreater(healthy.bid_for(cfp)["confidence"], broken.bid_for(cfp)["confidence"])
        self.assertEqual(broken.bid_for(cfp)["note"], "degraded backend")

    def test_a_throwing_health_probe_counts_as_unhealthy(self) -> None:
        def explode() -> bool:
            raise RuntimeError("socket closed")

        peer = MeshPeer("hermes", ["reasoning"], healthy=explode)
        self.assertFalse(peer.is_healthy())


class HandleTests(unittest.IsolatedAsyncioTestCase):
    async def test_ignores_non_mesh_frames(self) -> None:
        peer = MeshPeer("hermes", ["reasoning"])
        ws = AsyncMock()
        self.assertFalse(await peer.handle(ws, {"type": "PROMPT"}))
        self.assertFalse(await peer.handle(ws, "garbage"))
        self.assertFalse(await peer.handle(ws, None))
        ws.send.assert_not_awaited()

    async def test_cfp_produces_a_bid_and_reserves_capacity(self) -> None:
        peer = MeshPeer("scribe", ["retrieval"])
        ws = AsyncMock()
        await peer.handle(ws, {"type": "MESH_CFP", "payload": {"taskId": "t1", "capabilities": ["retrieval"]}})
        frames = sent_frames(ws)
        self.assertEqual(frames[0]["type"], "MESH_BID")
        self.assertEqual(peer.load, 1, "a bid must reserve capacity so the next CFP sees real depth")

    async def test_losing_the_award_releases_capacity(self) -> None:
        peer = MeshPeer("scribe", ["retrieval"])
        ws = AsyncMock()
        await peer.handle(ws, {"type": "MESH_CFP", "payload": {"taskId": "t1", "capabilities": ["retrieval"]}})
        await peer.handle(ws, {"type": "MESH_CFP_CLOSED", "payload": {"taskId": "t1", "winner": "hermes"}})
        self.assertEqual(peer.load, 0)

    async def test_award_runs_the_handler_and_submits_the_result(self) -> None:
        peer = MeshPeer("scribe", ["retrieval"])
        peer.on_award(lambda task, caps, task_id: {"result": f"answered {task}", "confidence": 0.9})
        ws = AsyncMock()
        await peer.handle(ws, {"type": "MESH_AWARD", "payload": {"taskId": "t1", "task": "recall the watchdog"}})
        frame = sent_frames(ws)[-1]
        self.assertEqual(frame["type"], "MESH_RESULT")
        self.assertEqual(frame["payload"]["result"], "answered recall the watchdog")
        self.assertEqual(frame["payload"]["confidence"], 0.9)
        self.assertEqual(peer.load, 0, "capacity is released once the award is answered")

    async def test_award_accepts_a_plain_string_handler(self) -> None:
        peer = MeshPeer("hermes", ["reasoning"])
        peer.on_award(lambda task, caps, task_id: "plain answer")
        ws = AsyncMock()
        await peer.handle(ws, {"type": "MESH_AWARD", "payload": {"taskId": "t", "task": "x"}})
        self.assertEqual(sent_frames(ws)[-1]["payload"]["result"], "plain answer")

    async def test_a_crashing_award_handler_reports_failure(self) -> None:
        """Silence would strand the contract; the mesh needs the failure."""
        peer = MeshPeer("hermes", ["reasoning"])

        def boom(task, caps, task_id):
            raise RuntimeError("ollama exploded")

        peer.on_award(boom)
        ws = AsyncMock()
        await peer.handle(ws, {"type": "MESH_AWARD", "payload": {"taskId": "t", "task": "x"}})
        payload = sent_frames(ws)[-1]["payload"]
        self.assertFalse(payload["ok"])
        self.assertIn("ollama exploded", payload["result"])
        self.assertEqual(peer.load, 0)

    async def test_award_without_a_handler_fails_loudly(self) -> None:
        peer = MeshPeer("hermes", ["reasoning"])
        ws = AsyncMock()
        await peer.handle(ws, {"type": "MESH_AWARD", "payload": {"taskId": "t", "task": "x"}})
        payload = sent_frames(ws)[-1]["payload"]
        self.assertFalse(payload["ok"])
        self.assertIn("no award handler", payload["result"])

    async def test_async_handlers_are_awaited(self) -> None:
        peer = MeshPeer("hermes", ["reasoning"])

        async def handler(task, caps, task_id):
            return "async answer"

        peer.on_award(handler)
        ws = AsyncMock()
        await peer.handle(ws, {"type": "MESH_AWARD", "payload": {"taskId": "t", "task": "x"}})
        self.assertEqual(sent_frames(ws)[-1]["payload"]["result"], "async answer")

    async def test_blocking_handler_is_offloaded(self) -> None:
        """A sync handler must run off the loop, or heartbeats starve."""
        released = threading.Event()

        def blocking(task, caps, task_id):
            return "generated" if released.wait(timeout=2.0) else "timeout"

        peer = MeshPeer("hermes", ["reasoning"])
        peer.on_award(blocking)
        ws = AsyncMock()

        async def releaser() -> None:
            released.set()

        import asyncio
        handle = asyncio.create_task(
            peer.handle(ws, {"type": "MESH_AWARD", "payload": {"taskId": "t", "task": "x"}})
        )
        await asyncio.sleep(0)
        await releaser()
        await handle
        self.assertEqual(sent_frames(ws)[-1]["payload"]["result"], "generated")

    async def test_direct_message_is_answered_to_the_sender(self) -> None:
        peer = MeshPeer("scribe", ["retrieval"])
        peer.on_message(lambda intent, body, sender: {"context": f"{intent}:{body['query']}"})
        ws = AsyncMock()
        await peer.handle(ws, {
            "type": "MESH_MESSAGE",
            "payload": {"from": "hermes", "intent": "retrieve", "replyTo": "t9", "body": {"query": "watchdog"}},
        })
        frame = sent_frames(ws)[-1]
        self.assertEqual(frame["type"], "MESH_DIRECT")
        self.assertEqual(frame["payload"]["to"], "hermes")
        self.assertEqual(frame["payload"]["replyTo"], "t9")
        self.assertEqual(frame["payload"]["body"]["context"], "retrieve:watchdog")

    async def test_a_handler_returning_none_sends_nothing(self) -> None:
        peer = MeshPeer("hermes", ["reasoning"])
        peer.on_message(lambda intent, body, sender: None)
        ws = AsyncMock()
        await peer.handle(ws, {"type": "MESH_MESSAGE", "payload": {"from": "scribe", "body": {}}})
        ws.send.assert_not_awaited()

    async def test_mesh_state_updates_the_peer_view(self) -> None:
        peer = MeshPeer("hermes", ["reasoning"])
        ws = AsyncMock()
        await peer.handle(ws, {"type": "MESH_STATE", "payload": {"peers": [{"name": "scribe"}]}})
        self.assertEqual(peer.peers[0]["name"], "scribe")

    async def test_bad_mesh_state_payload_is_survivable(self) -> None:
        peer = MeshPeer("hermes", ["reasoning"])
        ws = AsyncMock()
        self.assertTrue(await peer.handle(ws, {"type": "MESH_STATE", "payload": "nope"}))
        self.assertEqual(peer.peers, [])

    async def test_a_throwing_gossip_handler_never_kills_the_loop(self) -> None:
        peer = MeshPeer("scribe", ["retrieval"])

        def boom(topic, body):
            raise RuntimeError("disk full")

        peer.on_gossip(boom)
        ws = AsyncMock()
        self.assertTrue(await peer.handle(ws, {"type": "MESH_GOSSIP", "payload": {"topic": "x", "body": {}}}))

    async def test_consensus_and_result_handlers_fire(self) -> None:
        peer = MeshPeer("hermes", ["reasoning"])
        seen: list[str] = []
        peer.on_consensus(lambda payload: seen.append(f"consensus:{payload.get('taskId')}"))
        peer.on_result(lambda payload: seen.append(f"result:{payload.get('peer')}"))
        ws = AsyncMock()
        await peer.handle(ws, {"type": "MESH_CONSENSUS", "payload": {"taskId": "t1"}})
        await peer.handle(ws, {"type": "MESH_RESULT", "payload": {"peer": "scribe"}})
        self.assertEqual(seen, ["consensus:t1", "result:scribe"])

    async def test_handlers_must_be_callable(self) -> None:
        peer = MeshPeer("hermes", ["reasoning"])
        for register in (peer.on_award, peer.on_message, peer.on_gossip, peer.on_consensus, peer.on_result):
            with self.assertRaises(TypeError):
                register("nope")  # type: ignore[arg-type]


class OutboundTests(unittest.IsolatedAsyncioTestCase):
    async def test_announce_emits_a_clamped_cfp(self) -> None:
        peer = MeshPeer("hermes", ["reasoning"])
        ws = AsyncMock()
        task_id = await peer.announce(ws, "x" * 9000, ["Reasoning"], quorum=2)
        frame = sent_frames(ws)[0]
        self.assertEqual(frame["type"], "MESH_ANNOUNCE")
        self.assertEqual(len(frame["payload"]["task"]), 4000)
        self.assertEqual(frame["payload"]["capabilities"], ["reasoning"])
        self.assertEqual(frame["payload"]["quorum"], 2)
        self.assertTrue(task_id.startswith("hermes-"))

    async def test_announce_rejects_an_empty_task(self) -> None:
        peer = MeshPeer("hermes", ["reasoning"])
        with self.assertRaises(ValueError):
            await peer.announce(AsyncMock(), "  ")

    async def test_ask_targets_a_named_peer(self) -> None:
        peer = MeshPeer("hermes", ["reasoning"])
        ws = AsyncMock()
        await peer.ask(ws, "Scribe", "retrieve", {"query": "watchdog"}, "t1")
        frame = sent_frames(ws)[0]
        self.assertEqual(frame["payload"]["to"], "scribe")
        self.assertEqual(frame["payload"]["intent"], "retrieve")

    async def test_ask_rejects_a_blank_target(self) -> None:
        with self.assertRaises(ValueError):
            await MeshPeer("hermes", []).ask(AsyncMock(), "  ", "x", {})

    async def test_gossip_clamps_ttl(self) -> None:
        peer = MeshPeer("hermes", ["reasoning"])
        ws = AsyncMock()
        await peer.gossip(ws, "ollama", {"status": "down"}, ttl=99)
        self.assertEqual(sent_frames(ws)[0]["payload"]["ttl"], 4)

    async def test_submit_clamps_the_result(self) -> None:
        peer = MeshPeer("hermes", ["reasoning"])
        ws = AsyncMock()
        await peer.submit(ws, "t1", "y" * 9000, confidence=0.7)
        payload = sent_frames(ws)[0]["payload"]
        self.assertEqual(len(payload["result"]), 8000)
        self.assertEqual(payload["confidence"], 0.7)


class RagClientTests(unittest.TestCase):
    def test_retrieve_is_fail_open_when_the_vault_is_down(self) -> None:
        original = rag._get
        rag._get = lambda *a, **k: None  # type: ignore[assignment]
        try:
            result = rag.retrieve("what did hermes say?")
        finally:
            rag._get = original  # type: ignore[assignment]
        self.assertTrue(result["degraded"])
        self.assertFalse(result["grounded"])
        self.assertIn("unavailable", result["prompt"])
        self.assertIn("Do not invent citations", result["prompt"])

    def test_retrieve_passes_through_a_grounded_answer(self) -> None:
        original = rag._get
        rag._get = lambda *a, **k: {  # type: ignore[assignment]
            "prompt": "SWARM MEMORY:\n[1] scribe/doc\nwatchdog is 20s\n\nQUESTION: x",
            "context": "[1] scribe/doc\nwatchdog is 20s",
            "citations": [{"n": 1, "agent": "scribe", "kind": "doc", "score": 0.8}],
            "hits": [{"id": "a"}],
            "grounded": True,
            "space": "hash-v1",
        }
        try:
            result = rag.retrieve("how long is the watchdog?")
        finally:
            rag._get = original  # type: ignore[assignment]
        self.assertTrue(result["grounded"])
        self.assertFalse(result["degraded"])
        self.assertEqual(result["space"], "hash-v1")

    def test_retrieve_survives_a_malformed_vault_response(self) -> None:
        original = rag._get
        rag._get = lambda *a, **k: {"unexpected": True}  # type: ignore[assignment]
        try:
            result = rag.retrieve("x")
        finally:
            rag._get = original  # type: ignore[assignment]
        self.assertTrue(result["degraded"])

    def test_retrieve_validates_arguments(self) -> None:
        with self.assertRaises(ValueError):
            rag.retrieve("  ")
        with self.assertRaises(ValueError):
            rag.retrieve("x", k=0)
        with self.assertRaises(ValueError):
            rag.retrieve("x", candidates=99)

    def test_ingest_rejects_junk_without_calling_out(self) -> None:
        called: list[str] = []
        original = rag._post
        rag._post = lambda *a, **k: called.append("posted") or {}  # type: ignore[assignment]
        try:
            self.assertIsNone(rag.ingest("", "doc", "text"))
            self.assertIsNone(rag.ingest("scribe", "", "text"))
            self.assertIsNone(rag.ingest("scribe", "doc", "   "))
        finally:
            rag._post = original  # type: ignore[assignment]
        self.assertEqual(called, [])

    def test_citation_line_summarizes_provenance(self) -> None:
        line = rag.citation_line([
            {"n": 1, "agent": "scribe", "kind": "doc", "score": 0.831},
            {"n": 2, "agent": "hermes", "kind": "mesh_answer", "score": 0.42},
        ])
        self.assertIn("[1] scribe/doc 0.83", line)
        self.assertIn("[2] hermes/mesh_answer 0.42", line)

    def test_citation_line_handles_junk(self) -> None:
        self.assertEqual(rag.citation_line([]), "")
        self.assertEqual(rag.citation_line("nope"), "")  # type: ignore[arg-type]

    def test_local_prompt_requires_a_question(self) -> None:
        with self.assertRaises(ValueError):
            rag.local_prompt("  ")


if __name__ == "__main__":
    unittest.main()
