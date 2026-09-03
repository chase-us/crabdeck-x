"""Retrieval must actually rank related text above unrelated text."""

from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path

from embeddings import (
    EMBED_DIM,
    Blake2Embedder,
    EmbeddingError,
    FallbackEmbedder,
    HashingEmbedder,
    blake2_embed,
    cosine,
    hashing_embed,
    open_embedder,
    tokenize,
)
from vectors import SqliteVectorMemory, embed_text


class TokenizerTests(unittest.TestCase):
    def test_lowercases_and_drops_function_words(self) -> None:
        self.assertEqual(tokenize("The Swarm is ON the Mesh"), ["swarm", "mesh"])

    def test_keeps_identifier_shapes(self) -> None:
        self.assertIn("gateway_token", tokenize("check GATEWAY_TOKEN now"))
        self.assertIn("v2.2", tokenize("crabdeck v2.2"))

    def test_rejects_non_string(self) -> None:
        with self.assertRaises(TypeError):
            tokenize(42)  # type: ignore[arg-type]


class HashingEmbedTests(unittest.TestCase):
    def test_normalized_384d(self) -> None:
        vec = hashing_embed("swarm mesh heartbeat")
        self.assertEqual(len(vec), EMBED_DIM)
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in vec)), 1.0, places=6)

    def test_deterministic(self) -> None:
        self.assertEqual(hashing_embed("mesh peer"), hashing_embed("mesh peer"))

    def test_paraphrase_beats_unrelated(self) -> None:
        """The property blake2-v1 lacked, and the reason RAG works at all."""
        query = hashing_embed("how does the swarm mesh route a task to a peer")
        related = hashing_embed("the mesh routes each task to the best peer in the swarm")
        unrelated = hashing_embed("garlic butter recipe for roast potatoes")
        self.assertGreater(cosine(query, related), cosine(query, unrelated))
        self.assertGreater(cosine(query, related), 0.2)

    def test_word_order_is_distinguishable(self) -> None:
        """Bigrams make phrase order matter without erasing bag-of-words recall."""
        a = hashing_embed("hermes asks scribe")
        b = hashing_embed("scribe asks hermes")
        self.assertLess(cosine(a, b), 0.999)
        self.assertGreater(cosine(a, b), 0.4)

    def test_rejects_untokenizable(self) -> None:
        with self.assertRaises(ValueError):
            hashing_embed("!!! ???")


class Blake2LegacyTests(unittest.TestCase):
    def test_still_normalized(self) -> None:
        vec = blake2_embed("ping")
        self.assertEqual(len(vec), EMBED_DIM)
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in vec)), 1.0, places=5)

    def test_is_not_semantic(self) -> None:
        """Documents why the default moved off blake2-v1."""
        query = blake2_embed("how does the swarm mesh route a task to a peer")
        related = blake2_embed("the mesh routes each task to the best peer in the swarm")
        self.assertLess(cosine(query, related), 0.2)


class EmbedTextDefaultTests(unittest.TestCase):
    def test_default_is_hashing_and_back_compatible(self) -> None:
        vec = embed_text("ping")
        self.assertEqual(len(vec), EMBED_DIM)
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in vec)), 1.0, places=6)


class FallbackEmbedderTests(unittest.TestCase):
    def test_falls_back_and_reports_space(self) -> None:
        class Broken:
            id = "ollama:down"
            dim = 768

            def embed(self, text: str) -> list[float]:
                raise EmbeddingError("connection refused")

        fallback = FallbackEmbedder(Broken(), HashingEmbedder())
        vec = fallback.embed("mesh peer online")
        self.assertEqual(len(vec), EMBED_DIM)
        # Space must reflect what actually produced the vector, so degraded
        # rows never get scored against real semantic ones.
        self.assertEqual(fallback.id, "hash-v1")
        self.assertTrue(fallback.degraded)

    def test_primary_wins_when_healthy(self) -> None:
        class Healthy:
            id = "ollama:test"
            dim = 4

            def embed(self, text: str) -> list[float]:
                return [0.5, 0.5, 0.5, 0.5]

        fallback = FallbackEmbedder(Healthy(), HashingEmbedder())
        self.assertEqual(len(fallback.embed("x")), 4)
        self.assertEqual(fallback.id, "ollama:test")
        self.assertFalse(fallback.degraded)


class OpenEmbedderTests(unittest.TestCase):
    def test_resolves_names(self) -> None:
        self.assertEqual(open_embedder("hash").id, "hash-v1")
        self.assertEqual(open_embedder("blake2").id, "blake2-v1")
        self.assertEqual(open_embedder("ollama", model="m").id, "ollama:m")

    def test_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            open_embedder("word2vec")


class VectorSpaceMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "vec.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ranks_paraphrase_of_stored_text(self) -> None:
        store = SqliteVectorMemory(self.path)
        store.add("mesh", "the swarm mesh awarded the task to openclaw", {"agent": "hermes"})
        store.add("food", "sourdough starter needs feeding twice a day", {"agent": "hermes"})
        hits = store.query("which peer did the mesh award the task to", n=2)
        self.assertEqual(hits[0]["id"], "mesh")
        store.close()

    def test_legacy_rows_are_reembedded_into_active_space(self) -> None:
        legacy = SqliteVectorMemory(self.path, embedder=Blake2Embedder())
        legacy.add("old", "hermes reasoned about ollama latency", {"agent": "hermes"})
        legacy.close()

        active = SqliteVectorMemory(self.path)
        hits = active.query("ollama latency reasoning", n=3)
        self.assertEqual([h["id"] for h in hits], ["old"])
        with sqlite3.connect(self.path) as conn:
            space = conn.execute("SELECT space FROM vectors WHERE id='old'").fetchone()[0]
        self.assertEqual(space, "hash-v1")
        active.close()

    def test_v22_schema_without_space_column_upgrades(self) -> None:
        """A real v2.2 vectors DB has no `space` column at all."""
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TABLE vectors (id TEXT PRIMARY KEY, text TEXT NOT NULL, "
                "metadata TEXT NOT NULL, embedding TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO vectors VALUES (?, ?, ?, ?)",
                ("legacy", "gateway watchdog tripped", "{}", json.dumps(blake2_embed("gateway watchdog tripped"))),
            )
            conn.commit()

        store = SqliteVectorMemory(self.path)
        hits = store.query("gateway watchdog tripped", n=1)
        self.assertEqual(hits[0]["id"], "legacy")
        store.close()

    def test_unindexable_row_is_parked_not_retried(self) -> None:
        legacy = SqliteVectorMemory(self.path, embedder=Blake2Embedder())
        legacy.add("punct", "!!! ???", {})
        legacy.add("good", "swarm mesh consensus reached", {})
        legacy.close()

        store = SqliteVectorMemory(self.path)
        hits = store.query("swarm mesh consensus", n=5)
        self.assertEqual([h["id"] for h in hits], ["good"])
        with sqlite3.connect(self.path) as conn:
            space = conn.execute("SELECT space FROM vectors WHERE id='punct'").fetchone()[0]
        self.assertEqual(space, "hash-v1:unindexable")
        store.close()

    def test_query_bounds_are_enforced(self) -> None:
        store = SqliteVectorMemory(self.path)
        store.add("a", "swarm mesh", {})
        with self.assertRaises(ValueError):
            store.query("swarm", n=0)
        with self.assertRaises(ValueError):
            store.query("swarm", n=51)
        store.close()


if __name__ == "__main__":
    unittest.main()
