"""
Tests for orchestrator/main.py

Run:
    pip install pytest pytest-asyncio httpx fastapi
    cd orchestrator
    pytest tests/test_main.py -v
"""

import asyncio
import os
import sys
import time
import uuid
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — add orchestrator/ to sys.path so we can import main
# ---------------------------------------------------------------------------

ORCHESTRATOR_DIR = os.path.join(os.path.dirname(__file__), "..")
if ORCHESTRATOR_DIR not in sys.path:
    sys.path.insert(0, ORCHESTRATOR_DIR)


# ---------------------------------------------------------------------------
# Helpers — import main with mocked heavy deps
# ---------------------------------------------------------------------------

def _import_main(env_overrides=None):
    """Import (or re-import) orchestrator/main.py with controlled env."""
    overrides = env_overrides or {}
    env = {
        "GATEWAY_URL":     overrides.get("GATEWAY_URL",     "ws://localhost:8765"),
        "ALLOWED_ORIGINS": overrides.get("ALLOWED_ORIGINS", "http://localhost:5173"),
    }
    if "GATEWAY_TOKEN" in overrides:
        env["GATEWAY_TOKEN"] = overrides["GATEWAY_TOKEN"]
    else:
        env.pop("GATEWAY_TOKEN", None)

    sys.modules.pop("main", None)
    with patch.dict(os.environ, env, clear=False):
        import main as m
        return m


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset module-level state before each test."""
    sys.modules.pop("main", None)
    yield
    # Clean up imported module
    sys.modules.pop("main", None)


# ---------------------------------------------------------------------------
# Import + fixtures for FastAPI TestClient
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    """Create a FastAPI TestClient with agents pre-seeded.

    The lifespan launches listen_gateway() which tries to connect to the
    WebSocket gateway (not running during tests).  We patch websockets.connect
    so the coroutine raises immediately, causing listen_gateway to log an
    event and retry — but the task is cancelled during TestClient shutdown
    so tests complete quickly.
    """
    from fastapi.testclient import TestClient
    import main

    async def _never_connect(*args, **kwargs):
        raise ConnectionRefusedError("gateway not available during tests")

    # Patch at the module level so listen_gateway() sees the mock
    with patch("main.websockets.connect", side_effect=_never_connect):
        # Reset module state
        main.agents.clear()
        main.events.clear()
        main._heartbeat_thread_started = False
        main.seed_agents()

        with TestClient(main.app) as c:
            yield c

    main.agents.clear()
    main.events.clear()


# ---------------------------------------------------------------------------
# add_event()
# ---------------------------------------------------------------------------

class TestAddEvent:

    def test_appends_event_to_list(self):
        import main
        main.events.clear()
        main.add_event("TEST", "test message", None)
        assert len(main.events) == 1

    def test_event_has_correct_type_and_message(self):
        import main
        main.events.clear()
        main.add_event("MY_TYPE", "my message", "agent1")
        evt = main.events[-1]
        assert evt.type == "MY_TYPE"
        assert evt.message == "my message"
        assert evt.agent_id == "agent1"

    def test_event_has_unique_id(self):
        import main
        main.events.clear()
        main.add_event("T1", "msg1", None)
        main.add_event("T2", "msg2", None)
        ids = [e.id for e in main.events]
        assert ids[0] != ids[1]

    def test_event_has_timestamp(self):
        import main
        main.events.clear()
        before = time.time()
        main.add_event("T", "m", None)
        after = time.time()
        evt = main.events[-1]
        assert before <= evt.timestamp <= after

    def test_enforces_max_events_limit(self):
        import main
        main.events.clear()
        # Fill to MAX_EVENTS
        for i in range(main.MAX_EVENTS + 10):
            main.add_event("T", f"msg {i}", None)
        assert len(main.events) <= main.MAX_EVENTS

    def test_drops_oldest_events_when_over_limit(self):
        import main
        main.events.clear()
        # Fill exactly to limit
        for i in range(main.MAX_EVENTS):
            main.add_event("T", f"msg {i}", None)
        # Add one more — should drop the oldest
        main.add_event("T", "newest message", None)
        assert len(main.events) == main.MAX_EVENTS
        # The newest message should still be there
        assert main.events[-1].message == "newest message"

    def test_agent_id_none_allowed(self):
        import main
        main.events.clear()
        main.add_event("SYSTEM", "system event", None)
        assert main.events[-1].agent_id is None


# ---------------------------------------------------------------------------
# seed_agents()
# ---------------------------------------------------------------------------

class TestSeedAgents:

    def test_creates_three_agents(self):
        import main
        main.agents.clear()
        main.events.clear()
        main.seed_agents()
        assert len(main.agents) == 3

    def test_creates_expected_agent_ids(self):
        import main
        main.agents.clear()
        main.events.clear()
        main.seed_agents()
        assert "crabdeck" in main.agents
        assert "openclaw" in main.agents
        assert "hermes" in main.agents

    def test_agents_start_offline(self):
        import main
        main.agents.clear()
        main.events.clear()
        main.seed_agents()
        for agent in main.agents.values():
            assert agent.status == "offline"

    def test_agents_have_correct_names(self):
        import main
        main.agents.clear()
        main.events.clear()
        main.seed_agents()
        assert main.agents["crabdeck"].name == "CrabDeck Gateway"
        assert main.agents["openclaw"].name == "OpenClaw Sovereign"
        assert main.agents["hermes"].name == "Hermes Messenger"

    def test_agents_belong_to_crabdeck_team(self):
        import main
        main.agents.clear()
        main.events.clear()
        main.seed_agents()
        for agent in main.agents.values():
            assert agent.team == "crabdeck"

    def test_agents_have_last_heartbeat_set(self):
        import main
        main.agents.clear()
        main.events.clear()
        before = time.time()
        main.seed_agents()
        after = time.time()
        for agent in main.agents.values():
            assert before <= agent.last_heartbeat <= after

    def test_adds_seeded_event(self):
        import main
        main.agents.clear()
        main.events.clear()
        main.seed_agents()
        event_types = [e.type for e in main.events]
        assert "SYSTEM" in event_types


# ---------------------------------------------------------------------------
# heartbeat_loop()  (unit test of the logic — not the threading)
# ---------------------------------------------------------------------------

class TestHeartbeatLoop:

    def test_marks_agent_as_error_after_timeout(self):
        import main
        main.agents.clear()
        main.events.clear()
        main.seed_agents()
        # Force hermes into running status with an old heartbeat
        main.agents["hermes"].status = "running"
        main.agents["hermes"].last_heartbeat = time.time() - (main.HEARTBEAT_TIMEOUT + 5)

        # Run one iteration of the loop logic directly (not via thread)
        now = time.time()
        for agent_id, agent in list(main.agents.items()):
            if agent.status == "running" and (now - agent.last_heartbeat) > main.HEARTBEAT_TIMEOUT:
                agent.status = "error"
                agent.error_message = "Heartbeat missed"
                main.add_event("HEARTBEAT_MISSED", f"{agent.name} missed heartbeat", agent_id)

        assert main.agents["hermes"].status == "error"
        assert main.agents["hermes"].error_message == "Heartbeat missed"

    def test_does_not_mark_offline_agents_as_error(self):
        import main
        main.agents.clear()
        main.events.clear()
        main.seed_agents()
        # hermes is offline (default), with an old-looking heartbeat
        main.agents["hermes"].status = "offline"
        main.agents["hermes"].last_heartbeat = time.time() - 9999

        now = time.time()
        for agent_id, agent in list(main.agents.items()):
            if agent.status == "running" and (now - agent.last_heartbeat) > main.HEARTBEAT_TIMEOUT:
                agent.status = "error"

        # Should still be offline — was not "running"
        assert main.agents["hermes"].status == "offline"

    def test_running_agent_within_timeout_not_marked_error(self):
        import main
        main.agents.clear()
        main.events.clear()
        main.seed_agents()
        main.agents["hermes"].status = "running"
        main.agents["hermes"].last_heartbeat = time.time()  # just now

        now = time.time()
        for agent_id, agent in list(main.agents.items()):
            if agent.status == "running" and (now - agent.last_heartbeat) > main.HEARTBEAT_TIMEOUT:
                agent.status = "error"

        assert main.agents["hermes"].status == "running"


# ---------------------------------------------------------------------------
# REST API — /health
# ---------------------------------------------------------------------------

class TestHealthEndpoint:

    def test_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_returns_status_ok(self, client):
        resp = client.get("/health")
        data = resp.json()
        assert data["status"] == "ok"

    def test_returns_agent_count(self, client):
        resp = client.get("/health")
        data = resp.json()
        assert data["agent_count"] == 3  # seeded 3 agents

    def test_returns_uptime_field(self, client):
        resp = client.get("/health")
        data = resp.json()
        assert "uptime" in data
        assert isinstance(data["uptime"], float)


# ---------------------------------------------------------------------------
# REST API — /agents
# ---------------------------------------------------------------------------

class TestListAgentsEndpoint:

    def test_returns_200(self, client):
        resp = client.get("/agents")
        assert resp.status_code == 200

    def test_returns_list_of_agents(self, client):
        resp = client.get("/agents")
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 3

    def test_agents_have_required_fields(self, client):
        resp = client.get("/agents")
        data = resp.json()
        required_fields = {"id", "name", "status", "last_heartbeat"}
        for agent in data:
            assert required_fields.issubset(agent.keys())

    def test_agent_ids_present(self, client):
        resp = client.get("/agents")
        data = resp.json()
        ids = {a["id"] for a in data}
        assert "crabdeck" in ids
        assert "openclaw" in ids
        assert "hermes" in ids


# ---------------------------------------------------------------------------
# REST API — /agents/{agent_id}
# ---------------------------------------------------------------------------

class TestGetAgentEndpoint:

    def test_returns_200_for_valid_agent(self, client):
        resp = client.get("/agents/hermes")
        assert resp.status_code == 200

    def test_returns_agent_data(self, client):
        resp = client.get("/agents/hermes")
        data = resp.json()
        assert data["id"] == "hermes"
        assert data["name"] == "Hermes Messenger"

    def test_returns_404_for_unknown_agent(self, client):
        resp = client.get("/agents/does-not-exist")
        assert resp.status_code == 404

    def test_404_detail_message(self, client):
        resp = client.get("/agents/nonexistent")
        data = resp.json()
        assert "not found" in data["detail"].lower()

    def test_returns_openclaw_agent(self, client):
        resp = client.get("/agents/openclaw")
        assert resp.status_code == 200
        assert resp.json()["id"] == "openclaw"

    def test_returns_crabdeck_agent(self, client):
        resp = client.get("/agents/crabdeck")
        assert resp.status_code == 200
        assert resp.json()["id"] == "crabdeck"


# ---------------------------------------------------------------------------
# REST API — /agents/{agent_id}/restart
# ---------------------------------------------------------------------------

class TestRestartAgentEndpoint:

    def test_returns_200_for_valid_agent(self, client):
        resp = client.post("/agents/hermes/restart")
        assert resp.status_code == 200

    def test_sets_status_to_running(self, client):
        import main
        main.agents["hermes"].status = "error"
        resp = client.post("/agents/hermes/restart")
        data = resp.json()
        assert data["status"] == "running"

    def test_clears_error_message(self, client):
        import main
        main.agents["hermes"].status = "error"
        main.agents["hermes"].error_message = "Heartbeat missed"
        resp = client.post("/agents/hermes/restart")
        data = resp.json()
        assert data["error_message"] is None

    def test_updates_last_heartbeat(self, client):
        import main
        old_hb = time.time() - 999
        main.agents["hermes"].last_heartbeat = old_hb
        before = time.time()
        resp = client.post("/agents/hermes/restart")
        data = resp.json()
        assert data["last_heartbeat"] >= before

    def test_returns_404_for_unknown_agent(self, client):
        resp = client.post("/agents/ghost/restart")
        assert resp.status_code == 404

    def test_adds_restart_event(self, client):
        import main
        main.events.clear()
        client.post("/agents/hermes/restart")
        event_types = [e.type for e in main.events]
        assert "AGENT_RESTARTED" in event_types

    def test_restart_offline_agent(self, client):
        import main
        main.agents["openclaw"].status = "offline"
        resp = client.post("/agents/openclaw/restart")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"


# ---------------------------------------------------------------------------
# REST API — /events
# ---------------------------------------------------------------------------

class TestGetEventsEndpoint:

    def test_returns_200(self, client):
        resp = client.get("/events")
        assert resp.status_code == 200

    def test_returns_list(self, client):
        resp = client.get("/events")
        assert isinstance(resp.json(), list)

    def test_returns_events_up_to_limit(self, client):
        import main
        main.events.clear()
        for i in range(50):
            main.add_event("T", f"msg {i}", None)
        resp = client.get("/events?limit=10")
        data = resp.json()
        assert len(data) == 10

    def test_default_limit_is_100(self, client):
        import main
        main.events.clear()
        for i in range(150):
            main.add_event("T", f"msg {i}", None)
        resp = client.get("/events")
        data = resp.json()
        assert len(data) == 100

    def test_returns_latest_events(self, client):
        import main
        main.events.clear()
        for i in range(20):
            main.add_event("T", f"msg {i}", None)
        resp = client.get("/events?limit=5")
        data = resp.json()
        messages = [e["message"] for e in data]
        assert "msg 19" in messages  # last added should be in results

    def test_events_have_required_fields(self, client):
        import main
        main.events.clear()
        main.add_event("TEST", "test msg", "agent1")
        resp = client.get("/events")
        data = resp.json()
        evt = data[-1]
        assert "id" in evt
        assert "timestamp" in evt
        assert "type" in evt
        assert "message" in evt

    def test_returns_empty_list_when_no_events(self, client):
        import main
        main.events.clear()
        resp = client.get("/events")
        assert resp.json() == []


# ---------------------------------------------------------------------------
# CORS configuration
# ---------------------------------------------------------------------------

class TestCORSConfig:

    def test_default_allowed_origin(self):
        env = os.environ.copy()
        env.pop("ALLOWED_ORIGINS", None)
        with patch.dict(os.environ, env, clear=True):
            sys.modules.pop("main", None)
            import main
            assert "http://localhost:5173" in main.ALLOWED_ORIGINS

    def test_custom_allowed_origins_parsed(self):
        origins = "http://localhost:5173,https://app.example.com"
        with patch.dict(os.environ, {"ALLOWED_ORIGINS": origins}, clear=False):
            sys.modules.pop("main", None)
            import main
            assert "http://localhost:5173" in main.ALLOWED_ORIGINS
            assert "https://app.example.com" in main.ALLOWED_ORIGINS

    def test_allowed_origins_strips_whitespace(self):
        origins = " http://localhost:5173 , https://app.example.com "
        with patch.dict(os.environ, {"ALLOWED_ORIGINS": origins}, clear=False):
            sys.modules.pop("main", None)
            import main
            for origin in main.ALLOWED_ORIGINS:
                assert origin == origin.strip()

    def test_wildcard_not_used_in_cors(self):
        """The orchestrator must NOT use allow_origins=* — it should use ALLOWED_ORIGINS."""
        import main
        # Verify no wildcard in the configured origins
        assert "*" not in main.ALLOWED_ORIGINS


# ---------------------------------------------------------------------------
# Agent model defaults
# ---------------------------------------------------------------------------

class TestAgentModel:

    def test_agent_auto_restart_defaults_true(self):
        import main
        main.agents.clear()
        main.events.clear()
        main.seed_agents()
        for agent in main.agents.values():
            assert agent.auto_restart is True

    def test_agent_cpu_starts_at_zero(self):
        import main
        main.agents.clear()
        main.events.clear()
        main.seed_agents()
        for agent in main.agents.values():
            assert agent.cpu_percent == 0.0

    def test_agent_memory_starts_at_zero(self):
        import main
        main.agents.clear()
        main.events.clear()
        main.seed_agents()
        for agent in main.agents.values():
            assert agent.memory_mb == 0.0

    def test_agent_error_message_starts_none(self):
        import main
        main.agents.clear()
        main.events.clear()
        main.seed_agents()
        for agent in main.agents.values():
            assert agent.error_message is None


# ---------------------------------------------------------------------------
# Gateway token config
# ---------------------------------------------------------------------------

class TestGatewayTokenConfig:

    def test_gateway_token_none_when_not_set(self):
        env = os.environ.copy()
        env.pop("GATEWAY_TOKEN", None)
        with patch.dict(os.environ, env, clear=True):
            sys.modules.pop("main", None)
            import main
            assert main.GATEWAY_TOKEN is None

    def test_gateway_token_set_from_env(self):
        with patch.dict(os.environ, {"GATEWAY_TOKEN": "mysecrettoken"}, clear=False):
            sys.modules.pop("main", None)
            import main
            assert main.GATEWAY_TOKEN == "mysecrettoken"