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
```

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
- Protocol: `artifacts/BHIVE_PROTOCOL.md`
- Vault HTTP: `artifacts/SHELL_CRACKED_API.md`
- Operator: `artifacts/OPERATOR_RUNBOOK.md`
