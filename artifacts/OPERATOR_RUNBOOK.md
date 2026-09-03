# Operator runbook

Local swarm on one machine. Production `hermesclaw.ai` is a separate Cloudflare origin — a green local stack does not clear a 521.

## Start (dev)

From a completed installer directory, `./start.sh` (Linux) or `Start-CrabDeck.ps1` (Windows). Order:

1. Ollama `:11434`
2. Gateway `:8765`
3. Vault `:7070`
4. Orchestrator `:8000`
5. Hermes
6. OpenClaw
7. UI `:5173`

Manual:

```bash
(cd vault && .venv/bin/uvicorn app:app --host 0.0.0.0 --port 7070)
(cd gateway && node server.js)
(cd orchestrator && .venv/bin/uvicorn main:app --port 8000)
(cd agents && .venv/bin/python hermes_agent.py)
(cd agents && .venv/bin/python openclaw_agent.py)
(cd ui && npm run dev)
```

Open **http://127.0.0.1:5173** or **http://localhost:5173**. Both origins are allow-listed.

## Health

```bash
curl -sS http://127.0.0.1:7070/health
curl -sS http://127.0.0.1:8765/health
curl -sS http://127.0.0.1:8765/metrics
curl -sS http://127.0.0.1:8000/health
```

## Telemetry tab

Expect: live `BHIVE SLOT`, `SHELL CRACKED` service name, gateway client count/uptime, agent watch from `/v1/bhive`, vector search against `/v1/memory/query`.

Header **Gateway connected** after HELLO ACK. WS 403 means the page origin is missing from `ALLOWED_ORIGINS`.

## Seed memory (smoke)

```bash
NOW=$(date +%s)
curl -sS -X POST http://127.0.0.1:7070/v1/heartbeat \
  -H 'content-type: application/json' \
  -d "{\"agent\":\"hermes\",\"ts\":$NOW,\"source\":\"runbook\"}"
curl -sS -X POST http://127.0.0.1:7070/v1/memory \
  -H 'content-type: application/json' \
  -d '{"agent":"hermes","kind":"prompt_result","text":"swarm status green"}'
```

Search `swarm status` in Telemetry. With only one document the hit appears even if the hash score is low.

## Tests

```bash
python3 -m unittest discover -s vault
python3 -m unittest discover -s agents
(cd gateway && npm test)
(cd ui && npm run build)
```

## Do not

- Flip `ENABLE_SHELL_EXEC=1` on an internet-facing host without a tight `SHELL_ALLOWLIST`.
- Commit `.env` or `vault/data/`.
- Use `npx convex deploy` for this stack.
- Assume this checkout can restart Cloudflare. 521 is origin/tunnel, not the event-loop or vault code.
