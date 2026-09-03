"""Regression: blocking Ollama/task work must not freeze heartbeats."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
        """Gateway watchdog is 20s; a 120s Ollama POST used to starve HEARTBEAT."""
        ticks: list[float] = []

        async def heartbeat() -> None:
            for _ in range(5):
                await asyncio.sleep(0.04)
                ticks.append(time.monotonic())

        beat = asyncio.create_task(heartbeat())
        started = time.monotonic()
        result = await run_blocking(_sleep_and_return, 0.22, "generated")
        await beat

        self.assertEqual(result, "generated")
        self.assertGreaterEqual(len(ticks), 4)
        self.assertLess(ticks[0] - started, 0.12)

    async def test_sync_call_on_loop_starves_heartbeat(self) -> None:
        """Documents the pre-fix failure mode: a blocking call in async code."""
        ticks: list[float] = []

        async def heartbeat() -> None:
            for _ in range(5):
                await asyncio.sleep(0.04)
                ticks.append(time.monotonic())

        beat = asyncio.create_task(heartbeat())
        _sleep_and_return(0.22, "blocked")
        # The loop could not run heartbeat while the thread was blocked.
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


if __name__ == "__main__":
    unittest.main()
