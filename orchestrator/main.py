"""
CrabDeck Orchestrator Core v2.3
FastAPI agent health tracker + CrabDeck Gateway bridge + swarm mesh peer

As a swarm peer the orchestrator needs no LLM: it answers every SWARM_ROUND with a
deterministic health digest (agent status, watchdog misses, recent events) so the
LLM peers reason against live operational facts, and it answers direct MESH asks
with the same digest.

Run: uvicorn main:app --reload --port 8000

Env vars:
    GATEWAY_URL      default ws://localhost:8765
    GATEWAY_TOKEN    shared secret, must match the gateway's GATEWAY_TOKEN
    ALLOWED_ORIGINS  comma-separated list, default http://localhost:5173
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import time, threading, asyncio, os, psutil, uuid, json, urllib.error, urllib.request
from typing import Dict, List, Optional
import websockets

# ── Config ────────────────────────────────────────────────────────────────────
GATEWAY_URL        = os.environ.get("GATEWAY_URL", "ws://localhost:8765")
GATEWAY_TOKEN      = os.environ.get("GATEWAY_TOKEN")
VAULT_URL          = os.environ.get("VAULT_URL", "http://localhost:7070").rstrip("/")
VAULT_TOKEN        = os.environ.get("VAULT_TOKEN") or GATEWAY_TOKEN
ALLOWED_ORIGINS    = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]
HEARTBEAT_INTERVAL = 3.0
HEARTBEAT_TIMEOUT  = 15.0
MAX_EVENTS         = 300
BHIVE_EVERY        = 10.0

# ── Models ────────────────────────────────────────────────────────────────────
class Agent(BaseModel):
    id:              str
    name:            str
    status:          str          # running | stopped | error | offline
    last_heartbeat:  float
    cpu_percent:     float = 0.0
    memory_mb:       float = 0.0
    error_message:   Optional[str] = None
    auto_restart:    bool = True
    team:            str = "crabdeck"

class Event(BaseModel):
    id:        str
    timestamp: float
    type:      str
    agent_id:  Optional[str] = None
    message:   str

# ── State ─────────────────────────────────────────────────────────────────────
agents: Dict[str, Agent] = {}
events: List[Event]      = []
_heartbeat_thread_started = False

def add_event(event_type: str, message: str, agent_id: Optional[str] = None):
    evt = Event(id=str(uuid.uuid4()), timestamp=time.time(),
                type=event_type, agent_id=agent_id, message=message)
    events.append(evt)
    if len(events) > MAX_EVENTS:
        del events[:len(events) - MAX_EVENTS]
    print(f"[{event_type}] {message}")

def seed_agents():
    now = time.time()
    for agent_id, name in [("crabdeck", "CrabDeck Gateway"),
                            ("openclaw", "OpenClaw Sovereign"),
                            ("hermes",   "Hermes Messenger")]:
        agents[agent_id] = Agent(id=agent_id, name=name, status="offline", last_heartbeat=now)
    add_event("SYSTEM", "Team CrabDeck agents seeded", None)

def heartbeat_loop():
    while True:
        now = time.time()
        for agent_id, agent in list(agents.items()):
            agent.cpu_percent = psutil.cpu_percent(interval=None)
            agent.memory_mb   = psutil.virtual_memory().used / (1024 * 1024)
            if agent.status == "running" and (now - agent.last_heartbeat) > HEARTBEAT_TIMEOUT:
                agent.status        = "error"
                agent.error_message = "Heartbeat missed"
                add_event("HEARTBEAT_MISSED", f"{agent.name} missed heartbeat", agent_id)
        time.sleep(HEARTBEAT_INTERVAL)

# ── Swarm mesh peer ──────────────────────────────────────────────────────────
SWARM_DIGEST_EVENTS = 5
SWARM_MAX_TEXT = 6000

def swarm_digest(agent_map: Dict[str, Agent], event_log: List[Event], round_no: int = 1,
                 peers: Optional[List[str]] = None, contributions: Optional[Dict[str, str]] = None,
                 now: Optional[float] = None) -> str:
    """Deterministic operational digest the orchestrator contributes to a swarm round."""
    if not isinstance(agent_map, dict):
        raise TypeError("agent_map must be a dict")
    if not isinstance(event_log, list):
        raise TypeError("event_log must be a list")
    ts = time.time() if now is None else float(now)
    lines: List[str] = [f"Orchestrator health digest (round {round_no}, bhive slot {int(ts // 60)}):"]
    running = 0
    for agent_id in sorted(agent_map):
        a = agent_map[agent_id]
        age = max(0.0, ts - a.last_heartbeat)
        flag = "" if a.status == "running" else f" ({a.error_message})" if a.error_message else ""
        lines.append(f"- {agent_id}: {a.status}{flag}, last heartbeat {age:.0f}s ago")
        if a.status == "running":
            running += 1
    lines.append(f"{running}/{len(agent_map)} agents running.")
    if peers:
        missing = [p for p in ("hermes", "openclaw") if p in peers and agent_map.get(p) is not None
                   and agent_map[p].status != "running"]
        if missing:
            lines.append(f"Constraint: swarm peer(s) {', '.join(missing)} are not healthy — treat their output as stale.")
    recent = [e for e in event_log if e.type != "SYSTEM"][-SWARM_DIGEST_EVENTS:]
    if recent:
        lines.append("Recent events:")
        for e in reversed(recent):
            lines.append(f"  · [{e.type}] {e.message}")
    if contributions:
        lines.append(f"Peers contributed last round: {', '.join(sorted(contributions))}. "
                     "Any action they propose should wait until every required agent is running.")
    text = "\n".join(lines)
    return text if len(text) <= SWARM_MAX_TEXT else text[:SWARM_MAX_TEXT]


def _swarm_round_payload(msg: dict) -> Optional[dict]:
    raw = msg.get("payload") if isinstance(msg, dict) else None
    if not isinstance(raw, dict):
        return None
    session_id = raw.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    rnd = raw.get("round")
    peers = raw.get("peers")
    contributions = raw.get("contributions")
    return {
        "session_id": session_id.strip(),
        "round": rnd if isinstance(rnd, int) and rnd >= 1 else 1,
        "peers": [p for p in peers if isinstance(p, str)] if isinstance(peers, list) else [],
        "contributions": {k: v for k, v in contributions.items() if isinstance(k, str) and isinstance(v, str)}
                         if isinstance(contributions, dict) else {},
    }


async def handle_swarm_round(ws, msg: dict) -> Optional[str]:
    rnd = _swarm_round_payload(msg)
    if rnd is None:
        return None
    text = swarm_digest(agents, events, rnd["round"], rnd["peers"], rnd["contributions"])
    await ws.send(json.dumps({
        "type": "SWARM_CONTRIBUTION",
        "agent": "orchestrator",
        "payload": {"session_id": rnd["session_id"], "round": rnd["round"], "text": text},
    }))
    add_event("SWARM", f"contributed health digest to swarm {rnd['session_id'][:8]} round {rnd['round']}", "orchestrator")
    return text


async def handle_mesh(ws, msg: dict) -> Optional[str]:
    sender = msg.get("from") if isinstance(msg, dict) else None
    raw = msg.get("payload") if isinstance(msg, dict) else None
    payload = raw if isinstance(raw, dict) else {"text": raw}
    if not isinstance(sender, str) or not sender.strip():
        return None
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    if payload.get("intent") != "ask":
        add_event("MESH", f"{sender} → orchestrator: {text.strip()[:80]}", "orchestrator")
        return None
    reply = swarm_digest(agents, events)
    out = {"intent": "tell", "text": reply}
    if isinstance(payload.get("session_id"), str) and payload["session_id"].strip():
        out["session_id"] = payload["session_id"].strip()
    await ws.send(json.dumps({"type": "MESH", "agent": "orchestrator", "to": sender.strip(), "payload": out}))
    return reply


def emit_vault_heartbeat(agent: str = "orchestrator") -> None:
    ts = time.time()
    body = json.dumps({
        "agent": agent,
        "ts": ts,
        "slot": int(ts // 60),
        "source": "orchestrator",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{VAULT_URL}/v1/heartbeat",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    if VAULT_TOKEN:
        req.add_header("X-Vault-Token", VAULT_TOKEN)
    try:
        urllib.request.urlopen(req, timeout=1.5).read()
    except (urllib.error.URLError, TimeoutError, OSError):
        pass


async def orchestrator_heartbeat(ws):
    while True:
        await asyncio.sleep(BHIVE_EVERY)
        ts = time.time()
        try:
            await ws.send(json.dumps({
                "type": "HEARTBEAT",
                "agent": "orchestrator",
                "ts": ts,
                "bhive_slot": int(ts // 60),
            }))
        except Exception:
            break
        await asyncio.to_thread(emit_vault_heartbeat, "orchestrator")


async def listen_gateway():
    while True:
        try:
            async with websockets.connect(GATEWAY_URL) as ws:
                hello = {"type": "HELLO", "client": "orchestrator"}
                if GATEWAY_TOKEN:
                    hello["token"] = GATEWAY_TOKEN
                await ws.send(json.dumps(hello))
                add_event("SYSTEM", "Connected to CrabDeck Gateway", None)
                hb_task = asyncio.create_task(orchestrator_heartbeat(ws))
                try:
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            if msg.get("type") == "ERROR":
                                add_event("SYSTEM", f"Gateway auth error: {msg.get('message')}", None)
                            if msg.get("type") == "AGENT_STATUS":
                                agent_id = msg.get("agent")
                                status   = msg.get("status", "offline")
                                if agent_id in agents:
                                    agents[agent_id].status         = status
                                    agents[agent_id].last_heartbeat = time.time()
                                    add_event("AGENT_STATUS", f"{agent_id} → {status}", agent_id)
                            elif msg.get("type") == "SWARM_ROUND":
                                await handle_swarm_round(ws, msg)
                            elif msg.get("type") == "MESH":
                                await handle_mesh(ws, msg)
                            elif msg.get("type") == "MESH_PEERS":
                                peers = msg.get("payload", {}).get("peers", []) if isinstance(msg.get("payload"), dict) else []
                                add_event("MESH", f"mesh peers: {', '.join(peers) or 'none'}", None)
                        except Exception:
                            pass
                finally:
                    hb_task.cancel()
        except Exception as e:
            add_event("SYSTEM", f"Gateway disconnected: {e} — retrying in 5 s", None)
            await asyncio.sleep(5)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _heartbeat_thread_started
    seed_agents()
    if not _heartbeat_thread_started:
        threading.Thread(target=heartbeat_loop, daemon=True).start()
        _heartbeat_thread_started = True
    gateway_task = asyncio.create_task(listen_gateway())
    add_event("SYSTEM", "CrabDeck Orchestrator started", None)
    yield
    gateway_task.cancel()

app = FastAPI(title="CrabDeck Orchestrator", version="2.3.0", lifespan=lifespan)

# CORS: locked to ALLOWED_ORIGINS instead of "*". Set ALLOWED_ORIGINS in the
# environment to your real frontend origin(s) before deploying publicly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── REST API ──────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    now = time.time()
    return {
        "status": "ok",
        "service": "orchestrator",
        "agent_count": len(agents),
        "uptime": now,
        "vault": VAULT_URL,
        "bhive_slot": int(now // 60),
    }

@app.get("/agents", response_model=List[Agent])
def list_agents():
    return list(agents.values())

@app.get("/agents/{agent_id}", response_model=Agent)
def get_agent(agent_id: str):
    a = agents.get(agent_id)
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")
    return a

@app.post("/agents/{agent_id}/restart", response_model=Agent)
def restart_agent(agent_id: str):
    a = agents.get(agent_id)
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")
    a.status, a.error_message, a.last_heartbeat = "running", None, time.time()
    add_event("AGENT_RESTARTED", f"Restarted {a.name}", agent_id)
    return a

@app.get("/events", response_model=List[Event])
def get_events(limit: int = 100):
    return events[-limit:]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
