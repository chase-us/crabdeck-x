---
name: crabdeck-event-loop
description: Keep CrabDeck agent asyncio loops non-blocking so gateway watchdogs stay green. Use when adding Ollama calls, HTTP, subprocess, vault I/O, or heartbeat emitters in Hermes, OpenClaw, or the orchestrator.
---

# Event loop integrity

The gateway marks a registered agent `missed_heartbeat` after **20 seconds** of WebSocket silence. Heartbeats are scheduled every **10 seconds** on the same asyncio loop that dispatches `PROMPT` / `TASK`. A blocking `requests.post` (Ollama up to 120s) on that loop starves `heartbeat()` and trips the watchdog.

## Instructions

1. All sync I/O from an `async def` must go through `run_blocking` in `agents/offload.py` (`asyncio.to_thread`).
2. Reject non-callables in `run_blocking` (`TypeError`).
3. Never `time.sleep`, `requests.*`, `urllib.request.urlopen`, or `subprocess.run` directly inside `async def run`, `dispatch_*`, or `heartbeat`.
4. Vault emits (`emit_heartbeat`, `emit_memory`) are sync HTTP — always `await run_blocking(...)`.
5. Tests must not call `threading.Event.wait()` on the asyncio thread. That blocks the loop the same way a hung Ollama call does. Yield with `await asyncio.sleep(0)` (or a short awaitable poll) until the event is set.

## Pattern

```python
from offload import run_blocking
from vault_client import emit_memory

reply = await run_blocking(generate, prompt, model)
await ws.send(json.dumps({"type": "HERMES_RESPONSE", "agent": "hermes", "payload": reply}))
if remember is not None:
    await run_blocking(remember, "hermes", "prompt_result", excerpt, {"model": model})
```

Injectable `generate` / `handle` / `remember` / `emit` keep unit tests off the network.

## Examples

**Correct — heartbeat continues during generate**

```python
async def dispatch_prompt(ws, msg, generate=ollama_generate, remember=emit_memory):
    reply = await run_blocking(generate, prompt, model)
    await ws.send(...)
```

**Incorrect — freezes HELLO/HEARTBEAT**

```python
reply = ollama_generate(prompt, model)  # blocks 120s
```

**Deterministic offload test**

```python
progressed = threading.Event()

def block_until_heartbeat() -> str:
    return "generated" if progressed.wait(timeout=2.0) else "timeout"

async def heartbeat() -> None:
    await asyncio.sleep(0)
    progressed.set()

beat = asyncio.create_task(heartbeat())
result = await run_blocking(block_until_heartbeat)
await beat
```

The worker thread waits on the Event; the loop must run the heartbeat task to release it. Wall-clock tick counts flake under load — do not use them as the proof.

## Performance notes

- `run_blocking` is a thin `asyncio.to_thread` wrapper. Do not add sleeps or retries inside it.
- Gateway `lastSeen` updates on **any** inbound WS frame, not only `HEARTBEAT`. Still send HEARTBEAT every 10s so vault/bHive stay current.
- Orchestrator vault ingest uses `asyncio.to_thread(emit_vault_heartbeat, ...)` after the WS send so a hung vault cannot delay the bus ACK path.

## Troubleshooting

| Symptom | Fix |
| ------- | --- |
| `missed_heartbeat` during a long prompt | Find a sync call in `async def` and wrap with `run_blocking` |
| Offload test hangs 12s then fails | Test thread called `Event.wait()` — poll with `await asyncio.sleep(0)` |
| Heartbeat test needs 10s | Pass `every=0` into `heartbeat(ws, emit=..., every=0)` |
