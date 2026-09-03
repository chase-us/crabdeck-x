"""
CrabDeck Orchestrator Core v2.2
FastAPI agent health tracker + CrabDeck Gateway bridge

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
                            ("hermes",   "Hermes Messenger"),
                            ("swarm",    "Swarm Coordinator")]:
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

app = FastAPI(title="CrabDeck Orchestrator", version="2.2.0", lifespan=lifespan)

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


class SwarmGoal(BaseModel):
    goal: str
    model: str = "llama3"
    session_id: Optional[str] = None


@app.post("/swarm/goal")
def post_swarm_goal(body: SwarmGoal):
    """Queue a collaborative swarm goal (proxied to gateway by UI in practice)."""
    if not body.goal.strip():
        raise HTTPException(status_code=400, detail="goal must be non-empty")
    add_event("SWARM_GOAL", body.goal[:200], "swarm")
    return {
        "status": "accepted",
        "goal": body.goal,
        "model": body.model,
        "session_id": body.session_id,
        "hint": "Send SWARM_GOAL via gateway WebSocket for live mesh execution",
    }


@app.get("/swarm/status")
def swarm_status():
    online = {aid: a.status for aid, a in agents.items() if aid in {"hermes", "openclaw", "swarm", "orchestrator"}}
    return {"mesh_agents": list(online.keys()), "online": online, "vault": VAULT_URL}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
