# Sprint artifact — Shell Cracked + bHive

Work landed on `cursor/shell-cracked-bhive-916a` (PR #6) and documented here so later agents do not rediscover it.

## Intent

Keep gateway watchdogs green during long Ollama/task work, persist swarm heartbeats and prompt/task excerpts, and give operators a live telemetry surface.

## Code map

| Area | Files |
| ---- | ----- |
| Vault | `vault/app.py`, `sqlite_store.py`, `vectors.py`, `bhive.py` |
| Vault tests | `vault/test_vault.py`, `vault/test_app.py` |
| Agent offload | `agents/offload.py` |
| Agent vault | `agents/vault_client.py` |
| Hermes / OpenClaw | `agents/hermes_agent.py`, `agents/openclaw_agent.py` |
| Agent tests | `agents/test_event_loop_offload.py` |
| Gateway | `gateway/server.js`, `bhive.js`, `vault_client.js` |
| Gateway tests | `gateway/test_bhive.js` |
| UI | `ui/src/Telemetry.jsx`, `CrabDeck.jsx`, `vite.config.js`, Tailwind configs |
| Orchestrator | `orchestrator/main.py` heartbeat + vault emit |
| Installers | `installer/Install-CrabDeck-Linux.sh`, `Install-CrabDeck.ps1` |

## Decisions

- Blake2 384-d embeddings so the vault runs air-gapped without an embedding API. Chroma is optional, same embed function.
- Fail-open vault HTTP (1.5s). Heartbeat ACK always wins.
- `remember` / `emit` injectors so unit tests never hit the network.
- Allow `127.0.0.1:5173` in addition to `localhost` — Playwright and some browsers use loopback and were 403'd.
- Hash-embedding tests query **exact** stored text.

## Verification recorded

- Vault unittest: 23 passed
- Agents unittest: 16 passed
- Gateway `node --test`: 4 passed
- `ui` `vite build` succeeded
- Browser: Telemetry slot, vault service, Hermes watch, vector hit; Gateway connected after origin fix

## Follow-ups (not in that PR)

- Production origin 521 on hermesclaw.ai still needs real host/tunnel access.
- Semantic embeddings if operators need paraphrase search.
- Rate limits on PROMPT/TASK (called out in SECURITY.md).
