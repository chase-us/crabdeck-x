"""POST heartbeats/memory to Shell Cracked without blocking the agent loop."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

VAULT_URL = os.environ.get("VAULT_URL", "http://localhost:7070").rstrip("/")
VAULT_TOKEN = os.environ.get("VAULT_TOKEN") or os.environ.get("GATEWAY_TOKEN")
SLOT_SECONDS = 60
ALLOWED_AGENTS = frozenset({"hermes", "openclaw", "orchestrator", "crabdeck", "vault"})


def minute_slot(ts_seconds: float) -> int:
    if not isinstance(ts_seconds, (int, float)):
        raise TypeError("ts_seconds must be a number")
    if ts_seconds < 0:
        raise ValueError("ts_seconds must be >= 0")
    return int(ts_seconds // SLOT_SECONDS)


def heartbeat_payload(agent: str, ts: float) -> dict[str, Any]:
    if not isinstance(agent, str) or not agent.strip():
        raise ValueError("agent must be a non-empty string")
    if not isinstance(ts, (int, float)):
        raise TypeError("ts must be a number")
    name = agent.strip().lower()
    if name not in ALLOWED_AGENTS:
        raise ValueError(f"unknown agent: {agent!r}")
    return {
        "type": "HEARTBEAT",
        "agent": name,
        "ts": float(ts),
        "bhive_slot": minute_slot(float(ts)),
    }


def _post(path: str, body: dict[str, Any], timeout: float = 1.5) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        raise TypeError("vault payload must be a dict")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("path must be an absolute URL path")
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{VAULT_URL}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    if VAULT_TOKEN:
        req.add_header("X-Vault-Token", VAULT_TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            return parsed if isinstance(parsed, dict) else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None


def emit_heartbeat(agent: str, ts: float, slot: int, source: str = "agent") -> bool:
    if not isinstance(agent, str) or not agent.strip():
        return False
    if not isinstance(ts, (int, float)) or not isinstance(slot, int):
        return False
    if not isinstance(source, str) or not source.strip():
        return False
    name = agent.strip().lower()
    if name not in ALLOWED_AGENTS:
        return False
    result = _post(
        "/v1/heartbeat",
        {"agent": name, "ts": float(ts), "slot": slot, "source": source.strip()},
    )
    return isinstance(result, dict)


def emit_memory(agent: str, kind: str, text: str, metadata: dict[str, Any] | None = None) -> bool:
    if not isinstance(agent, str) or not agent.strip():
        return False
    if not isinstance(kind, str) or not kind.strip() or len(kind) > 64:
        return False
    if not isinstance(text, str) or not text.strip():
        return False
    name = agent.strip().lower()
    if name not in ALLOWED_AGENTS:
        return False
    payload = {
        "agent": name,
        "kind": kind.strip(),
        "text": text[:8000],
        "metadata": metadata if isinstance(metadata, dict) else {},
    }
    return _post("/v1/memory", payload) is not None


def retrieve_memory(query: str, limit: int = 5, timeout: float = 1.5) -> list[dict[str, Any]]:
    """Fetch bounded RAG context. Retrieval is fail-open for agent availability."""
    if not isinstance(query, str) or not query.strip():
        return []
    if not isinstance(limit, int) or not 1 <= limit <= 50:
        return []
    params = urllib.parse.urlencode({"q": query.strip(), "n": limit})
    req = urllib.request.Request(
        f"{VAULT_URL}/v1/memory/query?{params}",
        headers={"Accept": "application/json"},
    )
    if VAULT_TOKEN:
        req.add_header("X-Vault-Token", VAULT_TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        hits = body.get("hits", []) if isinstance(body, dict) else []
        return hits if isinstance(hits, list) else []
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return []
