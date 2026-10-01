"""Run sync I/O off the asyncio event loop.

Ollama generation and OpenClaw task handlers use blocking `requests` /
`subprocess` calls (up to 120s). The gateway watchdog marks an agent
`missed_heartbeat` after 20s of silence. Calling those functions directly
from `async def run()` freezes the loop and starves `heartbeat()`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


async def run_blocking(fn: Callable[..., T], *args: object, **kwargs: object) -> T:
    """Execute a synchronous callable in a worker thread.

    The event loop — including heartbeat coroutines — keeps running
    while `fn` blocks on HTTP or subprocess I/O.
    """
    if not callable(fn):
        raise TypeError("run_blocking requires a callable")
    return await asyncio.to_thread(fn, *args, **kwargs)
