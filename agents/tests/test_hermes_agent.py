"""
Tests for agents/hermes_agent.py

Run:
    pip install pytest pytest-asyncio requests
    cd agents
    pytest tests/test_hermes_agent.py -v
"""

import asyncio
import json
import os
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Helpers to import the module with controlled env vars
# ---------------------------------------------------------------------------

def _import_hermes(env_overrides=None):
    """Import (or re-import) hermes_agent with the given env overrides."""
    import importlib
    overrides = env_overrides or {}
    env_patch = {
        "GATEWAY_URL":   overrides.get("GATEWAY_URL",    "ws://localhost:8765"),
        "GATEWAY_TOKEN": overrides.get("GATEWAY_TOKEN",  ""),
        "OLLAMA_URL":    overrides.get("OLLAMA_URL",     "http://localhost:11434"),
        "DEFAULT_MODEL": overrides.get("DEFAULT_MODEL",  "llama3"),
    }
    # Strip empty strings so unset vars don't clobber defaults via os.environ.get fallbacks
    env_patch_clean = {k: v for k, v in env_patch.items() if v != ""}
    # Remove keys not in overrides so module picks up its own defaults
    if "GATEWAY_TOKEN" not in overrides:
        env_patch_clean.pop("GATEWAY_TOKEN", None)

    with patch.dict(os.environ, env_patch_clean, clear=False):
        if "hermes_agent" in sys.modules:
            del sys.modules["hermes_agent"]
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import hermes_agent
        return hermes_agent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _ensure_sys_path():
    agents_dir = os.path.join(os.path.dirname(__file__), "..")
    if agents_dir not in sys.path:
        sys.path.insert(0, agents_dir)
    yield
    # Cleanup imported module so each test gets a fresh import
    sys.modules.pop("hermes_agent", None)


# ---------------------------------------------------------------------------
# ollama_available()
# ---------------------------------------------------------------------------

class TestOllamaAvailable:

    def test_returns_true_when_status_200(self):
        import hermes_agent
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("hermes_agent.requests.get", return_value=mock_resp) as mock_get:
            result = hermes_agent.ollama_available()
        assert result is True
        mock_get.assert_called_once()

    def test_returns_false_when_status_non_200(self):
        import hermes_agent
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        with patch("hermes_agent.requests.get", return_value=mock_resp):
            result = hermes_agent.ollama_available()
        assert result is False

    def test_returns_false_on_connection_error(self):
        import hermes_agent
        with patch("hermes_agent.requests.get", side_effect=ConnectionError("refused")):
            result = hermes_agent.ollama_available()
        assert result is False

    def test_returns_false_on_timeout(self):
        import hermes_agent
        import requests as req_lib
        with patch("hermes_agent.requests.get", side_effect=req_lib.exceptions.Timeout()):
            result = hermes_agent.ollama_available()
        assert result is False

    def test_uses_correct_endpoint(self):
        import hermes_agent
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("hermes_agent.requests.get", return_value=mock_resp) as mock_get:
            hermes_agent.ollama_available()
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith("/api/tags")

    def test_uses_timeout_3(self):
        import hermes_agent
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("hermes_agent.requests.get", return_value=mock_resp) as mock_get:
            hermes_agent.ollama_available()
        assert mock_get.call_args[1].get("timeout") == 3


# ---------------------------------------------------------------------------
# ollama_models()
# ---------------------------------------------------------------------------

class TestOllamaModels:

    def test_returns_list_of_model_names(self):
        import hermes_agent
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "models": [{"name": "llama3"}, {"name": "mistral"}, {"name": "crabdeck"}]
        }
        with patch("hermes_agent.requests.get", return_value=mock_resp):
            result = hermes_agent.ollama_models()
        assert result == ["llama3", "mistral", "crabdeck"]

    def test_returns_empty_list_when_no_models_key(self):
        import hermes_agent
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        with patch("hermes_agent.requests.get", return_value=mock_resp):
            result = hermes_agent.ollama_models()
        assert result == []

    def test_returns_empty_list_on_network_error(self):
        import hermes_agent
        with patch("hermes_agent.requests.get", side_effect=ConnectionError("refused")):
            result = hermes_agent.ollama_models()
        assert result == []

    def test_returns_empty_list_on_json_error(self):
        import hermes_agent
        mock_resp = MagicMock()
        mock_resp.json.side_effect = ValueError("bad json")
        with patch("hermes_agent.requests.get", return_value=mock_resp):
            result = hermes_agent.ollama_models()
        assert result == []

    def test_returns_empty_list_when_models_is_empty(self):
        import hermes_agent
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"models": []}
        with patch("hermes_agent.requests.get", return_value=mock_resp):
            result = hermes_agent.ollama_models()
        assert result == []

    def test_single_model(self):
        import hermes_agent
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"models": [{"name": "phi3"}]}
        with patch("hermes_agent.requests.get", return_value=mock_resp):
            result = hermes_agent.ollama_models()
        assert result == ["phi3"]


# ---------------------------------------------------------------------------
# ollama_generate()
# ---------------------------------------------------------------------------

