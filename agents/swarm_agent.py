"""
Swarm Coordinator Agent — CrabDeck mesh orchestrator with RAG.

Connects to the gateway as `swarm`, receives SWARM_GOAL events, retrieves
shared RAG context from Shell Cracked, delegates subtasks to Hermes and
OpenClaw, collects peer responses, and publishes SWARM_RESULT.

Run:
    pip install -r requirements.txt
    python swarm_agent.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import requests
import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from offload import run_blocking
from rag import inject_rag, retrieve_context
from swarm_mesh import (
    MSG_SWARM_DELEGATE,
    MSG_SWARM_GOAL,
    MSG_SWARM_PEER_RESPONSE,
    MSG_SWARM_RESULT,
    SwarmTask,
    build_delegate_message,
    build_peer_response,
    build_swarm_result,
    enrich_with_rag,
    new_session_id,
    new_task_id,
    prepare_swarm_task,
    synthesize_results,
)
from vault_client import emit_heartbeat, emit_memory, heartbeat_payload

GATEWAY_URL = os.environ.get("GATEWAY_URL", "ws://localhost:8765")
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "llama3")
HEARTBEAT_EVERY = 10
RECONNECT_DELAY = 5
DELEGATE_TIMEOUT = 90.0

_active_tasks: dict[str, SwarmTask] = {}
_pending_peers: dict[str, asyncio.Future[str]] = {}


def ollama_synthesize(goal: str, results: dict[str, str], model: str = DEFAULT_MODEL) -> str:
    """Use Ollama to synthesize peer agent outputs into a final swarm answer."""
    parts = "\n\n".join(f"### {agent}\n{text}" for agent, text in results.items())
    prompt = (
        f"You are the CrabDeck Swarm Coordinator. Synthesize these agent outputs "
        f"into one clear, actionable answer for the operator.\n\n"
        f"Goal: {goal}\n\nAgent outputs:\n{parts}\n\nSynthesis:"
    )
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=90,
        )
        r.raise_for_status()
        return r.json().get("response", synthesize_results(
            SwarmTask(task_id="", goal=goal, session_id="", results=results)
        ))
    except Exception as exc:
        task = SwarmTask(task_id="", goal=goal, session_id="", results=results)
        return f"{synthesize_results(task)}\n\n[synthesis fallback: {exc}]"


async def dispatch_swarm_goal(ws, msg: dict) -> None:
    """Handle SWARM_GOAL — RAG retrieve, delegate, synthesize."""
    payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
    goal = str(payload.get("goal", "")).strip()
    if not goal:
        await ws.send(json.dumps({
            "type": MSG_SWARM_RESULT,
            "payload": {"error": "empty goal"},
        }))
        return

    model = str(payload.get("model", DEFAULT_MODEL))
    session_id = str(msg.get("session_id") or payload.get("session_id") or new_session_id())
    task = await prepare_swarm_task(goal, model=model, session_id=session_id)
    _active_tasks[task.task_id] = task

    print(f"[Swarm] goal={goal[:80]} session={task.session_id} rag_hits={len(task.rag_hits)}")

    # Broadcast shared RAG context to mesh peers
    await ws.send(json.dumps({
        "type": "SWARM_CONTEXT",
        "task_id": task.task_id,
        "session_id": task.session_id,
        "payload": {"rag_context": task.rag_context, "goal": goal},
    }))

    # Delegate subtasks to Hermes and OpenClaw sequentially (collect peer results)
    for sub in task.subtasks:
        target = sub["agent"]
        instruction = sub["instruction"]
        delegate = build_delegate_message(
            task_id=task.task_id,
            session_id=task.session_id,
            target=target,
            instruction=instruction,
            rag_context=task.rag_context,
            model=model,
            from_agent="swarm",
        )
        peer_key = f"{task.task_id}:{target}"
        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        _pending_peers[peer_key] = future
        await ws.send(json.dumps(delegate))
        try:
            result = await asyncio.wait_for(future, timeout=DELEGATE_TIMEOUT)
            task.results[target] = result
        except asyncio.TimeoutError:
            task.results[target] = f"[timeout] {target} did not respond within {DELEGATE_TIMEOUT}s"
        finally:
            _pending_peers.pop(peer_key, None)

    synthesis = await run_blocking(ollama_synthesize, goal, task.results, model)
    result_msg = build_swarm_result(task, synthesis)
    await ws.send(json.dumps(result_msg))

    excerpt = f"goal: {goal}\n---\n{synthesis[:4000]}"
    await run_blocking(
        emit_memory,
        "swarm",
        "swarm_result",
        excerpt,
        {"task_id": task.task_id, "session_id": task.session_id, "agents": list(task.results.keys())},
    )
    _active_tasks.pop(task.task_id, None)


def handle_peer_response(msg: dict) -> None:
    """Resolve a pending peer future when an agent responds."""
    task_id = str(msg.get("task_id", ""))
    from_agent = str(msg.get("from", ""))
    payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
    answer = str(payload.get("answer", payload.get("result", "")))
    key = f"{task_id}:{from_agent}"
    future = _pending_peers.get(key)
    if future and not future.done():
        future.set_result(answer)


async def run() -> None:
    print("[Swarm] Starting swarm mesh coordinator…")
    if not GATEWAY_TOKEN:
        print("[Swarm] ⚠ GATEWAY_TOKEN not set — only OK on localhost dev.")

    while True:
        try:
            async with websockets.connect(GATEWAY_URL) as ws:
                hello = {"type": "HELLO", "client": "swarm", "version": "2.3-swarm"}
                if GATEWAY_TOKEN:
                    hello["token"] = GATEWAY_TOKEN
                await ws.send(json.dumps(hello))

                ack_raw = await asyncio.wait_for(ws.recv(), timeout=5)
                ack = json.loads(ack_raw)
                if ack.get("type") == "ERROR":
                    print(f"[Swarm] ❌ Gateway rejected handshake: {ack.get('message')}")
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue

                print("[Swarm] Connected to Gateway ✅")
                heartbeat_task = asyncio.create_task(heartbeat(ws))

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    mtype = msg.get("type")
                    if mtype == MSG_SWARM_GOAL:
                        asyncio.create_task(dispatch_swarm_goal(ws, msg))
                    elif mtype == MSG_SWARM_PEER_RESPONSE:
                        handle_peer_response(msg)

                heartbeat_task.cancel()

        except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as exc:
            print(f"[Swarm] Gateway disconnected: {exc} — retrying in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)
        except asyncio.TimeoutError:
            print(f"[Swarm] Handshake timeout — retrying in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)
        except Exception as exc:
            print(f"[Swarm] Unexpected error: {exc} — retrying in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)


async def heartbeat(ws, emit=emit_heartbeat, every=HEARTBEAT_EVERY) -> None:
    while True:
        await asyncio.sleep(every)
        try:
            ts = time.time()
            payload = heartbeat_payload("swarm", ts)
            await ws.send(json.dumps(payload))
            if emit is not None:
                await run_blocking(emit, payload["agent"], payload["ts"], payload["bhive_slot"], "agent")
        except Exception:
            break


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[Swarm] Stopped.")
