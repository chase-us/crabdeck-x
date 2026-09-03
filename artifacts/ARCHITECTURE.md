# CrabDeck Quantum architecture

Snapshot after the Shell Cracked + bHive sprint. This is the v2.2 ws-gateway stack plus a persistent memory vault — not the Redis/Tauri monorepo described in older notes.

## Processes

```
                    ┌──────────────┐
                    │  Browser UI  │  :5173  Vite / React / Tailwind
                    │  CrabDeck +  │
                    │  Telemetry   │
                    └──────┬───────┘
           WS HELLO+token  │  HTTP /vault /gw /api (dev proxies)
                           ▼
                    ┌──────────────┐
                    │   Gateway    │  :8765  Express + WebSocket bus
                    │  bHive wd 20s│
                    └─┬───┬───┬────┘
                      │   │   │ fail-open POST heartbeat
         TASK/PROMPT  │   │   ▼
                      │   │  ┌─────────────┐
                      │   │  │ Shell       │ :7070
                      │   │  │ Cracked     │ SQLite WAL + vectors
                      │   │  └─────────────┘
                      ▼   ▼
              Hermes     OpenClaw      Orchestrator :8000
              Ollama     TASK/opt-in   health + AGENT_STATUS
              :11434     shell
                 ╲          │          ╱
                  ╲   swarm mesh      ╱     SWARM_ROUND fan-out · SWARM_PEER echo
                   ╲  (via gateway)  ╱      MESH peer↔peer · Hermes synthesizes
                    ╲───────┼───────╱       result → vault swarm_result (RAG seed
                            ▼                for the next swarm)
```

## Swarm mesh

The gateway is still the only socket hop, but agent roles now behave as a mesh: a `SWARM_TASK` is retrieved against the vault (RAG seed), fanned out to every connected peer as `SWARM_ROUND`, each contribution is echoed to the other peers and rolled into the next round, and Hermes synthesizes the final `SWARM_RESULT`, which is written back into Shell Cracked. Addressed `MESH` frames let any peer ask another directly. Contract: `SWARM_MESH_PROTOCOL.md`. UI: the Swarm tab.

## Trust boundaries

| Path | Auth |
| ---- | ---- |
| Browser → Gateway WS | `HELLO.token` == `GATEWAY_TOKEN` when set; Origin allow-list |
| Browser → Vault HTTP | optional `VAULT_TOKEN` on writes; CORS allow-list |
| Agents → Gateway WS | same shared token |
| Agents → Vault HTTP | `X-Vault-Token` if configured; 1.5s timeout |
| Gateway → Vault | same; never blocks ACK |

## Persistence

- `vault/data/shell_cracked.db` — agents, heartbeats, events, sessions (WAL).
- `vault/data/vectors-sqlite` — blake2 384-d embeddings (default).
- Optional Chroma directory when `VAULT_VECTOR_BACKEND=chroma`.

## Related files

- Skills: `.cursor/skills/`
- Protocol: `artifacts/BHIVE_PROTOCOL.md`, `artifacts/SWARM_MESH_PROTOCOL.md`
- Vault HTTP: `artifacts/SHELL_CRACKED_API.md`
- Operator: `artifacts/OPERATOR_RUNBOOK.md`