class TestOllamaGenerate:

    def test_returns_response_text_on_success(self):
        import hermes_agent
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "Hello from Ollama!"}
        mock_resp.raise_for_status = MagicMock()
        with patch("hermes_agent.requests.post", return_value=mock_resp):
            result = hermes_agent.ollama_generate("Say hello")
        assert result == "Hello from Ollama!"

    def test_returns_fallback_when_no_response_key(self):
        import hermes_agent
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status = MagicMock()
        with patch("hermes_agent.requests.post", return_value=mock_resp):
            result = hermes_agent.ollama_generate("ping")
        assert result == "(no response from Ollama)"

    def test_returns_error_string_on_http_error(self):
        import hermes_agent
        import requests as req_lib
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req_lib.exceptions.HTTPError("500")
        with patch("hermes_agent.requests.post", return_value=mock_resp):
            result = hermes_agent.ollama_generate("prompt")
        assert result.startswith("[Hermes error]")

    def test_returns_error_string_on_connection_error(self):
        import hermes_agent
        with patch("hermes_agent.requests.post", side_effect=ConnectionError("refused")):
            result = hermes_agent.ollama_generate("prompt")
        assert result.startswith("[Hermes error]")

    def test_posts_correct_payload_structure(self):
        import hermes_agent
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()
        with patch("hermes_agent.requests.post", return_value=mock_resp) as mock_post:
            hermes_agent.ollama_generate("my prompt", "mistral")
        _, kwargs = mock_post.call_args
        body = kwargs["json"]
        assert body["model"] == "mistral"
        assert body["prompt"] == "my prompt"
        assert body["stream"] is False

    def test_uses_default_model_when_none_specified(self):
        import hermes_agent
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()
        with patch("hermes_agent.requests.post", return_value=mock_resp) as mock_post:
            hermes_agent.ollama_generate("test prompt")
        body = mock_post.call_args[1]["json"]
        assert body["model"] == hermes_agent.DEFAULT_MODEL

    def test_uses_timeout_120(self):
        import hermes_agent
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()
        with patch("hermes_agent.requests.post", return_value=mock_resp) as mock_post:
            hermes_agent.ollama_generate("test")
        assert mock_post.call_args[1].get("timeout") == 120

    def test_posts_to_api_generate_endpoint(self):
        import hermes_agent
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()
        with patch("hermes_agent.requests.post", return_value=mock_resp) as mock_post:
            hermes_agent.ollama_generate("test")
        url = mock_post.call_args[0][0]
        assert url.endswith("/api/generate")

    def test_returns_error_on_timeout(self):
        import hermes_agent
        import requests as req_lib
        with patch("hermes_agent.requests.post", side_effect=req_lib.exceptions.Timeout()):
            result = hermes_agent.ollama_generate("test")
        assert result.startswith("[Hermes error]")


# ---------------------------------------------------------------------------
# heartbeat() coroutine
# ---------------------------------------------------------------------------

class TestHeartbeat:

    @pytest.mark.asyncio
    async def test_heartbeat_sends_json_message(self):
        import hermes_agent
        ws = AsyncMock()
        # Cancel the infinite loop after first iteration by making sleep raise CancelledError
        call_count = 0

        async def fake_sleep(n):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise asyncio.CancelledError()

        with patch("hermes_agent.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await hermes_agent.heartbeat(ws)

        ws.send.assert_called_once()
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["type"] == "HEARTBEAT"
        assert sent["agent"] == "hermes"
        assert "ts" in sent

    @pytest.mark.asyncio
    async def test_heartbeat_breaks_on_send_exception(self):
        import hermes_agent
        ws = AsyncMock()
        ws.send.side_effect = Exception("ws closed")

        async def fast_sleep(n):
            pass  # Don't actually sleep

        with patch("hermes_agent.asyncio.sleep", side_effect=fast_sleep):
            # Should return without raising (breaks out of while loop)
            await hermes_agent.heartbeat(ws)

        # send was called once and threw, causing the loop to break
        assert ws.send.call_count == 1

    @pytest.mark.asyncio
    async def test_heartbeat_timestamp_is_float(self):
        import hermes_agent
        ws = AsyncMock()
        call_count = 0

        async def fake_sleep(n):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise asyncio.CancelledError()

        with patch("hermes_agent.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await hermes_agent.heartbeat(ws)

        sent = json.loads(ws.send.call_args[0][0])
        assert isinstance(sent["ts"], float)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

class TestModuleConstants:

    def test_default_gateway_url(self):
        env = {"OLLAMA_URL": "http://localhost:11434", "DEFAULT_MODEL": "llama3"}
        with patch.dict(os.environ, env, clear=False):
            if "hermes_agent" in sys.modules:
                del sys.modules["hermes_agent"]
            import hermes_agent
            assert hermes_agent.GATEWAY_URL == "ws://localhost:8765"

    def test_custom_ollama_url_from_env(self):
        with patch.dict(os.environ, {"OLLAMA_URL": "http://myhost:9999"}, clear=False):
            if "hermes_agent" in sys.modules:
                del sys.modules["hermes_agent"]
            import hermes_agent
            # OLLAMA_URL used in requests, check the constant
            assert hermes_agent.OLLAMA_URL == "http://myhost:9999"

    def test_gateway_token_none_by_default(self):
        env = os.environ.copy()
        env.pop("GATEWAY_TOKEN", None)
        with patch.dict(os.environ, env, clear=True):
            if "hermes_agent" in sys.modules:
                del sys.modules["hermes_agent"]
            import hermes_agent
            assert hermes_agent.GATEWAY_TOKEN is None

    def test_custom_default_model(self):
        with patch.dict(os.environ, {"DEFAULT_MODEL": "codellama"}, clear=False):
            if "hermes_agent" in sys.modules:
                del sys.modules["hermes_agent"]
            import hermes_agent
            assert hermes_agent.DEFAULT_MODEL == "codellama"

    def test_heartbeat_every_is_10(self):
        import hermes_agent
        assert hermes_agent.HEARTBEAT_EVERY == 10

    def test_reconnect_delay_is_5(self):
        import hermes_agent
        assert hermes_agent.RECONNECT_DELAY == 5