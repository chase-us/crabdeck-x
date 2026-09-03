"""Swarm behaviours: bidding, decomposition, and consensus."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from swarm import (
    CAP_REASONING,
    CAP_RETRIEVAL,
    CAP_SYSTEM,
    bid_confidence,
    capability_ratio,
    consensus,
    decompose,
    jaccard,
    merge_results,
    normalize_capabilities,
    required_capabilities,
    should_bid,
    tokenize,
)


class CapabilityTests(unittest.TestCase):
    def test_normalizes_and_dedupes(self) -> None:
        self.assertEqual(normalize_capabilities([" Reasoning ", "REASONING", "llm"]), ["reasoning", "llm"])

    def test_rejects_hostile_values(self) -> None:
        self.assertEqual(normalize_capabilities(None), [])
        self.assertEqual(normalize_capabilities("reasoning"), [])
        self.assertEqual(normalize_capabilities([None, 5, "", "../x", "x" * 40]), [])

    def test_ratio_measures_coverage(self) -> None:
        self.assertEqual(capability_ratio(["reasoning", "llm"], ["reasoning"]), 1.0)
        self.assertEqual(capability_ratio(["reasoning"], ["reasoning", "system"]), 0.5)
        self.assertEqual(capability_ratio(["cooking"], ["system"]), 0.0)

    def test_open_call_matches_any_peer(self) -> None:
        self.assertEqual(capability_ratio(["anything"], []), 1.0)


class BidTests(unittest.TestCase):
    def test_full_coverage_beats_partial(self) -> None:
        full = bid_confidence(["reasoning", "system"], ["reasoning", "system"])
        half = bid_confidence(["reasoning"], ["reasoning", "system"])
        self.assertGreater(full, half)
        self.assertLessEqual(full, 1.0)

    def test_no_coverage_is_zero(self) -> None:
        self.assertEqual(bid_confidence(["cooking"], ["system"]), 0.0)

    def test_load_and_health_lower_confidence(self) -> None:
        base = bid_confidence(["reasoning"], ["reasoning"])
        self.assertLess(bid_confidence(["reasoning"], ["reasoning"], load=3), base)
        self.assertLess(bid_confidence(["reasoning"], ["reasoning"], healthy=False), base)

    def test_degraded_peer_does_not_outbid_healthy_peer(self) -> None:
        degraded = bid_confidence(["reasoning"], ["reasoning"], healthy=False)
        healthy = bid_confidence(["reasoning"], ["reasoning"], healthy=True)
        self.assertLess(degraded, healthy)

    def test_should_bid_declines_when_saturated_or_unqualified(self) -> None:
        self.assertTrue(should_bid(["reasoning"], ["reasoning"], load=0, max_load=4))
        self.assertFalse(should_bid(["reasoning"], ["reasoning"], load=4, max_load=4))
        self.assertFalse(should_bid(["cooking"], ["system"]))


class RoutingTests(unittest.TestCase):
    def test_system_wording_routes_to_system(self) -> None:
        self.assertEqual(required_capabilities("check disk usage on the host"), [CAP_SYSTEM])
        self.assertEqual(required_capabilities("list running processes"), [CAP_SYSTEM])

    def test_recall_wording_routes_to_retrieval(self) -> None:
        self.assertEqual(required_capabilities("what did hermes say earlier"), [CAP_RETRIEVAL])
        self.assertEqual(required_capabilities("recall the vault history"), [CAP_RETRIEVAL])

    def test_default_is_reasoning(self) -> None:
        self.assertEqual(required_capabilities("explain the contract net protocol"), [CAP_REASONING])
        self.assertEqual(required_capabilities(""), [CAP_REASONING])


class DecomposeTests(unittest.TestCase):
    def test_single_task_stays_whole(self) -> None:
        parts = decompose("explain the mesh protocol")
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["capabilities"], [CAP_REASONING])

    def test_conjunction_splits_and_tags_each_part(self) -> None:
        parts = decompose("check disk usage and then explain what it means")
        caps = [p["capabilities"][0] for p in parts]
        self.assertIn(CAP_SYSTEM, caps)
        self.assertIn(CAP_REASONING, caps)

    def test_multi_part_task_gains_a_retrieval_leg(self) -> None:
        """A split task is worth grounding, so retrieval joins the fan-out."""
        parts = decompose("check disk usage; explain the result")
        self.assertTrue(any(CAP_RETRIEVAL in p["capabilities"] for p in parts))

    def test_respects_max_subtasks(self) -> None:
        task = "; ".join(f"step {i}" for i in range(20))
        self.assertLessEqual(len(decompose(task, max_subtasks=3)), 3)

    def test_rejects_bad_input(self) -> None:
        with self.assertRaises(ValueError):
            decompose("   ")
        with self.assertRaises(ValueError):
            decompose("task", max_subtasks=0)


class ConsensusTests(unittest.TestCase):
    def test_agreeing_peers_form_the_winning_cluster(self) -> None:
        agreed = consensus([
            {"peer": "hermes", "result": "the gateway watchdog trips after 20 seconds", "confidence": 0.7},
            {"peer": "scribe", "result": "watchdog trips after 20 seconds of silence", "confidence": 0.8},
            {"peer": "openclaw", "result": "disk usage is 41 percent", "confidence": 0.9},
        ])
        self.assertEqual(agreed["votes"], 2)
        self.assertTrue(agreed["confident"])
        self.assertEqual(sorted(agreed["peers"]), ["hermes", "scribe"])

    def test_all_disagreeing_is_not_confident(self) -> None:
        agreed = consensus([
            {"peer": "a", "result": "alpha alpha"},
            {"peer": "b", "result": "bravo bravo"},
            {"peer": "c", "result": "charlie charlie"},
        ])
        self.assertEqual(agreed["votes"], 1)
        self.assertFalse(agreed["confident"])

    def test_single_answer_is_never_consensus(self) -> None:
        agreed = consensus([{"peer": "a", "result": "only one", "confidence": 1.0}])
        self.assertEqual(agreed["agreement"], 1.0)
        self.assertFalse(agreed["confident"])

    def test_most_confident_member_speaks(self) -> None:
        agreed = consensus([
            {"peer": "a", "result": "mesh awarded task one to openclaw", "confidence": 0.3},
            {"peer": "b", "result": "mesh awarded task one to the openclaw peer", "confidence": 0.9},
        ])
        self.assertEqual(agreed["answer"], "mesh awarded task one to the openclaw peer")

    def test_ignores_malformed_entries(self) -> None:
        agreed = consensus([None, {"peer": "a"}, {"result": "  "}, 7])  # type: ignore[list-item]
        self.assertEqual(agreed["votes"], 0)
        self.assertIsNone(agreed["answer"])

    def test_matches_gateway_thresholds(self) -> None:
        """Python and Node consensus must not disagree about agreement."""
        from pathlib import Path

        mesh_js = (Path(__file__).resolve().parents[1] / "gateway" / "mesh.js").read_text()
        self.assertIn("threshold = 0.45", mesh_js)
        self.assertIn("minVotes = 2", mesh_js)

    def test_rejects_bad_threshold(self) -> None:
        with self.assertRaises(ValueError):
            consensus([], threshold=0)


class MergeTests(unittest.TestCase):
    def test_attributes_each_contribution(self) -> None:
        merged = merge_results([
            {"peer": "scribe", "result": "two passages found", "confidence": 0.8},
            {"peer": "hermes", "result": "therefore the answer is 20s", "confidence": 0.6},
        ])
        self.assertIn("[scribe · 0.80]", merged)
        self.assertIn("[hermes · 0.60]", merged)

    def test_skips_empty_contributions(self) -> None:
        self.assertEqual(merge_results([{"peer": "a", "result": " "}, None]), "")


class TokenTests(unittest.TestCase):
    def test_jaccard_bounds(self) -> None:
        self.assertEqual(jaccard(tokenize("mesh peer"), tokenize("mesh peer")), 1.0)
        self.assertEqual(jaccard(tokenize("mesh"), tokenize("garlic")), 0.0)
        self.assertEqual(jaccard(set(), tokenize("mesh")), 0.0)


if __name__ == "__main__":
    unittest.main()
