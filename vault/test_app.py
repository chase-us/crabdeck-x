"""HTTP contract tests for the Shell Cracked FastAPI surface."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import create_app
from sqlite_store import SqliteVault
from vectors import SqliteVectorMemory


class VaultAppTests(unittest.TestCase):
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

    def test_health_and_bhive(self) -> None:
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        body = health.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["service"], "shell-cracked")
        self.assertIn("bhive_slot", body)
        bhive = self.client.get("/v1/bhive")
        self.assertEqual(bhive.status_code, 200)
        self.assertIn("slot", bhive.json())
        self.assertEqual(bhive.json()["watchdog_seconds"], 20)

    def test_heartbeat_round_trip(self) -> None:
        now = time.time()
        r = self.client.post(
            "/v1/heartbeat",
            json={"agent": "hermes", "ts": now, "source": "test"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["agent"], "hermes")
        self.assertEqual(body["status"], "running")
        listed = self.client.get("/v1/agents")
        self.assertEqual(listed.status_code, 200)
        agents = listed.json()["agents"]
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["id"], "hermes")

    def test_rejects_unknown_agent(self) -> None:
        r = self.client.post(
            "/v1/heartbeat",
            json={"agent": "not-a-swarm-member", "ts": time.time(), "slot": 0},
        )
        self.assertEqual(r.status_code, 400)

    def test_rejects_non_dict_metadata(self) -> None:
        r = self.client.post(
            "/v1/memory",
            json={
                "agent": "hermes",
                "kind": "prompt_result",
                "text": "ok",
                "metadata": ["not", "a", "dict"],
            },
        )
        self.assertEqual(r.status_code, 422)

    def test_memory_ingest_and_query(self) -> None:
        put = self.client.post(
            "/v1/memory",
            json={
                "agent": "hermes",
                "kind": "prompt_result",
                "text": "operator asked for swarm status; hermes answered green",
            },
        )
        self.assertEqual(put.status_code, 200)
        self.assertIn("id", put.json())
        q = self.client.get("/v1/memory/query", params={"q": "swarm status", "n": 3})
        self.assertEqual(q.status_code, 200)
        hits = q.json()["hits"]
        self.assertGreaterEqual(len(hits), 1)
        self.assertIn("swarm", hits[0]["text"].lower())

    def test_empty_memory_query_rejected(self) -> None:
        r = self.client.get("/v1/memory/query", params={"q": "   "})
        self.assertEqual(r.status_code, 400)

    def test_rag_retrieve_and_filter(self) -> None:
        self.client.post(
            "/v1/memory",
            json={
                "agent": "hermes",
                "kind": "prompt_result",
                "text": "Mesh node alpha reports latency 12ms",
            },
        )
        self.client.post(
            "/v1/memory",
            json={
                "agent": "openclaw",
                "kind": "task_result",
                "text": "Disk status check completed on root partition",
            },
        )
        # Cross-agent RAG retrieve
        res = self.client.post(
            "/v1/rag/retrieve",
            json={"query": "Mesh node latency", "n": 3, "synthesize": True},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("citations", body)
        self.assertIn("context_prompt", body)
        self.assertIn("synthesis", body)
        self.assertGreaterEqual(body["hits_count"], 1)
        self.assertIn("hermes", body["agents_represented"])

        # Agent-filtered retrieve with exact query string
        res_claw = self.client.post(
            "/v1/rag/retrieve",
            json={"query": "Disk status check completed on root partition", "agent": "openclaw", "n": 2},
        )
        self.assertEqual(res_claw.status_code, 200)
        claw_body = res_claw.json()
        self.assertIn("openclaw", claw_body["agents_represented"])
        for h in claw_body["hits"]:
            self.assertEqual(h["metadata"]["agent"], "openclaw")

    def test_session_round_trip(self) -> None:
        put = self.client.post(
            "/v1/session",
            json={"session_id": "loop-9", "context": {"note": "micro-restart"}},
        )
        self.assertEqual(put.status_code, 200)
        got = self.client.get("/v1/session/loop-9")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json()["context"]["note"], "micro-restart")

    def test_missing_session_is_404(self) -> None:
        r = self.client.get("/v1/session/does-not-exist")
        self.assertEqual(r.status_code, 404)

    def test_auth_required_when_token_set(self) -> None:
        locked = create_app(store=self.store, vectors=self.vectors, vault_token="secret")
        client = TestClient(locked)
        try:
            denied = client.post(
                "/v1/heartbeat",
                json={"agent": "hermes", "ts": time.time()},
            )
            self.assertEqual(denied.status_code, 401)
            ok = client.post(
                "/v1/heartbeat",
                json={"agent": "openclaw", "ts": time.time()},
                headers={"X-Vault-Token": "secret"},
            )
            self.assertEqual(ok.status_code, 200)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
