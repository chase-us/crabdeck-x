from __future__ import annotations

import math
import tempfile
import time
import unittest
from pathlib import Path

import bhive
from bhive import evaluate_agent, minute_slot, missed_slot, missed_watchdog, validate_agent


class BhiveTests(unittest.TestCase):
    def test_minute_slot_epoch(self) -> None:
        self.assertEqual(minute_slot(0), 0)
        self.assertEqual(minute_slot(59.9), 0)
        self.assertEqual(minute_slot(60), 1)
        self.assertEqual(minute_slot(120), 2)

    def test_minute_slot_rejects_bad_input(self) -> None:
        with self.assertRaises(TypeError):
            minute_slot("now")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            minute_slot(-1)

    def test_missed_slot_allows_same_and_next(self) -> None:
        self.assertFalse(missed_slot(10, 10))
        self.assertFalse(missed_slot(10, 11))
        self.assertTrue(missed_slot(10, 12))

    def test_watchdog_20s(self) -> None:
        self.assertFalse(missed_watchdog(100.0, 120.0))
        self.assertFalse(missed_watchdog(100.0, 120.0 + 1e-9))
        self.assertTrue(missed_watchdog(100.0, 120.0001))

    def test_evaluate_running(self) -> None:
        now = 1_700_000_060.0
        st = evaluate_agent("hermes", last_seen=now - 5, last_slot=minute_slot(now), now_seconds=now)
        self.assertEqual(st.status, "running")
        self.assertFalse(st.watchdog_miss)
        self.assertFalse(st.slot_miss)

    def test_evaluate_missed_heartbeat(self) -> None:
        now = 1_000.0
        st = evaluate_agent("openclaw", last_seen=now - 21, last_slot=minute_slot(now), now_seconds=now)
        self.assertEqual(st.status, "missed_heartbeat")

    def test_unknown_agent(self) -> None:
        with self.assertRaises(ValueError):
            validate_agent("not-a-real-agent")
        with self.assertRaises(ValueError):
            validate_agent("")


class SqliteAndVectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        from sqlite_store import SqliteVault
        from vectors import SqliteVectorMemory

        self.vault = SqliteVault(root / "state.db")
        self.vec = SqliteVectorMemory(root / "vec.db")

    def tearDown(self) -> None:
        self.vault.close()
        self.vec.close()
        self._tmp.cleanup()

    def test_heartbeat_roundtrip(self) -> None:
        now = time.time()
        rec = self.vault.record_heartbeat("hermes", ts=now, source="test")
        self.assertEqual(rec["agent"], "hermes")
        self.assertEqual(rec["slot"], minute_slot(now))
        hb = self.vault.recent_heartbeats("hermes", limit=1)
        self.assertEqual(len(hb), 1)

    def test_heartbeat_rejects_skewed_slot(self) -> None:
        now = time.time()
        with self.assertRaises(ValueError):
            self.vault.record_heartbeat("hermes", ts=now, slot=minute_slot(now) + 5)

    def test_session_survives(self) -> None:
        self.vault.upsert_session("sess-1", {"loop": 3, "note": "micro-restart"})
        row = self.vault.get_session("sess-1")
        assert row is not None
        self.assertEqual(row["context"]["loop"], 3)

    def test_event_body_must_be_dict(self) -> None:
        with self.assertRaises(TypeError):
            self.vault.log_event("x", "nope")  # type: ignore[arg-type]

    def test_vector_query_ranks_similar_text(self) -> None:
        self.vec.add("a", "hermes generated a reply about ollama latency", {"k": "a"})
        self.vec.add("b", "unrelated cooking recipe with garlic", {"k": "b"})
        hits = self.vec.query("ollama latency hermes", n=2)
        self.assertEqual(hits[0]["id"], "a")
        self.assertGreater(hits[0]["score"], hits[1]["score"])

    def test_embed_normalized(self) -> None:
        from vectors import embed_text

        v = embed_text("ping")
        self.assertEqual(len(v), 384)
        norm = math.sqrt(sum(x * x for x in v))
        self.assertAlmostEqual(norm, 1.0, places=5)

    def test_bhive_constants_match_gateway(self) -> None:
        self.assertEqual(bhive.SLOT_SECONDS, 60)
        self.assertEqual(bhive.WATCHDOG_SECONDS, 20.0)


if __name__ == "__main__":
    unittest.main()
