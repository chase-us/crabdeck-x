"""RAG pipeline: chunking, dedupe, MMR, citation budget, grounded prompts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import create_app
from rag import (
    assemble,
    augment_prompt,
    build_context,
    chunk_text,
    dedupe_hits,
    mmr_select,
    token_overlap,
)
from sqlite_store import SqliteVault
from vectors import SqliteVectorMemory


def hit(doc_id: str, text: str, score: float, agent: str = "hermes", kind: str = "memory") -> dict:
    return {"id": doc_id, "text": text, "score": score, "metadata": {"agent": agent, "kind": kind}}


class ChunkTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self) -> None:
        self.assertEqual(chunk_text("swarm mesh online"), ["swarm mesh online"])

    def test_empty_text_yields_nothing(self) -> None:
        self.assertEqual(chunk_text("   \n  "), [])

    def test_splits_on_paragraphs_and_respects_size(self) -> None:
        doc = "\n\n".join(f"paragraph {i} " + ("mesh " * 40) for i in range(6))
        chunks = chunk_text(doc, size=300, overlap=40)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 400)  # size + overlap carry

    def test_overlap_carries_context_across_the_seam(self) -> None:
        doc = "alpha " * 60 + "\n\n" + "bravo " * 60
        chunks = chunk_text(doc, size=200, overlap=60)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertIn("alpha", chunks[1])

    def test_unbroken_text_is_hard_split(self) -> None:
        chunks = chunk_text("x" * 900, size=200, overlap=0)
        self.assertGreater(len(chunks), 1)

    def test_chunk_count_is_bounded(self) -> None:
        doc = "\n\n".join(f"passage {i} mesh peer" for i in range(500))
        self.assertLessEqual(len(chunk_text(doc, size=100, overlap=0, max_chunks=8)), 8)

    def test_rejects_bad_bounds(self) -> None:
        with self.assertRaises(ValueError):
            chunk_text("abc", size=10)
        with self.assertRaises(ValueError):
            chunk_text("abc", size=200, overlap=200)
        with self.assertRaises(TypeError):
            chunk_text(None)  # type: ignore[arg-type]


class OverlapTests(unittest.TestCase):
    def test_identical_text_is_one(self) -> None:
        self.assertAlmostEqual(token_overlap("mesh peer", "mesh peer"), 1.0)

    def test_disjoint_text_is_zero(self) -> None:
        self.assertEqual(token_overlap("mesh peer", "garlic butter"), 0.0)

    def test_empty_side_is_zero(self) -> None:
        self.assertEqual(token_overlap("", "mesh"), 0.0)


class DedupeTests(unittest.TestCase):
    def test_drops_near_duplicates_keeping_best_score(self) -> None:
        kept = dedupe_hits([
            hit("a", "the mesh awarded the task to openclaw", 0.6),
            hit("b", "the mesh awarded the task to openclaw", 0.9),
            hit("c", "scribe retrieved four passages", 0.5),
        ])
        self.assertEqual([h["id"] for h in kept], ["b", "c"])

    def test_skips_malformed_hits(self) -> None:
        kept = dedupe_hits([None, {"text": ""}, {"nope": 1}, hit("ok", "mesh peer", 0.4)])
        self.assertEqual([h["id"] for h in kept], ["ok"])

    def test_rejects_bad_threshold(self) -> None:
        with self.assertRaises(ValueError):
            dedupe_hits([], threshold=0.0)


class MmrTests(unittest.TestCase):
    def test_prefers_coverage_over_three_paraphrases(self) -> None:
        """Top-k would return three restatements; MMR spends slot 2 on new ground."""
        pool = [
            hit("p1", "hermes generated a reply using the llama3 model", 0.90),
            hit("p2", "hermes generated a reply with llama3 model output", 0.89),
            hit("p3", "hermes generated llama3 model replies", 0.88),
            hit("d1", "openclaw inspected disk usage on the host", 0.80),
        ]
        self.assertEqual([h["id"] for h in mmr_select(pool, k=2)], ["p1", "d1"])
        # Same pool, redundancy penalty switched off ⇒ plain top-k.
        self.assertEqual([h["id"] for h in mmr_select(pool, k=2, lambda_=1.0)], ["p1", "p2"])

    def test_redundancy_penalty_scales_with_lambda(self) -> None:
        """A weaker but novel passage overtakes a paraphrase as lambda drops."""
        pool = [
            hit("p1", "hermes generated a reply using the llama3 model", 0.90),
            hit("p2", "hermes generated a reply with llama3 model output", 0.89),
            hit("d1", "openclaw inspected disk usage on the host", 0.55),
        ]
        self.assertEqual(mmr_select(pool, k=2, lambda_=0.9)[1]["id"], "p2")
        self.assertEqual(mmr_select(pool, k=2, lambda_=0.5)[1]["id"], "d1")

    def test_handles_short_pools(self) -> None:
        self.assertEqual(mmr_select([], k=3), [])
        self.assertEqual(len(mmr_select([hit("a", "mesh", 0.1)], k=3)), 1)

    def test_rejects_bad_args(self) -> None:
        with self.assertRaises(ValueError):
            mmr_select([], k=0)
        with self.assertRaises(ValueError):
            mmr_select([], lambda_=1.5)


class ContextTests(unittest.TestCase):
    def test_numbers_and_attributes_passages(self) -> None:
        context, citations = build_context([
            hit("a", "mesh awarded task-1 to openclaw", 0.8, agent="hermes", kind="prompt_result"),
            hit("b", "scribe returned four passages", 0.7, agent="scribe", kind="retrieval"),
        ])
        self.assertIn("[1] hermes/prompt_result", context)
        self.assertIn("[2] scribe/retrieval", context)
        self.assertEqual([c["n"] for c in citations], [1, 2])
        self.assertEqual(citations[1]["agent"], "scribe")

    def test_respects_budget(self) -> None:
        context, citations = build_context(
            [hit(str(i), "mesh " * 200, 0.5 - i / 100) for i in range(10)],
            budget=600,
        )
        self.assertLessEqual(len(context), 700)
        self.assertLess(len(citations), 10)

    def test_rejects_tiny_budget(self) -> None:
        with self.assertRaises(ValueError):
            build_context([], budget=10)


class PromptTests(unittest.TestCase):
    def test_grounded_prompt_carries_rules_and_question(self) -> None:
        prompt = augment_prompt("who won task-1?", "[1] hermes/memory\nopenclaw won task-1")
        self.assertIn("SWARM MEMORY", prompt)
        self.assertIn("Cite the passages you use as [n]", prompt)
        self.assertIn("who won task-1?", prompt)

    def test_empty_context_forbids_invented_citations(self) -> None:
        prompt = augment_prompt("who won?", "")
        self.assertIn("empty", prompt)
        self.assertIn("Do not invent citations", prompt)

    def test_rejects_blank_question(self) -> None:
        with self.assertRaises(ValueError):
            augment_prompt("  ", "ctx")


class AssembleTests(unittest.TestCase):
    def test_end_to_end_marks_grounded(self) -> None:
        result = assemble("which peer won?", [
            hit("a", "the mesh awarded task-1 to openclaw", 0.81),
            hit("b", "the mesh awarded task-1 to openclaw", 0.80),
            hit("c", "hermes holds the reasoning capability", 0.44),
        ], k=3)
        self.assertTrue(result["grounded"])
        self.assertEqual(len(result["citations"]), 2)  # duplicate dropped
        self.assertIn("openclaw", result["context"])

    def test_no_hits_is_not_grounded(self) -> None:
        result = assemble("anything?", [])
        self.assertFalse(result["grounded"])
        self.assertEqual(result["citations"], [])


class RagRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = SqliteVault(root / "t.db")
        self.vectors = SqliteVectorMemory(root / "v.db")
        self.app = create_app(store=self.store, vectors=self.vectors, vault_token=None)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.store.close()
        self.vectors.close()
        self._tmp.cleanup()

    def test_ingest_chunks_then_query_grounds(self) -> None:
        doc = "\n\n".join([
            "The swarm mesh uses a contract net: peers bid and the best bid wins the award.",
            "Pheromone trails reinforce peers that return accepted results.",
            "The scribe agent owns retrieval and answers with cited passages.",
        ])
        ingest = self.client.post(
            "/v1/rag/ingest",
            json={"agent": "scribe", "kind": "doc", "text": doc, "source": "protocol"},
        )
        self.assertEqual(ingest.status_code, 200)
        body = ingest.json()
        self.assertGreaterEqual(body["chunks"], 1)

        res = self.client.get("/v1/rag/query", params={"q": "which peer owns retrieval", "k": 2})
        self.assertEqual(res.status_code, 200)
        payload = res.json()
        self.assertTrue(payload["grounded"])
        self.assertIn("scribe", payload["context"])
        self.assertIn("SWARM MEMORY", payload["prompt"])
        self.assertEqual(payload["space"], "hash-v1")

    def test_ingest_rejects_unknown_agent(self) -> None:
        res = self.client.post(
            "/v1/rag/ingest",
            json={"agent": "rogue", "kind": "doc", "text": "mesh"},
        )
        self.assertEqual(res.status_code, 400)

    def test_ingest_rejects_unchunkable_text(self) -> None:
        res = self.client.post(
            "/v1/rag/ingest",
            json={"agent": "scribe", "kind": "doc", "text": "!!! ???"},
        )
        self.assertEqual(res.status_code, 400)

    def test_ingest_requires_token_when_configured(self) -> None:
        app = create_app(store=self.store, vectors=self.vectors, vault_token="s3cret")
        with TestClient(app) as client:
            body = {"agent": "scribe", "kind": "doc", "text": "mesh peers collaborate"}
            self.assertEqual(client.post("/v1/rag/ingest", json=body).status_code, 401)
            ok = client.post("/v1/rag/ingest", json=body, headers={"X-Vault-Token": "s3cret"})
            self.assertEqual(ok.status_code, 200)

    def test_query_rejects_blank(self) -> None:
        self.assertEqual(self.client.get("/v1/rag/query", params={"q": " "}).status_code, 400)

    def test_query_rejects_out_of_range_n(self) -> None:
        self.assertEqual(
            self.client.get("/v1/rag/query", params={"q": "mesh", "n": 500}).status_code, 400
        )

    def test_health_reports_embed_space(self) -> None:
        self.assertEqual(self.client.get("/health").json()["embed_space"], "hash-v1")


if __name__ == "__main__":
    unittest.main()
