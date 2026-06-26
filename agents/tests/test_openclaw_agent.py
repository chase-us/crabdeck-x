"""
Tests for agents/openclaw_agent.py

Run:
    pip install pytest pytest-asyncio requests
    cd agents
    pytest tests/test_openclaw_agent.py -v
"""

import asyncio
import json
import os
import platform
import subprocess
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

AGENTS_DIR = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture(autouse=True)
def _reset_module():
    """Ensure openclaw_agent is freshly imported for each test."""
    if AGENTS_DIR not in sys.path:
        sys.path.insert(0, AGENTS_DIR)
    yield
    sys.modules.pop("openclaw_agent", None)


def _import_openclaw(env_overrides=None):
    """Import openclaw_agent with controlled environment variables."""
    overrides = env_overrides or {}
    env = {
        "GATEWAY_URL":      overrides.get("GATEWAY_URL",      "ws://localhost:8765"),
        "OLLAMA_URL":       overrides.get("OLLAMA_URL",        "http://localhost:11434"),
        "OPENCLAW_HTTP":    overrides.get("OPENCLAW_HTTP",     "http://localhost:3131"),
        "DEFAULT_MODEL":    overrides.get("DEFAULT_MODEL",     "llama3"),
        "ENABLE_SHELL_EXEC": overrides.get("ENABLE_SHELL_EXEC", "0"),
        "SHELL_ALLOWLIST":  overrides.get("SHELL_ALLOWLIST",   ""),
    }
    if "GATEWAY_TOKEN" in overrides:
        env["GATEWAY_TOKEN"] = overrides["GATEWAY_TOKEN"]
    else:
        env.pop("GATEWAY_TOKEN", None)

    sys.modules.pop("openclaw_agent", None)
    with patch.dict(os.environ, env, clear=False):
        import openclaw_agent
        return openclaw_agent


# ---------------------------------------------------------------------------
# openclaw_available()
# ---------------------------------------------------------------------------

class TestOpenclawAvailable:

    def test_returns_true_when_status_200(self):
        oc = _import_openclaw()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("openclaw_agent.requests.get", return_value=mock_resp):
            assert oc.openclaw_available() is True

    def test_returns_false_when_status_non_200(self):
        oc = _import_openclaw()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("openclaw_agent.requests.get", return_value=mock_resp):
            assert oc.openclaw_available() is False

    def test_returns_false_on_connection_error(self):
        oc = _import_openclaw()
        with patch("openclaw_agent.requests.get", side_effect=ConnectionError("refused")):
            assert oc.openclaw_available() is False

    def test_calls_health_endpoint(self):
        oc = _import_openclaw()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("openclaw_agent.requests.get", return_value=mock_resp) as mock_get:
            oc.openclaw_available()
        url = mock_get.call_args[0][0]
        assert url.endswith("/health")

    def test_uses_timeout_2(self):
        oc = _import_openclaw()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("openclaw_agent.requests.get", return_value=mock_resp) as mock_get:
            oc.openclaw_available()
        assert mock_get.call_args[1].get("timeout") == 2


# ---------------------------------------------------------------------------
# openclaw_task()
# ---------------------------------------------------------------------------

class TestOpenclawTask:

    def test_returns_result_on_success(self):
        oc = _import_openclaw()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": "task done"}
        mock_resp.raise_for_status = MagicMock()
        with patch("openclaw_agent.requests.post", return_value=mock_resp):
            result = oc.openclaw_task("do something")
        assert result == "task done"

    def test_returns_fallback_string_when_no_result_key(self):
        oc = _import_openclaw()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status = MagicMock()
        with patch("openclaw_agent.requests.post", return_value=mock_resp):
            result = oc.openclaw_task("do something")
        assert result == "(no result)"

    def test_returns_none_on_exception(self):
        oc = _import_openclaw()
        with patch("openclaw_agent.requests.post", side_effect=ConnectionError("refused")):
            result = oc.openclaw_task("do something")
        assert result is None

    def test_returns_none_on_http_error(self):
        oc = _import_openclaw()
        import requests as req_lib
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req_lib.exceptions.HTTPError("500")
        with patch("openclaw_agent.requests.post", return_value=mock_resp):
            result = oc.openclaw_task("do something")
        assert result is None

    def test_posts_task_json_body(self):
        oc = _import_openclaw()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": "ok"}
        mock_resp.raise_for_status = MagicMock()
        with patch("openclaw_agent.requests.post", return_value=mock_resp) as mock_post:
            oc.openclaw_task("list files")
        body = mock_post.call_args[1]["json"]
        assert body == {"task": "list files"}

    def test_posts_to_task_endpoint(self):
        oc = _import_openclaw()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": "ok"}
        mock_resp.raise_for_status = MagicMock()
        with patch("openclaw_agent.requests.post", return_value=mock_resp) as mock_post:
            oc.openclaw_task("test")
        url = mock_post.call_args[0][0]
        assert url.endswith("/task")


