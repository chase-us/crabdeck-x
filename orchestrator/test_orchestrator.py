"""Tests for Orchestrator Swarm Mesh REST API."""

import unittest
from fastapi.testclient import TestClient
from main import app, seed_agents, active_swarm_tasks

class OrchestratorMeshTests(unittest.TestCase):
    def setUp(self):
        seed_agents()
        self.client = TestClient(app)

    def test_health_includes_agent_count(self):
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertGreaterEqual(body["agent_count"], 3)

    def test_mesh_topology_endpoint(self):
        res = self.client.get("/mesh")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("mesh_size", body)
        self.assertIn("agents", body)
        self.assertGreaterEqual(body["mesh_size"], 3)
        agent_names = [a["name"] for a in body["agents"]]
        self.assertTrue(any("Hermes" in name for name in agent_names))
        self.assertTrue(any("OpenClaw" in name for name in agent_names))

    def test_trigger_swarm_task(self):
        res = self.client.post(
            "/mesh/tasks",
            json={"goal": "Audit system resources and synthesize health advisory"},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("task_id", body)
        self.assertEqual(body["status"], "in_progress")

        # Query created task
        task_res = self.client.get(f"/mesh/tasks/{body['task_id']}")
        self.assertEqual(task_res.status_code, 200)
        task_data = task_res.json()
        self.assertEqual(task_data["goal"], "Audit system resources and synthesize health advisory")

if __name__ == "__main__":
    unittest.main()
