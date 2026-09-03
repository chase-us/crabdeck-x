"""Swarm behaviours shared by every CrabDeck peer.

Pure, dependency-free, and deliberately not LLM-backed: a peer must be able to
decide whether to bid, and the swarm must be able to agree on an answer, even
with Ollama down. Nothing here performs I/O.

`consensus` mirrors `gateway/mesh.js quorum` — same token clustering, same
threshold — so a peer's local view of agreement matches the gateway's.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# Capability vocabulary. Peers advertise these at HELLO; contracts request them.
CAP_REASONING = "reasoning"
CAP_RETRIEVAL = "retrieval"
CAP_SYSTEM = "system"
CAP_MEMORY = "memory"
CAP_SUMMARIZE = "summarize"
CAP_COORDINATE = "coordinate"

KNOWN_CAPABILITIES = frozenset({
    CAP_REASONING, CAP_RETRIEVAL, CAP_SYSTEM, CAP_MEMORY, CAP_SUMMARIZE,
    CAP_COORDINATE, "llm", "exec", "inspect", "generation",
})

CONSENSUS_THRESHOLD = 0.45
MIN_VOTES = 2
MAX_SUBTASKS = 4

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+.#-]*")

# Routing hints for decomposition. Order matters: the first capability whose
# cues appear in the task claims the subtask.
_ROUTING = (
    (CAP_SYSTEM, (
        "disk", "process", "cpu", "memory usage", "port", "file", "directory",
        "shell", "command", "uptime", "network", "install", "service", "host",
        "log", "path", "environment",
    )),
    (CAP_RETRIEVAL, (
        "recall", "remember", "history", "previously", "earlier", "last time",
        "what did", "who said", "context", "vault", "memory", "past", "before",
        "prior", "documented", "notes",
    )),
)


def tokenize(text: str) -> set[str]:
    if not isinstance(text, str):
        return set()
    return set(_TOKEN_RE.findall(text.lower()))


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    shared = len(a & b)
    return shared / (len(a) + len(b) - shared)


def normalize_capabilities(raw: Iterable[str] | None) -> list[str]:
    """Lowercase, dedupe, and drop anything unusable as a capability token."""
    if not raw or isinstance(raw, str):
        return []
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        cap = entry.strip().lower()
        if not cap or len(cap) > 32 or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", cap):
            continue
        if cap not in out:
            out.append(cap)
        if len(out) >= 12:
            break
    return out


def capability_ratio(offered: Iterable[str] | None, required: Iterable[str] | None) -> float:
    """Fraction of `required` that `offered` covers. No requirement ⇒ open call."""
    have = set(normalize_capabilities(offered))
    need = normalize_capabilities(required)
    if not need:
        return 1.0 if have else 0.5
    return sum(1 for cap in need if cap in have) / len(need)


def bid_confidence(
    offered: Iterable[str] | None,
    required: Iterable[str] | None,
    load: int = 0,
    healthy: bool = True,
) -> float:
    """How strongly this peer should claim a task.

    Honest self-assessment is the peer's job in a contract net; the gateway
    independently re-weights by capability coverage and pheromone trail, so
    inflating this only wastes an award the peer then fails.
    """
    ratio = capability_ratio(offered, required)
    if ratio == 0.0:
        return 0.0
    confidence = 0.35 + 0.6 * ratio
    if not healthy:
        # A peer whose backend is down can still serve degraded answers, but
        # should not outbid a healthy peer for the same work.
        confidence *= 0.4
    if isinstance(load, int) and load > 0:
        confidence /= 1.0 + 0.5 * min(load, 8)
    return max(0.0, min(1.0, round(confidence, 4)))


def should_bid(
    offered: Iterable[str] | None,
    required: Iterable[str] | None,
    load: int = 0,
    max_load: int = 4,
) -> bool:
    """Decline work this peer cannot cover or does not have capacity for."""
    if isinstance(load, int) and isinstance(max_load, int) and load >= max_load:
        return False
    return capability_ratio(offered, required) > 0.0


def required_capabilities(task: str) -> list[str]:
    """Infer which capability a task needs from its wording."""
    if not isinstance(task, str) or not task.strip():
        return [CAP_REASONING]
    lowered = task.lower()
    for capability, cues in _ROUTING:
        if any(cue in lowered for cue in cues):
            return [capability]
    return [CAP_REASONING]


def decompose(task: str, max_subtasks: int = MAX_SUBTASKS) -> list[dict[str, Any]]:
    """Split a task into capability-tagged subtasks for the mesh to allocate.

    Deliberately lexical rather than LLM-driven: decomposition runs before any
    peer is awarded, so it must be instant, deterministic, and available when
    Ollama is not.
    """
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task must be a non-empty string")
    if not isinstance(max_subtasks, int) or max_subtasks < 1:
        raise ValueError("max_subtasks must be a positive int")

    body = task.strip()
    # Explicit conjunctions are the only split signal we trust.
    parts = [p.strip() for p in re.split(r"(?:\s+and\s+then\s+|\s*;\s*|\s+then\s+)", body) if p.strip()]
    if len(parts) <= 1:
        parts = [body]

    subtasks: list[dict[str, Any]] = []
    for index, part in enumerate(parts[:max_subtasks]):
        subtasks.append({
            "index": index,
            "subtask": part[:4000],
            "capabilities": required_capabilities(part),
        })

    # Anything worth splitting is worth grounding: retrieval runs alongside.
    if len(subtasks) > 1 and not any(CAP_RETRIEVAL in s["capabilities"] for s in subtasks):
        if len(subtasks) < max_subtasks:
            subtasks.append({
                "index": len(subtasks),
                "subtask": f"recall prior swarm context for: {body[:200]}",
                "capabilities": [CAP_RETRIEVAL],
            })
    return subtasks


def consensus(
    results: Iterable[dict[str, Any]],
    threshold: float = CONSENSUS_THRESHOLD,
    min_votes: int = MIN_VOTES,
) -> dict[str, Any]:
    """Cluster peer answers by token overlap; the largest cluster wins.

    Mirror of `quorum` in `gateway/mesh.js`. `confident` stays False for a lone
    answer — one peer is a data point, not agreement.
    """
    if not isinstance(threshold, (int, float)) or not 0.0 < threshold <= 1.0:
        raise ValueError("threshold must be in (0, 1]")

    usable = [
        r for r in (results or [])
        if isinstance(r, dict) and isinstance(r.get("result"), str) and r["result"].strip()
    ]
    if not usable:
        return {"answer": None, "agreement": 0.0, "votes": 0, "total": 0, "peers": [], "confident": False}

    sets = [tokenize(r["result"]) for r in usable]
    clusters: list[dict[str, Any]] = []
    for i in range(len(usable)):
        for cluster in clusters:
            if jaccard(sets[i], sets[cluster["seed"]]) >= threshold:
                cluster["members"].append(i)
                break
        else:
            clusters.append({"seed": i, "members": [i]})

    def confidence_of(index: int) -> float:
        raw = usable[index].get("confidence", 0.5)
        return float(raw) if isinstance(raw, (int, float)) else 0.5

    clusters.sort(
        key=lambda c: (len(c["members"]), max(confidence_of(i) for i in c["members"])),
        reverse=True,
    )
    winner = clusters[0]
    spokesman = max(winner["members"], key=confidence_of)
    votes = len(winner["members"])
    return {
        "answer": usable[spokesman]["result"],
        "agreement": votes / len(usable),
        "votes": votes,
        "total": len(usable),
        "peers": [usable[i].get("peer") for i in winner["members"] if usable[i].get("peer")],
        "confident": votes >= min_votes and votes / len(usable) > 0.5,
    }


def merge_results(results: Iterable[dict[str, Any]]) -> str:
    """Attributed transcript of what every peer contributed."""
    lines: list[str] = []
    for entry in results or []:
        if not isinstance(entry, dict):
            continue
        text = entry.get("result")
        if not isinstance(text, str) or not text.strip():
            continue
        peer = entry.get("peer", "peer")
        confidence = entry.get("confidence", 0.5)
        confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.5
        lines.append(f"[{peer} · {confidence:.2f}] {text.strip()}")
    return "\n\n".join(lines)
