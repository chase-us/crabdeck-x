"""Regression: blocking Ollama/task work must not freeze heartbeats."""

from __future__ import annotations

import asyncio
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
        )
        self.assertEqual(result, "done:status")
        ws.send.assert_awaited()
        sent = ws.send.await_args.args[0]
        self.assertIn("TASK_RESULT", sent)
        self.assertIn("done:status", sent)

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


if __name__ == "__main__":
    unittest.main()
