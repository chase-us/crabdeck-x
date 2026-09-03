"""Orchestrator as a swarm mesh peer: deterministic digest + payload hardening."""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main
from main import Agent, Event, _swarm_round_payload, handle_mesh, handle_swarm_round, swarm_digest


def _agents(now: float) -> dict:
    return {
        "hermes": Agent(id="hermes", name="Hermes", status="running", last_heartbeat=now - 3),
        "openclaw": Agent(id="openclaw", name="OpenClaw", status="error", last_heartbeat=now - 40,
                          error_message="Heartbeat missed"),
        "crabdeck": Agent(id="crabdeck", name="Gateway", status="running", last_heartbeat=now),
    }


class DigestTests(unittest.TestCase):
    def test_digest_lists_agents_events_and_constraints(self) -> None:
        now = 1_700_000_120.0
        events = [
            Event(id="1", timestamp=now, type="SYSTEM", message="boot"),
            Event(id="2", timestamp=now, type="HEARTBEAT_MISSED", message="OpenClaw missed heartbeat", agent_id="openclaw"),
        ]
        text = swarm_digest(_agents(now), events, round_no=2, peers=["hermes", "openclaw"],
                            contributions={"hermes": "x"}, now=now)
        self.assertIn("round 2", text)
        self.assertIn(f"bhive slot {int(now // 60)}", text)
        self.assertIn("openclaw: error (Heartbeat missed)", text)
        self.assertIn("2/3 agents running", text)
        self.assertIn("Constraint: swarm peer(s) openclaw", text)
        self.assertIn("[HEARTBEAT_MISSED]", text)
        self.assertNotIn("[SYSTEM]", text)
        self.assertIn("Peers contributed last round: hermes", text)

    def test_digest_rejects_bad_types(self) -> None:
        with self.assertRaises(TypeError):
            swarm_digest("nope", [])  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            swarm_digest({}, "nope")  # type: ignore[arg-type]

    def test_round_payload_hardening(self) -> None:
        self.assertIsNone(_swarm_round_payload({"payload": "bad"}))
        self.assertIsNone(_swarm_round_payload({"payload": {"round": 1}}))
        rnd = _swarm_round_payload({"payload": {"session_id": " s ", "round": "x", "peers": ["hermes", 1],
                                                "contributions": {"hermes": "ok", "bad": 2}}})
        assert rnd is not None
        self.assertEqual(rnd, {"session_id": "s", "round": 1, "peers": ["hermes"], "contributions": {"hermes": "ok"}})


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        main.agents.clear()
        main.events.clear()
        main.agents.update(_agents(time.time()))

    async def test_round_sends_contribution(self) -> None:
        ws = AsyncMock()
        text = await handle_swarm_round(ws, {"payload": {"session_id": "sess", "round": 1, "peers": ["orchestrator"]}})
        assert text is not None
        sent = json.loads(ws.send.await_args.args[0])
        self.assertEqual(sent["type"], "SWARM_CONTRIBUTION")
        self.assertEqual(sent["agent"], "orchestrator")
        self.assertEqual(sent["payload"]["session_id"], "sess")
        self.assertIn("agents running", sent["payload"]["text"])

    async def test_round_ignores_bad_payload(self) -> None:
        ws = AsyncMock()
        self.assertIsNone(await handle_swarm_round(ws, {"payload": None}))
        ws.send.assert_not_awaited()

    async def test_mesh_ask_replies_tell(self) -> None:
        ws = AsyncMock()
        reply = await handle_mesh(ws, {"from": "hermes", "payload": {"intent": "ask", "text": "health?", "session_id": "s"}})
        assert reply is not None
        sent = json.loads(ws.send.await_args.args[0])
        self.assertEqual(sent["type"], "MESH")
        self.assertEqual(sent["to"], "hermes")
        self.assertEqual(sent["payload"]["intent"], "tell")
        self.assertEqual(sent["payload"]["session_id"], "s")

    async def test_mesh_tell_is_logged_only(self) -> None:
        ws = AsyncMock()
        self.assertIsNone(await handle_mesh(ws, {"from": "openclaw", "payload": {"text": "fyi"}}))
        ws.send.assert_not_awaited()
        self.assertTrue(any(e.type == "MESH" for e in main.events))


if __name__ == "__main__":
    unittest.main()
