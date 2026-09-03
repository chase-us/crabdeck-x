---
name: crabdeck-stack
description: Orient on CrabDeck Quantum — ports, processes, security defaults, and where to change code. Use when starting work on hermesclaw.ai / CrabDeck, adding a service, or asking how the swarm is wired.
---

# CrabDeck Quantum stack

Single-host swarm: React operator UI, Node gateway bus, FastAPI orchestrator, Python Hermes/OpenClaw agents, Shell Cracked vault, Ollama.

## Instructions

1. Read `SECURITY.md` before exposing anything beyond localhost.
2. Do not set `ENABLE_SHELL_EXEC=1` unless the operator explicitly asked and `SHELL_ALLOWLIST` is scoped.
3. Do not run `npx convex deploy` for this stack. Development is local processes (`npx convex dev` is unrelated unless a Convex PR is the task).
4. Prefer fail-open vault ingest (timeout 1.5s, swallow network errors) over blocking a heartbeat.
5. Keep `GATEWAY_TOKEN` consistent across gateway, orchestrator, agents, vault (`VAULT_TOKEN` may reuse it), and `ui/.env.local` (`VITE_GATEWAY_TOKEN`).

## Process map

| Service | Bind | Entry |
| ------- | ---- | ----- |
| UI | `5173` | `ui/` Vite + React + Tailwind |
| Gateway | `8765` | `gateway/server.js` Express + `ws` |
| Orchestrator | `8000` | `orchestrator/main.py` FastAPI |
| Vault (Shell Cracked) | `7070` | `vault/app.py` FastAPI |
| Hermes | WS client | `agents/hermes_agent.py` |
| OpenClaw | WS client | `agents/openclaw_agent.py` |
| Ollama | `11434` | `ollama serve` |

Allowed origins default to `http://localhost:5173` and `http://127.0.0.1:5173`. Missing the loopback origin 403s browser WebSockets.

## Data path

```
Browser ──WS HELLO+token──► Gateway :8765
   │                         │  HEARTBEAT_ACK + bhive_slot
   │                         ▼
   │                    fail-open POST /v1/heartbeat ──► Vault :7070
   │
   └── /vault, /gw, /api proxies (Vite) ──► Vault / Gateway HTTP / Orchestrator
```

Hermes/OpenClaw also POST vault heartbeats and memory from worker threads via `run_blocking`.

Swarm mesh (`SWARM_TASK` → `SWARM_ROUND` fan-out → `SWARM_CONTRIBUTION`/`SWARM_PEER` → `SWARM_SYNTHESIS` → `SWARM_RESULT`, plus addressed `MESH` frames) rides the same bus; see `crabdeck-swarm-mesh`.

## Security defaults (do not weaken)

- `ENABLE_SHELL_EXEC=0`
- Gateway `HELLO` requires `GATEWAY_TOKEN` when set; role locks after first HELLO
- Vault writes accept optional `X-Vault-Token` / `Authorization: Bearer`
- CORS is an allow-list, never `*`
- `.env` is gitignored; only `.env.example` ships

## Examples

**Add a new swarm member**

1. Add the lowercase id to `ALLOWED_AGENTS` in `vault/bhive.py` and `agents/vault_client.py`.
2. Register a gateway HELLO `client` role in `gateway/server.js`.
3. Send `HEARTBEAT` with `ts` (unix seconds) and `bhive_slot` every 10s.
4. Offload any `requests` / subprocess work with `agents/offload.py` `run_blocking`.

**Local verify**

```bash
curl -sS http://127.0.0.1:7070/health
curl -sS http://127.0.0.1:8765/health
curl -sS http://127.0.0.1:8765/metrics
```

## Performance notes

- Gateway watchdog: 20s silence → `missed_heartbeat`.
- Heartbeat interval: 10s (agents), 10s (orchestrator → gateway).
- Vault HTTP timeout from agents/gateway: 1.5s.
- Ollama generate timeout: 120s (must stay off the asyncio loop).

## Troubleshooting

| Symptom | Likely cause |
| ------- | ------------ |
| UI `Gateway disconnected` + WS 403 | Origin not in `ALLOWED_ORIGINS` (include `http://127.0.0.1:5173`) |
| Agent `missed_heartbeat` while Ollama is busy | Blocking call on the event loop — use `run_blocking` |
| Vault 400 on heartbeat | Unknown agent or `bhive_slot` more than one minute off `ts` |
| `hermesclaw.ai` HTTP 521 | Cloudflare origin down — this repo is not the live origin host |
