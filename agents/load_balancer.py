"""Weighted round-robin load balancer for swarm agent dispatch.

Tracks per-agent token slots and host memory pressure before delegating work.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore

DEFAULT_WEIGHTS: dict[str, int] = {
    "hermes": int(os.environ.get("SWARM_WEIGHT_HERMES", "2")),
    "openclaw": int(os.environ.get("SWARM_WEIGHT_OPENCLAW", "3")),
}
MAX_RAM_PERCENT = float(os.environ.get("SWARM_MAX_RAM_PERCENT", "85"))
MAX_TOKENS_PER_AGENT = int(os.environ.get("SWARM_MAX_CONCURRENT_TOKENS", "2"))
DISPATCH_TIMEOUT_SEC = float(os.environ.get("SWARM_DISPATCH_TIMEOUT", "90"))


@dataclass
class AgentLoad:
    agent: str
    weight: int
    active_tokens: int = 0
    total_dispatched: int = 0
    last_dispatch: float = 0.0
    online: bool = True


@dataclass
class LoadBalancerState:
    agents: dict[str, AgentLoad] = field(default_factory=dict)
    round_robin_cursor: int = 0
    memory_percent: float = 0.0
    memory_pressure: bool = False
    updated_at: float = field(default_factory=time.time)


_lock = threading.RLock()
_state = LoadBalancerState()


def _ensure_agents(weights: dict[str, int] | None = None) -> None:
    w = weights or DEFAULT_WEIGHTS
    for agent, weight in w.items():
        if agent not in _state.agents:
            _state.agents[agent] = AgentLoad(agent=agent, weight=max(1, weight))


def refresh_memory() -> bool:
    """Update memory pressure flag. Returns True when over threshold."""
    if psutil is None:
        _state.memory_percent = 0.0
        _state.memory_pressure = False
        return False
    pct = float(psutil.virtual_memory().percent)
    _state.memory_percent = pct
    _state.memory_pressure = pct >= MAX_RAM_PERCENT
    _state.updated_at = time.time()
    return _state.memory_pressure


def set_agent_online(agent: str, online: bool) -> None:
    with _lock:
        _ensure_agents()
        if agent in _state.agents:
            _state.agents[agent].online = online


def can_dispatch(agent: str) -> tuple[bool, str | None]:
    """Return (allowed, reason) for dispatching to an agent."""
    with _lock:
        _ensure_agents()
        refresh_memory()
        if _state.memory_pressure:
            return False, f"host RAM { _state.memory_percent:.0f}% >= {MAX_RAM_PERCENT:.0f}%"
        load = _state.agents.get(agent)
        if load is None:
            return False, f"unknown agent {agent!r}"
        if not load.online:
            return False, f"{agent} offline"
        if load.active_tokens >= MAX_TOKENS_PER_AGENT:
            return False, f"{agent} token cap ({MAX_TOKENS_PER_AGENT})"
        return True, None


def acquire_token(agent: str) -> bool:
    ok, _ = can_dispatch(agent)
    if not ok:
        return False
    with _lock:
        load = _state.agents[agent]
        load.active_tokens += 1
        load.total_dispatched += 1
        load.last_dispatch = time.time()
        return True


def release_token(agent: str) -> None:
    with _lock:
        load = _state.agents.get(agent)
        if load and load.active_tokens > 0:
            load.active_tokens -= 1


def order_subtasks(subtasks: list[dict[str, str]], online: dict[str, bool] | None = None) -> list[dict[str, str]]:
    """Weighted round-robin ordering — higher weight agents appear earlier in the cycle."""
    if not subtasks:
        return []
    with _lock:
        _ensure_agents()
        if online:
            for agent, is_on in online.items():
                if agent in _state.agents:
                    _state.agents[agent].online = bool(is_on)
        refresh_memory()

        by_agent: dict[str, list[dict[str, str]]] = {}
        for sub in subtasks:
            by_agent.setdefault(sub["agent"], []).append(sub)

        agents = [a for a in _state.agents if a in by_agent]
        if not agents:
            return list(subtasks)

        max_weight = max(_state.agents[a].weight for a in agents)
        ordered: list[dict[str, str]] = []
        for tick in range(max_weight):
            for agent in agents:
                load = _state.agents[agent]
                if tick >= load.weight:
                    continue
                ok, _ = can_dispatch(agent)
                if not ok:
                    continue
                if by_agent[agent]:
                    ordered.append(by_agent[agent].pop(0))

        for agent in agents:
            ordered.extend(by_agent.get(agent, []))
        return ordered if ordered else list(subtasks)


def snapshot() -> dict[str, Any]:
    with _lock:
        _ensure_agents()
        refresh_memory()
        return {
            "memory_percent": round(_state.memory_percent, 1),
            "memory_pressure": _state.memory_pressure,
            "max_ram_percent": MAX_RAM_PERCENT,
            "max_tokens_per_agent": MAX_TOKENS_PER_AGENT,
            "dispatch_timeout_sec": DISPATCH_TIMEOUT_SEC,
            "agents": {
                name: {
                    "weight": load.weight,
                    "active_tokens": load.active_tokens,
                    "total_dispatched": load.total_dispatched,
                    "online": load.online,
                }
                for name, load in _state.agents.items()
            },
            "updated_at": _state.updated_at,
        }