# ---------------------------------------------------------------------------
# system_info()
# ---------------------------------------------------------------------------

class TestSystemInfo:

    def test_returns_dict_with_required_keys(self):
        oc = _import_openclaw()
        info = oc.system_info()
        required_keys = {"platform", "node", "python", "cwd", "pid", "shell_exec_enabled"}
        assert required_keys.issubset(info.keys())

    def test_pid_is_current_process(self):
        oc = _import_openclaw()
        info = oc.system_info()
        assert info["pid"] == os.getpid()

    def test_cwd_is_current_directory(self):
        oc = _import_openclaw()
        info = oc.system_info()
        assert info["cwd"] == os.getcwd()

    def test_shell_exec_enabled_false_by_default(self):
        oc = _import_openclaw({"ENABLE_SHELL_EXEC": "0"})
        info = oc.system_info()
        assert info["shell_exec_enabled"] is False

    def test_shell_exec_enabled_true_when_set(self):
        oc = _import_openclaw({"ENABLE_SHELL_EXEC": "1"})
        info = oc.system_info()
        assert info["shell_exec_enabled"] is True

    def test_python_version_is_string(self):
        oc = _import_openclaw()
        info = oc.system_info()
        assert isinstance(info["python"], str)
        assert len(info["python"]) > 0

    def test_platform_is_string(self):
        oc = _import_openclaw()
        info = oc.system_info()
        assert isinstance(info["platform"], str)


# ---------------------------------------------------------------------------
# command_allowed()
# ---------------------------------------------------------------------------

