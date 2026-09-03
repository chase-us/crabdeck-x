"""RAG client for CrabDeck agents.

Retrieval lives in the vault (`/v1/rag/query`) so every peer grounds on the
same context and citation numbering. This module is the thin, fail-open client
plus a local fallback for when :7070 is down.

All calls here are synchronous `urllib`. Per `.cursor/rules/crabdeck-event-loop.mdc`
callers must wrap them in `run_blocking`.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

VAULT_URL = os.environ.get("VAULT_URL", "http://localhost:7070").rstrip("/")
VAULT_TOKEN = os.environ.get("VAULT_TOKEN") or os.environ.get("GATEWAY_TOKEN")
RAG_TIMEOUT = float(os.environ.get("RAG_TIMEOUT", "3.0"))
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "4"))
RAG_CANDIDATES = int(os.environ.get("RAG_CANDIDATES", "12"))

# Longer than the 1.5s heartbeat ingest budget: retrieval is on the request
# path and worth a short wait, but never long enough to risk the 20s watchdog.
MAX_TIMEOUT = 10.0


def _get(path: str, params: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("path must be an absolute URL path")
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(
        f"{VAULT_URL}{path}?{query}",
        method="GET",
        headers={"Accept": "application/json"},
    )
    if VAULT_TOKEN:
        req.add_header("X-Vault-Token", VAULT_TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=min(timeout, MAX_TIMEOUT)) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body if isinstance(body, dict) else None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return None


def _post(path: str, body: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        raise TypeError("body must be a dict")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("path must be an absolute URL path")
    req = urllib.request.Request(
        f"{VAULT_URL}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    if VAULT_TOKEN:
        req.add_header("X-Vault-Token", VAULT_TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=min(timeout, MAX_TIMEOUT)) as resp:
            raw = resp.read().decode("utf-8")
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return None


def local_prompt(question: str) -> str:
    """Ungrounded prompt used when the vault is unreachable.

    It states the absence of swarm memory instead of inviting the model to
    fabricate citations for context it never saw.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    return (
        "SWARM MEMORY: (unavailable — the Shell Cracked vault did not answer)\n\n"
        "Answer from your own knowledge and say that swarm memory was unavailable. "
        "Do not invent citations.\n\n"
        f"QUESTION: {question.strip()}"
    )


def retrieve(
    question: str,
    k: int = RAG_TOP_K,
    candidates: int = RAG_CANDIDATES,
    timeout: float = RAG_TIMEOUT,
) -> dict[str, Any]:
    """Fetch grounded context for `question`. Never raises on a vault outage.

    Returns `{prompt, context, citations, grounded, hits, degraded}`.
    `degraded=True` means the vault did not answer and `prompt` is ungrounded.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    if not isinstance(k, int) or k < 1 or k > 20:
        raise ValueError("k must be an int in 1..20")
    if not isinstance(candidates, int) or candidates < 1 or candidates > 50:
        raise ValueError("candidates must be an int in 1..50")

    query = question.strip()
    body = _get("/v1/rag/query", {"q": query, "k": k, "n": candidates}, timeout)
    if not isinstance(body, dict) or not isinstance(body.get("prompt"), str):
        return {
            "query": query,
            "prompt": local_prompt(query),
            "context": "",
            "citations": [],
            "hits": [],
            "grounded": False,
            "degraded": True,
        }
    return {
        "query": query,
        "prompt": body["prompt"],
        "context": body.get("context", "") if isinstance(body.get("context"), str) else "",
        "citations": body["citations"] if isinstance(body.get("citations"), list) else [],
        "hits": body["hits"] if isinstance(body.get("hits"), list) else [],
        "grounded": bool(body.get("grounded")),
        "degraded": False,
        "space": body.get("space"),
    }


def ingest(
    agent: str,
    kind: str,
    text: str,
    metadata: dict[str, Any] | None = None,
    source: str = "",
    timeout: float = RAG_TIMEOUT,
) -> dict[str, Any] | None:
    """Chunk-ingest a document so future retrieval hits passages, not blobs."""
    if not isinstance(agent, str) or not agent.strip():
        return None
    if not isinstance(kind, str) or not kind.strip() or len(kind) > 64:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    payload: dict[str, Any] = {
        "agent": agent.strip().lower(),
        "kind": kind.strip(),
        "text": text[:40000],
        "metadata": metadata if isinstance(metadata, dict) else {},
    }
    if isinstance(source, str) and source.strip():
        payload["source"] = source.strip()[:200]
    return _post("/v1/rag/ingest", payload, timeout)


def citation_line(citations: list[dict[str, Any]]) -> str:
    """One-line provenance summary, e.g. `[1] hermes/prompt_result 0.83`."""
    if not isinstance(citations, list) or not citations:
        return ""
    parts = []
    for cite in citations[:8]:
        if not isinstance(cite, dict):
            continue
        parts.append(
            f"[{cite.get('n', '?')}] {cite.get('agent', 'unknown')}/{cite.get('kind', 'memory')}"
            f" {float(cite.get('score', 0.0)):.2f}"
        )
    return " · ".join(parts)
