# Swarm Mesh Protocol

Collaborative multi-agent mesh for CrabDeck v2.3+. All mesh traffic routes through the gateway WebSocket bus (`:8765`) with optional `GATEWAY_TOKEN` auth.

## Mesh agents

| Agent | Role | Client HELLO id |
|-------|------|-----------------|
| **swarm** | Coordinator — RAG retrieve, decompose, delegate, synthesize | `swarm` |
| **hermes** | Reasoning / LLM analysis | `hermes` |
| **openclaw** | Execution / system tasks | `openclaw` |
| **orchestrator** | Health + REST API | `orchestrator` |

## Message types

| Type | Direction | Purpose |
|------|-----------|---------|
| `SWARM_GOAL` | UI → swarm | Start collaborative run |
| `SWARM_DELEGATE` | swarm → peer | Assign subtask with shared RAG context |
| `SWARM_PEER_QUERY` | any → peer | Ask another agent a question |
| `SWARM_PEER_RESPONSE` | peer → swarm/UI | Answer to delegate or query |
| `SWARM_CONTEXT` | swarm → mesh | Broadcast retrieved RAG context |
| `SWARM_RESULT` | swarm → UI | Final synthesized answer |
| `SWARM_BROADCAST` | any → mesh | Fan-out to all mesh agents |
| `SWARM_MESH_STATUS` | any → gateway | Request mesh topology |
| `SWARM_ACK` | gateway → sender | Goal accepted / target offline |

## RAG flow

1. **Retrieve** — Coordinator calls `GET /v1/memory/query?q=...` on Shell Cracked (`:7070`).
2. **Inject** — Retrieved hits are formatted and prepended to each agent prompt.
3. **Share** — `SWARM_CONTEXT` broadcasts context to all mesh peers.
4. **Store** — Each agent writes results to vault (`POST /v1/memory`) for future retrieval.

Hermes and OpenClaw also retrieve RAG context on normal `PROMPT` / `TASK` traffic.

## Example: start a swarm goal

```json
{
  "type": "SWARM_GOAL",
  "session_id": "swarm-abc123",
  "payload": {
    "goal": "Analyze system health and recommend next steps",
    "model": "llama3"
  }
}
```

Coordinator decomposes into Hermes (reason) + OpenClaw (execute), delegates with shared context, waits for `SWARM_PEER_RESPONSE`, synthesizes via Ollama, emits `SWARM_RESULT`.

## Run the mesh

```bash
# Terminal 1 — gateway
cd gateway && node server.js

# Terminal 2 — vault
cd vault && python app.py

# Terminal 3-5 — agents
cd agents && python hermes_agent.py
cd agents && python openclaw_agent.py
cd agents && python swarm_agent.py
```

## Security

- Mesh messages require gateway authentication when `GATEWAY_TOKEN` is set.
- OpenClaw shell execution remains opt-in (`ENABLE_SHELL_EXEC=0` default).
- Vault writes use `X-Vault-Token` / `VAULT_TOKEN`.