class TestCommandAllowed:

    def test_returns_true_when_no_allowlist(self):
        """When SHELL_ALLOWLIST is unset/empty, all commands pass."""
        with patch.dict(os.environ, {"SHELL_ALLOWLIST": "", "ENABLE_SHELL_EXEC": "0"}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            assert openclaw_agent.SHELL_ALLOWLIST is None
            assert openclaw_agent.command_allowed("rm -rf /") is True

    def test_returns_true_when_command_matches_prefix(self):
        with patch.dict(os.environ, {"SHELL_ALLOWLIST": "git,ls,docker ps", "ENABLE_SHELL_EXEC": "1"}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            assert openclaw_agent.command_allowed("git status") is True

    def test_returns_true_for_exact_prefix_match(self):
        with patch.dict(os.environ, {"SHELL_ALLOWLIST": "ls", "ENABLE_SHELL_EXEC": "1"}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            assert openclaw_agent.command_allowed("ls -la") is True

    def test_returns_false_when_no_prefix_matches(self):
        with patch.dict(os.environ, {"SHELL_ALLOWLIST": "git,ls", "ENABLE_SHELL_EXEC": "1"}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            assert openclaw_agent.command_allowed("rm -rf /") is False

    def test_returns_false_for_empty_command_when_allowlist_set(self):
        with patch.dict(os.environ, {"SHELL_ALLOWLIST": "git,ls", "ENABLE_SHELL_EXEC": "1"}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            assert openclaw_agent.command_allowed("") is False

    def test_handles_leading_whitespace_in_command(self):
        """command_allowed strips leading whitespace from cmd."""
        with patch.dict(os.environ, {"SHELL_ALLOWLIST": "git", "ENABLE_SHELL_EXEC": "1"}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            assert openclaw_agent.command_allowed("  git status") is True

    def test_multiple_prefixes_in_allowlist(self):
        with patch.dict(os.environ, {"SHELL_ALLOWLIST": "git,docker ps,ls,dir", "ENABLE_SHELL_EXEC": "1"}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            assert openclaw_agent.command_allowed("docker ps -a") is True
            assert openclaw_agent.command_allowed("docker rm container") is False

    def test_allowlist_with_spaces_around_commas(self):
        """Allowlist entries should be stripped of surrounding whitespace."""
        with patch.dict(os.environ, {"SHELL_ALLOWLIST": " git , ls ", "ENABLE_SHELL_EXEC": "1"}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            assert openclaw_agent.command_allowed("git status") is True
            assert openclaw_agent.command_allowed("ls -la") is True


# ---------------------------------------------------------------------------
# run_shell()
# ---------------------------------------------------------------------------

class TestRunShell:

    def test_returns_blocked_message_when_shell_exec_disabled(self):
        oc = _import_openclaw({"ENABLE_SHELL_EXEC": "0"})
        result = oc.run_shell("echo hello")
        assert "[blocked]" in result
        assert "disabled" in result

    def test_does_not_call_subprocess_when_disabled(self):
        oc = _import_openclaw({"ENABLE_SHELL_EXEC": "0"})
        with patch("openclaw_agent.subprocess.run") as mock_run:
            oc.run_shell("echo hello")
        mock_run.assert_not_called()

    def test_returns_blocked_when_command_not_in_allowlist(self):
        with patch.dict(os.environ, {"SHELL_ALLOWLIST": "ls,git", "ENABLE_SHELL_EXEC": "1"}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            result = openclaw_agent.run_shell("rm -rf /tmp/test")
        assert "[blocked]" in result
        assert "SHELL_ALLOWLIST" in result

    def test_executes_command_when_enabled_and_no_allowlist(self):
        with patch.dict(os.environ, {"ENABLE_SHELL_EXEC": "1", "SHELL_ALLOWLIST": ""}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            mock_result = MagicMock()
            mock_result.stdout = "hello world\n"
            mock_result.stderr = ""
            with patch("openclaw_agent.subprocess.run", return_value=mock_result):
                result = openclaw_agent.run_shell("echo hello world")
        assert result == "hello world"

    def test_combines_stdout_and_stderr(self):
        with patch.dict(os.environ, {"ENABLE_SHELL_EXEC": "1", "SHELL_ALLOWLIST": ""}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            mock_result = MagicMock()
            mock_result.stdout = "out"
            mock_result.stderr = "err"
            with patch("openclaw_agent.subprocess.run", return_value=mock_result):
                result = openclaw_agent.run_shell("cmd")
        assert result == "outerr"

    def test_returns_no_output_when_both_empty(self):
        with patch.dict(os.environ, {"ENABLE_SHELL_EXEC": "1", "SHELL_ALLOWLIST": ""}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            mock_result = MagicMock()
            mock_result.stdout = ""
            mock_result.stderr = ""
            with patch("openclaw_agent.subprocess.run", return_value=mock_result):
                result = openclaw_agent.run_shell("cmd")
        assert result == "(no output)"

    def test_returns_timeout_message_on_timeout(self):
        with patch.dict(os.environ, {"ENABLE_SHELL_EXEC": "1", "SHELL_ALLOWLIST": ""}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            with patch("openclaw_agent.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
                result = openclaw_agent.run_shell("sleep 999")
        assert "[timeout]" in result

    def test_returns_error_message_on_exception(self):
        with patch.dict(os.environ, {"ENABLE_SHELL_EXEC": "1", "SHELL_ALLOWLIST": ""}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            with patch("openclaw_agent.subprocess.run", side_effect=OSError("permission denied")):
                result = openclaw_agent.run_shell("cmd")
        assert "[error]" in result

    def test_truncates_output_at_2000_chars(self):
        with patch.dict(os.environ, {"ENABLE_SHELL_EXEC": "1", "SHELL_ALLOWLIST": ""}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            mock_result = MagicMock()
            mock_result.stdout = "x" * 5000
            mock_result.stderr = ""
            with patch("openclaw_agent.subprocess.run", return_value=mock_result):
                result = openclaw_agent.run_shell("cmd")
        assert len(result) == 2000

    def test_executes_command_allowed_by_allowlist(self):
        with patch.dict(os.environ, {"ENABLE_SHELL_EXEC": "1", "SHELL_ALLOWLIST": "echo"}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            mock_result = MagicMock()
            mock_result.stdout = "ok"
            mock_result.stderr = ""
            with patch("openclaw_agent.subprocess.run", return_value=mock_result) as mock_run:
                result = openclaw_agent.run_shell("echo test")
        mock_run.assert_called_once()
        assert result == "ok"


# ---------------------------------------------------------------------------
# ollama_reason()
# ---------------------------------------------------------------------------

class TestOllamaReason:

    def test_returns_response_text(self):
        oc = _import_openclaw()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "do this task"}
        mock_resp.raise_for_status = MagicMock()
        with patch("openclaw_agent.requests.post", return_value=mock_resp):
            result = oc.ollama_reason("list the files")
        assert result == "do this task"

    def test_returns_error_string_on_failure(self):
        oc = _import_openclaw()
        with patch("openclaw_agent.requests.post", side_effect=ConnectionError("down")):
            result = oc.ollama_reason("task")
        assert result.startswith("[Ollama error]")

    def test_prompt_contains_task_text(self):
        oc = _import_openclaw()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()
        with patch("openclaw_agent.requests.post", return_value=mock_resp) as mock_post:
            oc.ollama_reason("my unique task string 12345")
        body = mock_post.call_args[1]["json"]
        assert "my unique task string 12345" in body["prompt"]

    def test_prompt_mentions_shell_disabled_when_exec_off(self):
        oc = _import_openclaw({"ENABLE_SHELL_EXEC": "0"})
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()
        with patch("openclaw_agent.requests.post", return_value=mock_resp) as mock_post:
            oc.ollama_reason("task")
        body = mock_post.call_args[1]["json"]
        assert "disabled" in body["prompt"].lower() or "shell execution is disabled" in body["prompt"]

    def test_prompt_mentions_cmd_format_when_exec_on(self):
        oc = _import_openclaw({"ENABLE_SHELL_EXEC": "1"})
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()
        with patch("openclaw_agent.requests.post", return_value=mock_resp) as mock_post:
            oc.ollama_reason("task")
        body = mock_post.call_args[1]["json"]
        assert "<CMD>" in body["prompt"]

    def test_uses_specified_model(self):
        oc = _import_openclaw()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()
        with patch("openclaw_agent.requests.post", return_value=mock_resp) as mock_post:
            oc.ollama_reason("task", "codellama")
        body = mock_post.call_args[1]["json"]
        assert body["model"] == "codellama"

    def test_returns_fallback_when_no_response_key(self):
        oc = _import_openclaw()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status = MagicMock()
        with patch("openclaw_agent.requests.post", return_value=mock_resp):
            result = oc.ollama_reason("task")
        assert result == "(no response)"


# ---------------------------------------------------------------------------
# handle_task()
# ---------------------------------------------------------------------------

class TestHandleTask:

    def test_routes_to_openclaw_app_when_available(self):
        oc = _import_openclaw()
        with patch("openclaw_agent.openclaw_available", return_value=True), \
             patch("openclaw_agent.openclaw_task", return_value="app result"):
            result = oc.handle_task({"task": "do something"})
        assert "[via OpenClaw.app]" in result
        assert "app result" in result

    def test_falls_back_to_ollama_when_app_unavailable(self):
        oc = _import_openclaw()
        with patch("openclaw_agent.openclaw_available", return_value=False), \
             patch("openclaw_agent.ollama_reason", return_value="ollama response"):
            result = oc.handle_task({"task": "do something"})
        assert result == "ollama response"

    def test_falls_back_to_ollama_when_app_returns_none(self):
        oc = _import_openclaw()
        with patch("openclaw_agent.openclaw_available", return_value=True), \
             patch("openclaw_agent.openclaw_task", return_value=None), \
             patch("openclaw_agent.ollama_reason", return_value="ollama fallback"):
            result = oc.handle_task({"task": "do something"})
        assert result == "ollama fallback"

    def test_extracts_task_from_dict_payload(self):
        oc = _import_openclaw()
        captured_task = []
        def fake_ollama(task, model):
            captured_task.append(task)
            return "ok"
        with patch("openclaw_agent.openclaw_available", return_value=False), \
             patch("openclaw_agent.ollama_reason", side_effect=fake_ollama):
            oc.handle_task({"task": "my task text", "model": "llama3"})
        assert captured_task[0] == "my task text"

    def test_handles_non_dict_payload(self):
        oc = _import_openclaw()
        captured_task = []
        def fake_ollama(task, model):
            captured_task.append(task)
            return "ok"
        with patch("openclaw_agent.openclaw_available", return_value=False), \
             patch("openclaw_agent.ollama_reason", side_effect=fake_ollama):
            oc.handle_task("a plain string task")
        assert captured_task[0] == "a plain string task"

    def test_uses_model_from_payload(self):
        oc = _import_openclaw()
        captured_model = []
        def fake_ollama(task, model):
            captured_model.append(model)
            return "ok"
        with patch("openclaw_agent.openclaw_available", return_value=False), \
             patch("openclaw_agent.ollama_reason", side_effect=fake_ollama):
            oc.handle_task({"task": "t", "model": "mistral"})
        assert captured_model[0] == "mistral"

    def test_executes_cmd_block_when_shell_enabled(self):
        with patch.dict(os.environ, {"ENABLE_SHELL_EXEC": "1", "SHELL_ALLOWLIST": ""}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent

        shell_response = "Here is the command: <CMD>echo hello</CMD>"
        with patch("openclaw_agent.openclaw_available", return_value=False), \
             patch("openclaw_agent.ollama_reason", return_value=shell_response), \
             patch("openclaw_agent.run_shell", return_value="hello") as mock_run:
            result = openclaw_agent.handle_task({"task": "say hi"})
        mock_run.assert_called_once_with("echo hello")
        assert "Command output:" in result
        assert "hello" in result

    def test_does_not_execute_cmd_block_when_shell_disabled(self):
        oc = _import_openclaw({"ENABLE_SHELL_EXEC": "0"})
        shell_response = "Use this: <CMD>rm -rf /</CMD>"
        with patch("openclaw_agent.openclaw_available", return_value=False), \
             patch("openclaw_agent.ollama_reason", return_value=shell_response), \
             patch("openclaw_agent.run_shell") as mock_run:
            result = oc.handle_task({"task": "delete everything"})
        mock_run.assert_not_called()
        # Should just return the raw ollama response
        assert result == shell_response

    def test_returns_empty_task_for_missing_key(self):
        oc = _import_openclaw()
        captured_task = []
        def fake_ollama(task, model):
            captured_task.append(task)
            return "ok"
        with patch("openclaw_agent.openclaw_available", return_value=False), \
             patch("openclaw_agent.ollama_reason", side_effect=fake_ollama):
            oc.handle_task({})  # no "task" key
        assert captured_task[0] == ""


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

class TestModuleConstants:

    def test_shell_exec_disabled_by_default(self):
        env = os.environ.copy()
        env.pop("ENABLE_SHELL_EXEC", None)
        with patch.dict(os.environ, env, clear=True):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            assert openclaw_agent.ENABLE_SHELL_EXEC is False

    def test_shell_exec_enabled_when_env_is_one(self):
        with patch.dict(os.environ, {"ENABLE_SHELL_EXEC": "1"}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            assert openclaw_agent.ENABLE_SHELL_EXEC is True

    def test_shell_exec_disabled_for_non_one_values(self):
        for val in ["0", "true", "yes", "TRUE", "2"]:
            with patch.dict(os.environ, {"ENABLE_SHELL_EXEC": val}, clear=False):
                sys.modules.pop("openclaw_agent", None)
                import openclaw_agent
                assert openclaw_agent.ENABLE_SHELL_EXEC is False, f"Failed for ENABLE_SHELL_EXEC={val!r}"

    def test_shell_allowlist_is_none_when_empty(self):
        with patch.dict(os.environ, {"SHELL_ALLOWLIST": ""}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            assert openclaw_agent.SHELL_ALLOWLIST is None

    def test_shell_allowlist_parsed_correctly(self):
        with patch.dict(os.environ, {"SHELL_ALLOWLIST": "git,ls,docker ps"}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            assert openclaw_agent.SHELL_ALLOWLIST == ["git", "ls", "docker ps"]

    def test_shell_allowlist_strips_whitespace_entries(self):
        with patch.dict(os.environ, {"SHELL_ALLOWLIST": " git , ls "}, clear=False):
            sys.modules.pop("openclaw_agent", None)
            import openclaw_agent
            assert "git" in openclaw_agent.SHELL_ALLOWLIST
            assert "ls" in openclaw_agent.SHELL_ALLOWLIST

    def test_default_reconnect_delay(self):
        oc = _import_openclaw()
        assert oc.RECONNECT_DELAY == 10

    def test_default_heartbeat_every(self):
        oc = _import_openclaw()
        assert oc.HEARTBEAT_EVERY == 10


# ---------------------------------------------------------------------------
# heartbeat() coroutine
# ---------------------------------------------------------------------------

class TestHeartbeat:

    @pytest.mark.asyncio
    async def test_heartbeat_sends_heartbeat_message(self):
        oc = _import_openclaw()
        ws = AsyncMock()
        call_count = 0

        async def fake_sleep(n):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise asyncio.CancelledError()

        with patch("openclaw_agent.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await oc.heartbeat(ws)

        ws.send.assert_called_once()
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["type"] == "HEARTBEAT"
        assert sent["agent"] == "openclaw"

    @pytest.mark.asyncio
    async def test_heartbeat_breaks_on_send_exception(self):
        oc = _import_openclaw()
        ws = AsyncMock()
        ws.send.side_effect = Exception("connection closed")

        async def fast_sleep(n):
            pass

        with patch("openclaw_agent.asyncio.sleep", side_effect=fast_sleep):
            await oc.heartbeat(ws)

        assert ws.send.call_count == 1

    @pytest.mark.asyncio
    async def test_heartbeat_includes_timestamp(self):
        oc = _import_openclaw()
        ws = AsyncMock()
        call_count = 0

        async def fake_sleep(n):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise asyncio.CancelledError()

        with patch("openclaw_agent.asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await oc.heartbeat(ws)

        sent = json.loads(ws.send.call_args[0][0])
        assert isinstance(sent["ts"], float)