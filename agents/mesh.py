"""Mesh peer client for CrabDeck agents.

Wraps the `MESH_*` protocol so each agent only supplies handlers:

    peer = MeshPeer("hermes", ["reasoning", "llm"])
    peer.on_award(run_the_work)          # awarded a contract
    peer.on_message(answer_a_peer)       # another peer asked directly
    await peer.handle(ws, msg)           # inside the agent's recv loop

Handlers may be sync or async. Sync handlers are offloaded with `run_blocking`
so a 120s Ollama call cannot freeze the heartbeat coroutine
(`.cursor/rules/crabdeck-event-loop.mdc`).
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import uuid
from collections.abc import Callable
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from offload import run_blocking
from swarm import bid_confidence, normalize_capabilities, should_bid

MESH_MESSAGE_TYPES = frozenset({
    "MESH_STATE", "MESH_CFP", "MESH_AWARD", "MESH_CFP_CLOSED", "MESH_AWARDED",
    "MESH_UNAWARDED", "MESH_RESULT", "MESH_CONSENSUS", "MESH_MESSAGE",
    "MESH_GOSSIP", "MESH_JOIN", "MESH_LEAVE", "MESH_ERROR", "MESH_BID_ACK",
    "MESH_ANNOUNCED", "MESH_DIRECT_ACK", "MESH_GOSSIP_ACK",
})

MAX_LOAD = int(os.environ.get("MESH_MAX_LOAD", "4"))
GOSSIP_TTL = 2


def _payload_of(msg: Any) -> dict[str, Any]:
    """Extract a dict payload from an untrusted frame."""
    if not isinstance(msg, dict):
        return {}
    payload = msg.get("payload")
    return payload if isinstance(payload, dict) else {}


class MeshPeer:
    """One agent's membership in the swarm mesh."""

    def __init__(
        self,
        name: str,
        capabilities: list[str] | None = None,
        max_load: int = MAX_LOAD,
        healthy: Callable[[], bool] | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a non-empty string")
        if healthy is not None and not callable(healthy):
            raise TypeError("healthy must be callable or None")
        if not isinstance(max_load, int) or max_load < 1:
            raise ValueError("max_load must be a positive int")

        self.name = name.strip().lower()
        self.capabilities = normalize_capabilities(capabilities or [])
        self.max_load = max_load
        self.load = 0
        self.peers: list[dict[str, Any]] = []
        self._healthy = healthy
        self._award: Callable[..., Any] | None = None
        self._message: Callable[..., Any] | None = None
        self._gossip: Callable[..., Any] | None = None
        self._consensus: Callable[..., Any] | None = None
        self._result: Callable[..., Any] | None = None

    # ── Registration ──
    def hello_payload(self, token: str | None = None, version: str = "2.3") -> dict[str, Any]:
        hello: dict[str, Any] = {
            "type": "HELLO",
            "client": self.name,
            "version": version,
            "capabilities": self.capabilities,
        }
        if token:
            hello["token"] = token
        return hello

    def on_award(self, fn: Callable[..., Any]) -> None:
        """Handler for winning a contract: `fn(task, capabilities, task_id) -> str | dict`."""
        if not callable(fn):
            raise TypeError("award handler must be callable")
        self._award = fn

    def on_message(self, fn: Callable[..., Any]) -> None:
        """Handler for a direct peer request: `fn(intent, body, sender) -> Any`."""
        if not callable(fn):
            raise TypeError("message handler must be callable")
        self._message = fn

    def on_gossip(self, fn: Callable[..., Any]) -> None:
        if not callable(fn):
            raise TypeError("gossip handler must be callable")
        self._gossip = fn

    def on_consensus(self, fn: Callable[..., Any]) -> None:
        if not callable(fn):
            raise TypeError("consensus handler must be callable")
        self._consensus = fn

    def on_result(self, fn: Callable[..., Any]) -> None:
        """Handler for another peer's result on a contract this peer announced."""
        if not callable(fn):
            raise TypeError("result handler must be callable")
        self._result = fn

    # ── Bidding ──
    def is_healthy(self) -> bool:
        if self._healthy is None:
            return True
        try:
            return bool(self._healthy())
        except Exception:
            # A probe that throws is itself evidence of an unhealthy backend.
            return False

    def bid_for(self, cfp: dict[str, Any]) -> dict[str, Any] | None:
        """Decide whether to bid, and how strongly. None declines."""
        payload = cfp if isinstance(cfp, dict) else {}
        task_id = payload.get("taskId")
        if not isinstance(task_id, str) or not task_id.strip():
            return None
        required = normalize_capabilities(payload.get("capabilities") or [])
        if not should_bid(self.capabilities, required, self.load, self.max_load):
            return None
        healthy = self.is_healthy()
        return {
            "taskId": task_id.strip(),
            "confidence": bid_confidence(self.capabilities, required, self.load, healthy),
            # Load is the peer's own queue depth; the gateway divides by it.
            "cost": float(self.load),
            "capabilities": self.capabilities,
            "note": "" if healthy else "degraded backend",
        }

    # ── Outbound ──
    async def announce(
        self,
        ws,
        task: str,
        capabilities: list[str] | None = None,
        task_id: str | None = None,
        quorum: int = 1,
    ) -> str:
        """Put a task out to the mesh. Returns the task id used."""
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be a non-empty string")
        tid = (task_id or f"{self.name}-{uuid.uuid4().hex[:8]}")[:64]
        await ws.send(json.dumps({
            "type": "MESH_ANNOUNCE",
            "payload": {
                "taskId": tid,
                "task": task.strip()[:4000],
                "capabilities": normalize_capabilities(capabilities or []),
                "quorum": max(1, int(quorum) if isinstance(quorum, int) else 1),
            },
        }))
        return tid

    async def ask(self, ws, peer: str, intent: str, body: Any, reply_to: str = "") -> None:
        """Send a direct request to one named peer."""
        if not isinstance(peer, str) or not peer.strip():
            raise ValueError("peer must be a non-empty string")
        await ws.send(json.dumps({
            "type": "MESH_DIRECT",
            "payload": {
                "to": peer.strip().lower(),
                "intent": str(intent)[:32] if intent else "message",
                "replyTo": str(reply_to)[:64],
                "body": body,
            },
        }))

    async def reply(self, ws, peer: str, body: Any, reply_to: str = "") -> None:
        await self.ask(ws, peer, "reply", body, reply_to)

    async def gossip(self, ws, topic: str, body: Any, ttl: int = GOSSIP_TTL) -> str:
        """Flood a notice across the mesh. Returns the gossip id."""
        gid = f"{self.name}-{uuid.uuid4().hex[:10]}"
        await ws.send(json.dumps({
            "type": "MESH_GOSSIP",
            "payload": {
                "id": gid,
                "ttl": max(0, min(4, int(ttl) if isinstance(ttl, int) else GOSSIP_TTL)),
                "topic": str(topic)[:32] if topic else "notice",
                "origin": self.name,
                "body": body,
            },
        }))
        return gid

    async def submit(
        self,
        ws,
        task_id: str,
        result: str,
        confidence: float = 0.6,
        ok: bool = True,
        citations: list[dict[str, Any]] | None = None,
    ) -> None:
        """Return an answer for a contract. Feeds consensus and pheromone."""
        await ws.send(json.dumps({
            "type": "MESH_RESULT",
            "payload": {
                "taskId": str(task_id)[:64],
                "result": str(result)[:8000],
                "confidence": float(confidence) if isinstance(confidence, (int, float)) else 0.6,
                "ok": bool(ok),
                "citations": citations[:12] if isinstance(citations, list) else [],
            },
        }))

    async def request_peers(self, ws) -> None:
        await ws.send(json.dumps({"type": "MESH_PEERS"}))

    # ── Inbound ──
    async def _invoke(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Await async handlers; offload sync ones so the loop keeps ticking."""
        if inspect.iscoroutinefunction(fn):
            return await fn(*args)
        return await run_blocking(fn, *args)

    async def handle(self, ws, msg: Any) -> bool:
        """Process one mesh frame. Returns True when it was a mesh frame."""
        if not isinstance(msg, dict):
            return False
        mtype = msg.get("type")
        if not isinstance(mtype, str) or mtype not in MESH_MESSAGE_TYPES:
            return False
        payload = _payload_of(msg)

        if mtype == "MESH_STATE":
            peers = payload.get("peers")
            self.peers = peers if isinstance(peers, list) else []
            return True

        if mtype == "MESH_CFP":
            bid = self.bid_for(payload)
            if bid is not None:
                await ws.send(json.dumps({"type": "MESH_BID", "payload": bid}))
                # Reserve capacity now so a second CFP sees the real depth.
                self.load += 1
            return True

        if mtype == "MESH_CFP_CLOSED":
            # Lost the award — release the capacity reserved at bid time.
            self.load = max(0, self.load - 1)
            return True

        if mtype == "MESH_AWARD":
            task_id = str(payload.get("taskId", ""))[:64]
            task = payload.get("task")
            task = task if isinstance(task, str) else str(task)
            capabilities = normalize_capabilities(payload.get("capabilities") or [])
            if self._award is None:
                await self.submit(ws, task_id, "[no award handler registered]", 0.0, ok=False)
                self.load = max(0, self.load - 1)
                return True
            try:
                outcome = await self._invoke(self._award, task, capabilities, task_id)
            except Exception as exc:  # a peer crash must not silence the contract
                await self.submit(ws, task_id, f"[{self.name} error] {exc}", 0.0, ok=False)
                self.load = max(0, self.load - 1)
                return True
            if isinstance(outcome, dict):
                await self.submit(
                    ws,
                    task_id,
                    str(outcome.get("result", "")),
                    outcome.get("confidence", 0.6),
                    bool(outcome.get("ok", True)),
                    outcome.get("citations"),
                )
            else:
                await self.submit(ws, task_id, str(outcome), 0.6)
            self.load = max(0, self.load - 1)
            return True

        if mtype == "MESH_MESSAGE":
            sender = str(payload.get("from", "unknown"))[:32]
            intent = str(payload.get("intent", "message"))[:32]
            if self._message is None:
                return True
            try:
                answer = await self._invoke(self._message, intent, payload.get("body"), sender)
            except Exception as exc:
                answer = f"[{self.name} error] {exc}"
            if answer is not None:
                await self.reply(ws, sender, answer, str(payload.get("replyTo", ""))[:64])
            return True

        if mtype == "MESH_GOSSIP" and self._gossip is not None:
            try:
                await self._invoke(self._gossip, str(payload.get("topic", "notice")), payload.get("body"))
            except Exception:
                pass  # gossip is advisory; a bad handler must not kill the loop
            return True

        if mtype == "MESH_CONSENSUS" and self._consensus is not None:
            try:
                await self._invoke(self._consensus, payload)
            except Exception:
                pass
            return True

        if mtype == "MESH_RESULT" and self._result is not None:
            try:
                await self._invoke(self._result, payload)
            except Exception:
                pass
            return True

        return True
