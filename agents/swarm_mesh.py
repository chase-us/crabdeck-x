"""Swarm mesh protocol — peer collaboration, delegation, and shared RAG context."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from rag import inject_rag, retrieve_context, store_session_context

# Mesh-capable agents in CrabDeck
MESH_AGENTS = frozenset({"hermes", "openclaw", "orchestrator", "swarm"})

# Message types routed by the gateway swarm layer
MSG_SWARM_GOAL = "SWARM_GOAL"
MSG_SWARM_DELEGATE = "SWARM_DELEGATE"
MSG_SWARM_BROADCAST = "SWARM_BROADCAST"
MSG_SWARM_PEER_QUERY = "SWARM_PEER_QUERY"
MSG_SWARM_PEER_RESPONSE = "SWARM_PEER_RESPONSE"
MSG_SWARM_CONTEXT = "SWARM_CONTEXT"
MSG_SWARM_RESULT = "SWARM_RESULT"
MSG_SWARM_MESH_STATUS = "SWARM_MESH_STATUS"
MSG_SWARM_ACK = "SWARM_ACK"


@dataclass
class SwarmTask:
    task_id: str
    goal: str
    session_id: str
    model: str = "llama3"
    rag_context: str = ""
    rag_hits: list[dict[str, Any]] = field(default_factory=list)
    subtasks: list[dict[str, Any]] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


def new_session_id() -> str:
    return f"swarm-{uuid.uuid4().hex[:12]}"


def new_task_id() -> str:
    return f"task-{uuid.uuid4().hex[:10]}"


def decompose_goal(goal: str) -> list[dict[str, str]]:
    """Split a collaborative goal into agent-specific subtasks."""
    goal = goal.strip()
    if not goal:
        return []
    # Hermes handles reasoning/analysis; OpenClaw handles execution/system work.
    return [
        {
            "agent": "hermes",
            "role": "reason",
            "instruction": (
                f"Analyze and plan how to accomplish this goal. "
                f"Provide clear reasoning and recommendations:\n{goal}"
            ),
        },
        {
            "agent": "openclaw",
            "role": "execute",
            "instruction": (
                f"Execute or operationalize this goal on the local system. "
                f"Use prior Hermes reasoning when available:\n{goal}"
            ),
        },
    ]


def build_delegate_message(
    *,
    task_id: str,
    session_id: str,
    target: str,
    instruction: str,
    rag_context: str,
    model: str,
    from_agent: str = "swarm",
) -> dict[str, Any]:
    if target not in MESH_AGENTS and target not in {"hermes", "openclaw"}:
        raise ValueError(f"unknown mesh target: {target!r}")
    return {
        "type": MSG_SWARM_DELEGATE,
        "task_id": task_id,
        "session_id": session_id,
        "target": target,
        "from": from_agent,
        "payload": {
            "instruction": instruction,
            "model": model,
            "rag_context": rag_context,
        },
    }


def build_peer_query(
    *,
    task_id: str,
    session_id: str,
    target: str,
    question: str,
    from_agent: str,
) -> dict[str, Any]:
    return {
        "type": MSG_SWARM_PEER_QUERY,
        "task_id": task_id,
        "session_id": session_id,
        "target": target,
        "from": from_agent,
        "payload": {"question": question},
    }


def build_peer_response(
    *,
    task_id: str,
    session_id: str,
    answer: str,
    from_agent: str,
    in_reply_to: str | None = None,
) -> dict[str, Any]:
    return {
        "type": MSG_SWARM_PEER_RESPONSE,
        "task_id": task_id,
        "session_id": session_id,
        "from": from_agent,
        "in_reply_to": in_reply_to,
        "payload": {"answer": answer},
    }


def build_swarm_result(task: SwarmTask, synthesis: str) -> dict[str, Any]:
    return {
        "type": MSG_SWARM_RESULT,
        "task_id": task.task_id,
        "session_id": task.session_id,
        "payload": {
            "goal": task.goal,
            "synthesis": synthesis,
            "subtasks": task.subtasks,
            "results": task.results,
            "rag_hit_count": len(task.rag_hits),
        },
    }


def build_mesh_status(online: dict[str, str]) -> dict[str, Any]:
    return {
        "type": MSG_SWARM_MESH_STATUS,
        "agents": list(MESH_AGENTS),
        "online": online,
        "ts": time.time(),
    }


async def prepare_swarm_task(goal: str, model: str = "llama3", session_id: str | None = None) -> SwarmTask:
    """Retrieve RAG context and decompose a goal for mesh collaboration."""
    sid = session_id or new_session_id()
    ctx, hits = retrieve_context(goal)
    task = SwarmTask(
        task_id=new_task_id(),
        goal=goal,
        session_id=sid,
        model=model,
        rag_context=ctx,
        rag_hits=hits,
        subtasks=decompose_goal(goal),
    )
    store_session_context(sid, {
        "goal": goal,
        "task_id": task.task_id,
        "rag_hit_count": len(hits),
        "subtasks": task.subtasks,
        "created_at": task.created_at,
    })
    return task


def synthesize_results(task: SwarmTask) -> str:
    """Merge peer agent outputs into a single swarm response."""
    parts = [f"Swarm goal: {task.goal}"]
    if task.rag_context:
        parts.append(f"\nRAG context used ({len(task.rag_hits)} hits).")
    for agent, result in task.results.items():
        parts.append(f"\n--- {agent} ---\n{result}")
    if len(task.results) < 2:
        parts.append("\n(awaiting peer agents…)")
    return "\n".join(parts)


def parse_delegate_payload(msg: dict[str, Any]) -> tuple[str, str, str, str]:
    """Extract instruction, model, rag_context, session_id from a delegate message."""
    payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
    instruction = str(payload.get("instruction", ""))
    model = str(payload.get("model", "llama3"))
    rag_context = str(payload.get("rag_context", ""))
    session_id = str(msg.get("session_id", ""))
    return instruction, model, rag_context, session_id


def enrich_with_rag(instruction: str, rag_context: str) -> str:
    """Inject mesh-shared RAG context, or retrieve fresh context if absent."""
    if rag_context.strip():
        return inject_rag(instruction, rag_context)
    ctx, _ = retrieve_context(instruction)
    return inject_rag(instruction, ctx)


def handle_peer_query_payload(msg: dict[str, Any]) -> str:
    payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
    return str(payload.get("question", "")).strip()


PeerHandler = Callable[[dict[str, Any]], str]
