"""RAG helpers — retrieve shared context from Shell Cracked before generation."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

VAULT_URL = os.environ.get("VAULT_URL", "http://localhost:7070").rstrip("/")
VAULT_TOKEN = os.environ.get("VAULT_TOKEN") or os.environ.get("GATEWAY_TOKEN")
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "5"))
RAG_MESH_MIN_SCORE = float(os.environ.get("RAG_MESH_MIN_SCORE", "0.45"))
RAG_SESSION_MIN_SCORE = float(os.environ.get("RAG_SESSION_MIN_SCORE", "0.82"))
# Back-compat: RAG_MIN_SCORE overrides mesh gate when set explicitly
RAG_MIN_SCORE = float(
    os.environ.get("RAG_MIN_SCORE", str(RAG_MESH_MIN_SCORE))
)


def _vault_headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if VAULT_TOKEN:
        headers["X-Vault-Token"] = VAULT_TOKEN
    return headers


def threshold_for_profile(profile: str = "mesh") -> float:
    """mesh gate (0.45 default) vs session recall (0.82 default)."""
    if profile == "session":
        return RAG_SESSION_MIN_SCORE
    return RAG_MIN_SCORE


def query_memory(
    query: str,
    n: int = RAG_TOP_K,
    *,
    min_score: float | None = None,
    profile: str = "mesh",
) -> list[dict[str, Any]]:
    """Query Shell Cracked vector memory. Returns [] on failure (fail-open)."""
    if not isinstance(query, str) or not query.strip():
        return []
    if not isinstance(n, int) or n < 1:
        n = RAG_TOP_K
    params = urllib.parse.urlencode({"q": query.strip(), "n": min(n, 20)})
    req = urllib.request.Request(
        f"{VAULT_URL}/v1/memory/query?{params}",
        method="GET",
        headers=_vault_headers(),
    )
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return []
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        return []
    gate = min_score if min_score is not None else threshold_for_profile(profile)
    filtered: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        score = hit.get("score", 0.0)
        if isinstance(score, (int, float)) and score >= gate:
            filtered.append(hit)
    return filtered


def format_context(hits: list[dict[str, Any]], max_chars: int = 4000) -> str:
    """Turn memory hits into a prompt prefix."""
    if not hits:
        return ""
    lines: list[str] = ["Relevant prior swarm context:"]
    used = 0
    for i, hit in enumerate(hits, start=1):
        meta = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
        agent = meta.get("agent", "unknown")
        kind = meta.get("kind", "memory")
        text = str(hit.get("text", "")).strip()
        if not text:
            continue
        chunk = f"[{i}] ({agent}/{kind}) {text[:600]}"
        if used + len(chunk) > max_chars:
            break
        lines.append(chunk)
        used += len(chunk)
    return "\n".join(lines) if len(lines) > 1 else ""


def retrieve_context(
    query: str,
    n: int = RAG_TOP_K,
    *,
    profile: str = "mesh",
) -> tuple[str, list[dict[str, Any]]]:
    """Retrieve and format RAG context for injection into prompts."""
    hits = query_memory(query, n=n, profile=profile)
    return format_context(hits), hits


def inject_rag(prompt: str, context: str) -> str:
    """Prepend RAG context to a user prompt when available."""
    if not isinstance(prompt, str):
        prompt = str(prompt)
    ctx = context.strip() if isinstance(context, str) else ""
    if not ctx:
        return prompt
    return f"{ctx}\n\n---\n\nTask: {prompt}"


def store_session_context(session_id: str, context: dict[str, Any]) -> bool:
    """Persist swarm session state to vault (fail-open)."""
    if not isinstance(session_id, str) or not session_id.strip():
        return False
    if not isinstance(context, dict):
        return False
    body = json.dumps({"session_id": session_id.strip(), "context": context}).encode("utf-8")
    req = urllib.request.Request(
        f"{VAULT_URL}/v1/session",
        data=body,
        method="POST",
        headers={**_vault_headers(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
