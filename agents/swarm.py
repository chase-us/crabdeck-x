"""Swarm mesh peer logic shared by Hermes, OpenClaw, and the orchestrator.

Every peer receives the same SWARM_ROUND frame from the gateway:

    {
      "session_id": "...", "goal": "...", "model": "llama3" | null,
      "round": 1, "max_rounds": 2,
      "peers": ["hermes", "openclaw", "orchestrator"],
      "context": [{"id", "text", "agent", "kind", "score"}, ...],   # gateway RAG
      "contributions": {"openclaw": "...", ...}                    # previous round
    }

A peer answers with SWARM_CONTRIBUTION {session_id, round, text}. Hermes also
answers SWARM_SYNTHESIZE with SWARM_SYNTHESIS. MESH frames are direct
peer-to-peer messages ({intent: "ask"|"tell", text}).

Retrieval is two-layer RAG: the gateway seeds the round with vault hits for the
goal, and each peer may add its own retrieval (role-specific query) before
prompting. All prompting/retrieval runs through `run_blocking` so heartbeats
keep ticking under the 20s gateway watchdog.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from offload import run_blocking

MAX_TEXT = 6000
MAX_CONTEXT_HITS = 6
MAX_CONTEXT_CHARS = 600
MAX_PEER_CHARS = 1200
MAX_WORDS_HINT = 220

ROLE_BRIEFS: dict[str, str] = {
    "hermes": (
        "Hermes, the LLM messenger. You reason broadly, weigh trade-offs, and write the "
        "clearest explanation. You also synthesize the swarm's final answer."
    ),
    "openclaw": (
        "OpenClaw, the sovereign system agent. You focus on concrete actions, the exact "
        "commands an operator would run (describe them — do NOT execute anything during a "
        "swarm round), failure modes, and security risks."
    ),
    "orchestrator": (
        "the Orchestrator, the health tracker. You report live agent status, operational "
        "constraints, and whether the swarm is healthy enough to act."
    ),
}


def _clamp(text: object, limit: int) -> str:
    s = text if isinstance(text, str) else str(text)
    return s if len(s) <= limit else s[:limit]


def normalize_round(msg: object) -> dict[str, Any] | None:
    """Validate a SWARM_ROUND / SWARM_SYNTHESIZE payload. Returns None when unusable."""
    raw = msg.get("payload") if isinstance(msg, dict) else None
    if not isinstance(raw, dict):
        return None
    session_id = raw.get("session_id")
    goal = raw.get("goal")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    if not isinstance(goal, str) or not goal.strip():
        return None
    rnd = raw.get("round")
    max_rounds = raw.get("max_rounds")
    model = raw.get("model")
    peers = raw.get("peers")
    contributions = raw.get("contributions")
    transcript = raw.get("transcript")
    return {
        "session_id": session_id.strip(),
        "goal": goal.strip()[:4000],
        "round": rnd if isinstance(rnd, int) and rnd >= 1 else 1,
        "max_rounds": max_rounds if isinstance(max_rounds, int) and max_rounds >= 1 else 1,
        "model": model.strip() if isinstance(model, str) and model.strip() else None,
        "peers": [p for p in peers if isinstance(p, str)] if isinstance(peers, list) else [],
        "context": trim_context(raw.get("context")),
        "contributions": {
            k: _clamp(v, MAX_PEER_CHARS)
            for k, v in contributions.items()
            if isinstance(k, str) and isinstance(v, str) and v.strip()
        } if isinstance(contributions, dict) else {},
        "transcript": [
            {
                "round": t.get("round") if isinstance(t.get("round"), int) else 0,
                "agent": t.get("agent") if isinstance(t.get("agent"), str) else "peer",
                "text": _clamp(t.get("text"), MAX_PEER_CHARS),
            }
            for t in transcript
            if isinstance(t, dict) and isinstance(t.get("text"), str) and t["text"].strip()
        ] if isinstance(transcript, list) else [],
    }


def normalize_mesh(msg: object) -> dict[str, Any] | None:
    """Validate an inbound MESH frame → {from, intent, text, session_id}."""
    if not isinstance(msg, dict):
        return None
    sender = msg.get("from")
    if not isinstance(sender, str) or not sender.strip():
        return None
    raw = msg.get("payload")
    payload = raw if isinstance(raw, dict) else {"text": raw}
    text = payload.get("text")
    text = text.strip() if isinstance(text, str) else str(text or "").strip()
    if not text:
        return None
    session_id = payload.get("session_id")
    return {
        "from": sender.strip(),
        "intent": "ask" if payload.get("intent") == "ask" else "tell",
        "text": text[:MAX_TEXT],
        "session_id": session_id.strip() if isinstance(session_id, str) and session_id.strip() else None,
    }


def trim_context(hits: object, limit: int = MAX_CONTEXT_HITS) -> list[dict[str, Any]]:
    if not isinstance(hits, list):
        return []
    out: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        text = hit.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        meta = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
        agent = hit.get("agent") if isinstance(hit.get("agent"), str) else meta.get("agent", "unknown")
        kind = hit.get("kind") if isinstance(hit.get("kind"), str) else meta.get("kind", "memory")
        score = hit.get("score")
        out.append({
            "id": hit.get("id") if isinstance(hit.get("id"), str) else "",
            "text": text.strip()[:MAX_CONTEXT_CHARS],
            "agent": agent if isinstance(agent, str) else "unknown",
            "kind": kind if isinstance(kind, str) else "memory",
            "score": round(float(score), 4) if isinstance(score, (int, float)) else 0.0,
        })
        if len(out) >= limit:
            break
    return out


def merge_context(*sources: object, limit: int = MAX_CONTEXT_HITS) -> list[dict[str, Any]]:
    """Union gateway + local retrieval, dedupe by id/text, rank by score."""
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for src in sources:
        for hit in trim_context(src, limit=50):
            key = hit["id"] or hit["text"]
            if key in seen:
                continue
            seen.add(key)
            merged.append(hit)
    merged.sort(key=lambda h: h["score"], reverse=True)
    return merged[:limit]


def format_context(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "(no prior memory matched this goal)"
    lines = []
    for i, hit in enumerate(hits, 1):
        lines.append(f"[{i}] ({hit['agent']}/{hit['kind']} · score {hit['score']:.2f}) {hit['text']}")
    return "\n".join(lines)


def format_peers(contributions: dict[str, str], exclude: str | None = None) -> str:
    rows = [f"- {role}: {text}" for role, text in contributions.items() if role != exclude]
    return "\n".join(rows) if rows else "(no peer contributions yet)"


def build_round_prompt(role: str, rnd: dict[str, Any], extra_context: object = None) -> str:
    brief = ROLE_BRIEFS.get(role, f"{role}, a CrabDeck swarm peer.")
    context = merge_context(rnd.get("context"), extra_context)
    round_no = rnd["round"]
    max_rounds = rnd["max_rounds"]
    peers = ", ".join(p for p in rnd.get("peers", []) if p != role) or "none"
    if round_no == 1:
        instruction = (
            "This is the opening round. Give your perspective on the goal from your role. "
            "Cite retrieved memory by its [n] index when you rely on it."
        )
    elif round_no < max_rounds:
        instruction = (
            "Read your peers' contributions. Critique, correct, and build on them. "
            "Converge toward a shared plan; call out disagreements explicitly."
        )
    else:
        instruction = (
            "Final round. State your concluding position in a form the synthesizer can merge: "
            "decisions, actions, open risks."
        )
    return (
        f"You are {brief}\n"
        f"You are one node in the CrabDeck swarm mesh. Peers in this session: {peers}.\n\n"
        f"GOAL:\n{rnd['goal']}\n\n"
        f"RETRIEVED MEMORY (Shell Cracked RAG):\n{format_context(context)}\n\n"
        f"PEER CONTRIBUTIONS FROM ROUND {max(round_no - 1, 0)}:\n"
        f"{format_peers(rnd.get('contributions', {}), exclude=role)}\n\n"
        f"ROUND {round_no} OF {max_rounds}. {instruction}\n"
        f"Respond in under {MAX_WORDS_HINT} words. Plain text.\n\nResponse:"
    )


def build_synthesis_prompt(rnd: dict[str, Any]) -> str:
    transcript = rnd.get("transcript", [])
    body = "\n".join(f"[round {t['round']} · {t['agent']}] {t['text']}" for t in transcript) or "(empty)"
    return (
        "You are Hermes, synthesizer for the CrabDeck swarm mesh.\n"
        f"GOAL:\n{rnd['goal']}\n\n"
        f"RETRIEVED MEMORY:\n{format_context(rnd.get('context', []))}\n\n"
        f"SWARM TRANSCRIPT:\n{body}\n\n"
        "Merge the peers' positions into one answer: 1) decision, 2) concrete next actions "
        "(attribute each to the peer best placed to do it), 3) open risks. Note where peers "
        "disagreed. Under 300 words. Plain text.\n\nSynthesis:"
    )


def build_mesh_reply_prompt(role: str, mesh: dict[str, Any]) -> str:
    brief = ROLE_BRIEFS.get(role, f"{role}, a CrabDeck swarm peer.")
    return (
        f"You are {brief}\n"
        f"Peer '{mesh['from']}' asked you directly over the mesh:\n{mesh['text']}\n\n"
        "Answer in under 120 words. Plain text.\n\nAnswer:"
    )


async def dispatch_swarm_round(
    ws: Any,
    msg: object,
    *,
    agent: str,
    generate: Callable[..., str],
    retrieve: Callable[[str, int], list[dict[str, Any]]] | None = None,
    default_model: str = "llama3",
) -> str | None:
    """Answer a SWARM_ROUND with a SWARM_CONTRIBUTION, all blocking work off-loop."""
    if not callable(generate):
        raise TypeError("dispatch_swarm_round requires a callable generate")
    if retrieve is not None and not callable(retrieve):
        raise TypeError("retrieve must be callable or None")
    rnd = normalize_round(msg)
    if rnd is None:
        return None
    local_hits: list[dict[str, Any]] = []
    if retrieve is not None:
        try:
            local_hits = await run_blocking(retrieve, rnd["goal"], 5)
        except Exception:
            local_hits = []
    prompt = build_round_prompt(agent, rnd, local_hits)
    model = rnd["model"] or default_model
    print(f"[{agent}] SWARM_ROUND {rnd['round']}/{rnd['max_rounds']} ({rnd['session_id'][:8]}) rag={len(rnd['context']) + len(local_hits)}")
    text = await run_blocking(generate, prompt, model)
    text = _clamp(str(text).strip() or "(no contribution)", MAX_TEXT)
    await ws.send(json.dumps({
        "type": "SWARM_CONTRIBUTION",
        "agent": agent,
        "payload": {"session_id": rnd["session_id"], "round": rnd["round"], "text": text},
    }))
    return text


async def dispatch_swarm_synthesize(
    ws: Any,
    msg: object,
    *,
    agent: str,
    generate: Callable[..., str],
    default_model: str = "llama3",
) -> str | None:
    """Answer SWARM_SYNTHESIZE with SWARM_SYNTHESIS (Hermes only)."""
    if not callable(generate):
        raise TypeError("dispatch_swarm_synthesize requires a callable generate")
    rnd = normalize_round(msg)
    if rnd is None:
        return None
    prompt = build_synthesis_prompt(rnd)
    model = rnd["model"] or default_model
    print(f"[{agent}] SWARM_SYNTHESIZE ({rnd['session_id'][:8]}) {len(rnd['transcript'])} contributions")
    text = await run_blocking(generate, prompt, model)
    text = _clamp(str(text).strip() or "(no synthesis)", MAX_TEXT)
    await ws.send(json.dumps({
        "type": "SWARM_SYNTHESIS",
        "agent": agent,
        "payload": {"session_id": rnd["session_id"], "text": text},
    }))
    return text


async def dispatch_mesh(
    ws: Any,
    msg: object,
    *,
    agent: str,
    generate: Callable[..., str] | None,
    remember: Callable[..., bool] | None = None,
    default_model: str = "llama3",
) -> str | None:
    """Handle a direct MESH frame. `ask` gets a generated `tell` reply; `tell` is remembered.

    Replies are always `tell`, so two peers cannot ping-pong forever.
    """
    if generate is not None and not callable(generate):
        raise TypeError("generate must be callable or None")
    if remember is not None and not callable(remember):
        raise TypeError("remember must be callable or None")
    mesh = normalize_mesh(msg)
    if mesh is None:
        return None
    print(f"[{agent}] MESH {mesh['intent']} from {mesh['from']}: {mesh['text'][:60]}")
    if mesh["intent"] == "tell":
        if remember is not None:
            await run_blocking(
                remember, agent, "mesh_note",
                f"from {mesh['from']}: {mesh['text'][:4000]}",
                {"from": mesh["from"], "session_id": mesh["session_id"] or ""},
            )
        return None
    if generate is None:
        return None
    reply = await run_blocking(generate, build_mesh_reply_prompt(agent, mesh), default_model)
    reply = _clamp(str(reply).strip() or "(no answer)", MAX_TEXT)
    payload: dict[str, Any] = {"intent": "tell", "text": reply}
    if mesh["session_id"]:
        payload["session_id"] = mesh["session_id"]
    await ws.send(json.dumps({"type": "MESH", "agent": agent, "to": mesh["from"], "payload": payload}))
    return reply
