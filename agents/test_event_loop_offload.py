"""Regression: blocking Ollama/task work must not freeze heartbeats."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hermes_agent import _tool_request_payload, dispatch_tool_request
from offload import run_blocking


def _sleep_and_return(seconds: float, value: str) -> str:
    time.sleep(seconds)
    return value


class RunBlockingTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_non_callable(self) -> None:
        with self.assertRaises(TypeError):
            await run_blocking("not-a-function")  # type: ignore[arg-type]

    async def test_returns_wrapped_result(self) -> None:
        result = await run_blocking(_sleep_and_return, 0.01, "ok")
        self.assertEqual(result, "ok")

    async def test_heartbeat_ticks_during_blocking_call(self) -> None:
        """Offload is proven when the worker waits on a heartbeat-set Event.

        Wall-clock tick counts flake under runner load. The heartbeat
        coroutine must run on the loop to release the worker thread.
        """
        progressed = threading.Event()

        def block_until_heartbeat() -> str:
            return "generated" if progressed.wait(timeout=2.0) else "timeout"

        async def heartbeat() -> None:
            await asyncio.sleep(0)
            progressed.set()

        beat = asyncio.create_task(heartbeat())
        result = await run_blocking(block_until_heartbeat)
        await beat
        self.assertEqual(result, "generated")
        self.assertTrue(progressed.is_set())

    async def test_sync_call_on_loop_starves_heartbeat(self) -> None:
        """Documents the pre-fix failure mode: a blocking call in async code."""
        ticks: list[float] = []

        async def heartbeat() -> None:
            await asyncio.sleep(0)
            ticks.append(time.monotonic())

        beat = asyncio.create_task(heartbeat())
        _sleep_and_return(0.05, "blocked")
        self.assertEqual(ticks, [])
        beat.cancel()
        try:
            await beat
        except asyncio.CancelledError:
            pass


class AgentDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_hermes_prompt_uses_offload(self) -> None:
        from hermes_agent import dispatch_prompt

        ws = AsyncMock()
        reply = await dispatch_prompt(
            ws,
            {"payload": {"prompt": "ping", "model": "llama3"}},
            generate=lambda prompt, model: f"{model}:{prompt}",
            remember=None,
        )
        self.assertEqual(reply, "llama3:ping")
        ws.send.assert_awaited()
        sent = ws.send.await_args.args[0]
        self.assertIn("HERMES_RESPONSE", sent)
        self.assertIn("llama3:ping", sent)

    async def test_openclaw_task_uses_offload(self) -> None:
        from openclaw_agent import dispatch_task

        ws = AsyncMock()
        result = await dispatch_task(
            ws,
            {"payload": {"task": "status"}},
            handle=lambda payload: f"done:{payload['task']}",
            remember=None,
        )
        self.assertEqual(result, "done:status")
        ws.send.assert_awaited()
        sent = ws.send.await_args.args[0]
        self.assertIn("TASK_RESULT", sent)
        self.assertIn("done:status", sent)

    async def test_hermes_swarm_synthesis_returns_final_result(self) -> None:
        from hermes_agent import dispatch_swarm_synthesis

        ws = AsyncMock()
        result = await dispatch_swarm_synthesis(
            ws,
            {"payload": {
                "taskId": "mesh-1",
                "model": "llama3",
                "results": {"hermes": "reasoning", "openclaw": "systems"},
            }},
            generate=lambda prompt, model: f"{model}:{prompt.splitlines()[-1]}",
        )
        self.assertEqual(result, "llama3:Final answer:")
        sent = ws.send.await_args.args[0]
        self.assertIn("SWARM_SYNTHESIS_RESULT", sent)
        self.assertIn("mesh-1", sent)

    async def test_hermes_swarm_assignment_retrieves_context(self) -> None:
        from hermes_agent import dispatch_swarm_assignment

        ws = AsyncMock()
        result = await dispatch_swarm_assignment(
            ws,
            {"payload": {"taskId": "mesh-2", "task": "diagnose", "model": "llama3"}},
            generate=lambda prompt, model: "contribution" if "retrieved note" in prompt else "missing context",
            retrieve=lambda query, limit: [{"text": "retrieved note"}],
            remember=None,
        )
        self.assertEqual(result, "contribution")
        sent = ws.send.await_args.args[0]
        self.assertIn("SWARM_RESULT", sent)
        self.assertIn("mesh-2", sent)

    async def test_tool_request_accepts_non_dict_payload(self) -> None:
        ws = AsyncMock()
        result = await dispatch_tool_request(
            ws,
            {"payload": "not-a-dict"},
            generate=lambda prompt, model=None: prompt,
        )
        self.assertIn("Tool call:", result)
        sent = ws.send.await_args.args[0]
        self.assertIn('"tool": "unknown"', sent)

    async def test_tool_request_rejects_blank_tool_name(self) -> None:
        normalized = _tool_request_payload({"payload": {"tool": "  ", "arg": 1}})
        self.assertEqual(normalized["tool"], "unknown")
        self.assertEqual(normalized["arg"], 1)


class ToolPayloadTests(unittest.TestCase):
    def test_missing_msg_is_unknown(self) -> None:
        self.assertEqual(_tool_request_payload("bad")["tool"], "unknown")

    def test_list_payload_is_wrapped(self) -> None:
        out = _tool_request_payload({"payload": ["x"]})
        self.assertEqual(out["tool"], "unknown")
        self.assertEqual(out["raw"], ["x"])


class VaultClientContractTests(unittest.TestCase):
    def test_heartbeat_payload_includes_slot(self) -> None:
        from vault_client import heartbeat_payload, minute_slot

        ts = 1_700_000_120.0
        payload = heartbeat_payload("Hermes", ts)
        self.assertEqual(payload["type"], "HEARTBEAT")
        self.assertEqual(payload["agent"], "hermes")
        self.assertEqual(payload["bhive_slot"], minute_slot(ts))

    def test_heartbeat_payload_rejects_unknown_agent(self) -> None:
        from vault_client import heartbeat_payload

        with self.assertRaises(ValueError):
            heartbeat_payload("not-a-swarm-member", 10.0)

    def test_emit_heartbeat_rejects_bad_types(self) -> None:
        from vault_client import emit_heartbeat

        self.assertFalse(emit_heartbeat("", 1.0, 0))
        self.assertFalse(emit_heartbeat("hermes", "now", 0))  # type: ignore[arg-type]
        self.assertFalse(emit_heartbeat("stranger", 1.0, 0))

    def test_emit_memory_rejects_blank_text(self) -> None:
        from vault_client import emit_memory

        self.assertFalse(emit_memory("hermes", "prompt_result", "   "))


class HeartbeatOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_hermes_heartbeat_emits_slot_and_vault_off_loop(self) -> None:
        from hermes_agent import heartbeat

        sent: list[str] = []
        vault_calls: list[tuple] = []
        progressed = threading.Event()

        class _WS:
            async def send(self, raw: str) -> None:
                sent.append(raw)

        def emit(agent: str, ts: float, slot: int, source: str) -> bool:
            vault_calls.append((agent, ts, slot, source))
            progressed.set()
            return True

        task = asyncio.create_task(heartbeat(_WS(), emit=emit, every=0))
        deadline = time.monotonic() + 2.0
        while not progressed.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0)
        self.assertTrue(progressed.is_set())
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.assertEqual(len(sent), 1)
        body = json.loads(sent[0])
        self.assertEqual(body["type"], "HEARTBEAT")
        self.assertEqual(body["agent"], "hermes")
        self.assertIn("bhive_slot", body)
        self.assertEqual(vault_calls[0][0], "hermes")
        self.assertEqual(vault_calls[0][3], "agent")

    async def test_prompt_remember_runs_off_loop(self) -> None:
        from hermes_agent import dispatch_prompt

        remembered: list[tuple] = []
        ws = AsyncMock()
        await dispatch_prompt(
            ws,
            {"payload": {"prompt": "status", "model": "llama3"}},
            generate=lambda prompt, model: "green",
            remember=lambda *args: remembered.append(args) or True,
        )
        self.assertEqual(len(remembered), 1)
        self.assertEqual(remembered[0][0], "hermes")
        self.assertEqual(remembered[0][1], "prompt_result")
        self.assertIn("status", remembered[0][2])


if __name__ == "__main__":
    unittest.main()
