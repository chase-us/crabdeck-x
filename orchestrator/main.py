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
from typing import Dict, List, Optional, Any
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
    capabilities:    List[str] = []
    mesh_role:       str = "worker"


class SwarmTaskRequest(BaseModel):
    goal: str
    initiator: str = "orchestrator"
    parameters: Optional[Dict[str, Any]] = None


class SwarmTaskStatus(BaseModel):
    task_id: str
    goal: str
    status: str
    results: Dict[str, Any] = {}
    created_at: float


class SwarmMeshTopology(BaseModel):
    mesh_size: int
    active_nodes: int
    agents: List[Agent]
    rag_vault_status: str
    tasks: List[Dict[str, Any]] = []

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
    agents["crabdeck"] = Agent(
        id="crabdeck",
        name="CrabDeck Gateway",
        status="offline",
        last_heartbeat=now,
        capabilities=["websocket_mesh_bus", "p2p_routing", "watchdog_monitoring"],
        mesh_role="hub",
    )
    agents["openclaw"] = Agent(
        id="openclaw",
        name="OpenClaw Sovereign",
        status="offline",
        last_heartbeat=now,
        capabilities=["system_exec", "task_reasoning", "agent_collab"],
        mesh_role="node",
    )
    agents["hermes"] = Agent(
        id="hermes",
        name="Hermes Messenger",
        status="offline",
        last_heartbeat=now,
        capabilities=["llm_synthesis", "rag_retrieval", "tool_routing"],
        mesh_role="node",
    )
    add_event("SYSTEM", "Team CrabDeck swarm mesh agents seeded", None)

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


# ── Swarm Mesh State ────────────────────────────────────────────────────────
active_swarm_tasks: Dict[str, Dict[str, Any]] = {}
gateway_ws_conn = None

async def listen_gateway():
    global gateway_ws_conn
    while True:
        try:
            async with websockets.connect(GATEWAY_URL) as ws:
                gateway_ws_conn = ws
                hello = {"type": "HELLO", "client": "orchestrator"}
                if GATEWAY_TOKEN:
                    hello["token"] = GATEWAY_TOKEN
                await ws.send(json.dumps(hello))
                add_event("SYSTEM", "Connected to CrabDeck Gateway Swarm Mesh", None)
                hb_task = asyncio.create_task(orchestrator_heartbeat(ws))
                try:
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            mtype = msg.get("type")
                            if mtype == "ERROR":
                                add_event("SYSTEM", f"Gateway auth error: {msg.get('message')}", None)
                            elif mtype == "AGENT_STATUS":
                                agent_id = msg.get("agent")
                                status   = msg.get("status", "offline")
                                if agent_id in agents:
                                    agents[agent_id].status         = status
                                    agents[agent_id].last_heartbeat = time.time()
                                    add_event("AGENT_STATUS", f"{agent_id} → {status}", agent_id)
                            elif mtype == "SWARM_PEER_JOIN":
                                peer = msg.get("peer")
                                add_event("SWARM_PEER_JOIN", f"Peer {peer} joined swarm mesh", peer)
                            elif mtype == "SWARM_TASK_DISPATCH":
                                tid = msg.get("taskId")
                                if tid:
                                    active_swarm_tasks[tid] = {
                                        "taskId": tid,
                                        "goal": msg.get("goal"),
                                        "status": "in_progress",
                                        "results": {},
                                        "createdAt": msg.get("ts", time.time()),
                                    }
                                    add_event("SWARM_TASK", f"Swarm task dispatched: {msg.get('goal')}", None)
                            elif mtype == "SWARM_TASK_UPDATE":
                                tid = msg.get("taskId")
                                if tid and tid in active_swarm_tasks:
                                    active_swarm_tasks[tid]["results"][msg.get("agent")] = msg.get("contribution")
                                    if len(active_swarm_tasks[tid]["results"]) >= 2:
                                        active_swarm_tasks[tid]["status"] = "completed"
                                    add_event("SWARM_CONTRIBUTION", f"{msg.get('agent')} contributed to {tid}", msg.get("agent"))
                        except Exception:
                            pass
                finally:
                    hb_task.cancel()
                    gateway_ws_conn = None
        except Exception as e:
            gateway_ws_conn = None
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

# ── Swarm Mesh REST Endpoints ─────────────────────────────────────────────────
@app.get("/mesh", response_model=SwarmMeshTopology)
def get_mesh():
    active = [a for a in agents.values() if a.status == "running"]
    return SwarmMeshTopology(
        mesh_size=len(agents),
        active_nodes=len(active),
        agents=list(agents.values()),
        rag_vault_status="configured",
        tasks=list(active_swarm_tasks.values())[-10:],
    )

@app.post("/mesh/tasks", response_model=Dict[str, Any])
async def trigger_swarm_task(req: SwarmTaskRequest):
    if not req.goal or not req.goal.strip():
        raise HTTPException(status_code=400, detail="goal must be non-empty")
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    task_entry = {
        "taskId": task_id,
        "goal": req.goal.strip(),
        "initiator": req.initiator,
        "status": "in_progress",
        "results": {},
        "createdAt": time.time(),
    }
    active_swarm_tasks[task_id] = task_entry
    add_event("SWARM_TASK_INITIATED", f"Task {task_id}: {req.goal.strip()}", None)

    # If gateway ws is connected, broadcast coordinate message
    if gateway_ws_conn is not None:
        try:
            await gateway_ws_conn.send(json.dumps({
                "type": "SWARM_COORDINATE",
                "taskId": task_id,
                "goal": req.goal.strip(),
                "payload": req.parameters or {},
            }))
        except Exception as e:
            add_event("SWARM_ERROR", f"Failed to dispatch to gateway: {e}", None)

    return {"task_id": task_id, "status": "in_progress", "goal": req.goal.strip()}

@app.get("/mesh/tasks/{task_id}")
def get_swarm_task(task_id: str):
    t = active_swarm_tasks.get(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    return t

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
