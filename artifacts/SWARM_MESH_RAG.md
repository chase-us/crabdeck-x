# Swarm Mesh & Collaborative RAG Specification

## Architecture Overview

CrabDeck Quantum Swarm Mesh enables autonomous, decentralized collaboration between all agents in the fleet (Hermes, OpenClaw, Orchestrator, and CrabDeck UI), using grounded Retrieval-Augmented Generation (RAG) backed by the Shell Cracked vector vault.

```
                    ┌────────────────────────────┐
                    │      Browser UI            │ :5173  Vite / React / Tailwind
                    │  CrabDeck + Swarm Mesh View│
                    └─────────────┬──────────────┘
            WS HELLO+token / Swarm│  HTTP /vault /api /gw (Proxies)
                                  ▼
                    ┌────────────────────────────┐
                    │      CrabDeck Gateway      │ :8765  Express + WebSocket Mesh
                    │     Swarm Mesh Router      │
                    └─────┬───────┬───────┬──────┘
                          │       │       │ fail-open RAG & heartbeat
     P2P Swarm Messages   │       │       ▼
     & Task Dispatches    │       │ ┌────────────────────────┐
                          │       │ │ Shell Cracked Vault    │ :7070
                          │       │ │ RAG Retrieval & Memory │ SQLite WAL + Cosine Vectors
                          │       │ └────────────────────────┘
                          ▼       ▼               ▲
                  Hermes         OpenClaw         │
                  LLM Relay &    Sovereign Node & │
                  RAG Synthesis  System Agent     │
                  :11434 Ollama  P2P Mesh         │
                          │       │               │
                          └───────┴───────────────┘
                            Cross-Agent RAG Query
                            & Vector Memory Sync
```

## Protocol Specifications

### 1. Peer-to-Peer Mesh Messaging (`SWARM_MESSAGE`)
Direct agent-to-agent communication routed across the gateway mesh bus without centralized polling:
- `from`: Initiating agent role (`hermes`, `openclaw`, `orchestrator`, `crabdeck-ui`)
- `target`: Recipient agent role
- `action`: Specific operation (`COLLAB_QUERY`, `EXEC_TASK`, `RAG_VERIFY`, `PEER_REPLY`)
- `corrId`: Correlation UUID to track request/response pairs
- `payload`: Contextual arguments, questions, or execution payload

### 2. Multi-Agent Coordinated Tasks (`SWARM_COORDINATE` / `SWARM_TASK_DISPATCH` / `SWARM_TASK_CONTRIBUTION`)
Decentralized problem solving where all agents contribute their specialized domain capabilities:
- Initiator sends `SWARM_COORDINATE` with `taskId` and `goal`.
- Gateway dispatches `SWARM_TASK_DISPATCH` to all mesh peers.
- Each agent executes asynchronously:
  - **Hermes**: Queries the Shell Cracked vector vault (`POST /v1/rag/retrieve`), gathers relevant citations across historical memories, and synthesizes analytical strategy.
  - **OpenClaw**: Evaluates system state, executes diagnostic checks (under `ENABLE_SHELL_EXEC` safety rules), and produces operational findings.
- Agents submit `SWARM_TASK_CONTRIBUTION` to the mesh.
- Gateway aggregates contributions and completes the task when consensus is reached.

### 3. Swarm RAG Tech (`/v1/rag/retrieve`)
- Shell Cracked vector store indexes all prompt results, task outputs, and system telemetry in 384-dimensional blake2 L2-normalized vector space.
- The `/v1/rag/retrieve` endpoint supports:
  - Cross-agent semantic retrieval
  - Agent-specific filtering (`agent="hermes"` or `agent="openclaw"`)
  - Minimum relevance thresholding (`min_score`)
  - Deterministic multi-source RAG synthesis
  - Structured citations with `[source_id]`, `agent`, `kind`, `score`, and `excerpt`

### 4. Swarm Mesh Topology (`GET /mesh`)
Exposes live mesh topology across Gateway and Orchestrator:
- Node statuses
- Node capabilities
- Active swarm tasks and completion status
- Inter-agent connection metrics
