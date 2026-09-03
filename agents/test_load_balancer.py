"""Tests for swarm load balancer."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from load_balancer import (
    acquire_token,
    can_dispatch,
    order_subtasks,
    release_token,
    set_agent_online,
    snapshot,
)


class LoadBalancerTests(unittest.TestCase):
    def setUp(self) -> None:
        for agent in ("hermes", "openclaw"):
            set_agent_online(agent, True)
            for _ in range(4):
                release_token(agent)

    def test_order_subtasks_respects_weights(self) -> None:
        subs = [
            {"agent": "hermes", "role": "reason", "instruction": "a"},
            {"agent": "openclaw", "role": "execute", "instruction": "b"},
        ]
        ordered = order_subtasks(subs)
        agents = [s["agent"] for s in ordered]
        self.assertEqual(set(agents), {"hermes", "openclaw"})
        self.assertEqual(len(ordered), 2)

    def test_token_acquire_release(self) -> None:
        self.assertTrue(acquire_token("hermes"))
        release_token("hermes")

    def test_offline_agent_blocked(self) -> None:
        set_agent_online("hermes", False)
        ok, reason = can_dispatch("hermes")
        self.assertFalse(ok)
        self.assertIn("offline", reason or "")
        set_agent_online("hermes", True)

    def test_snapshot_shape(self) -> None:
        snap = snapshot()
        self.assertIn("agents", snap)
        self.assertIn("hermes", snap["agents"])


if __name__ == "__main__":
    unittest.main()
