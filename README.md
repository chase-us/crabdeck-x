# 🦀 CrabDeck v2.2 — Hermes + OpenClaw Edition

Installable, single-command launcher for the full CrabDeck stack:
**Gateway → Orchestrator → Hermes (Ollama LLM) → OpenClaw (Sovereign Agent) → React UI**

> **v2.2** hardens v2.1 for publication: the gateway now requires a shared
> auth token, OpenClaw's shell execution is off by default, and CORS is
> locked to a real origin list instead of `*`. See `SECURITY.md` before you
> point a public domain at this.

---

## 🚀 Quick Install

### Windows
```powershell
powershell -ExecutionPolicy Bypass -File installer\Install-CrabDeck.ps1
```
Creates `%USERPROFILE%\CrabDeck`, installs all deps, builds the Ollama model,
generates a `GATEWAY_TOKEN` and writes it into every service's `.env`, and
puts a **CrabDeck** shortcut on your Desktop.

### Linux / WSL
```bash
chmod +x installer/Install-CrabDeck-Linux.sh
./installer/Install-CrabDeck-Linux.sh
```

Pass `--open-gateway` to either installer if you explicitly want to skip
auth for local-only dev (not recommended once anything is internet-facing).

---

## 📋 Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Node.js | ≥ 18 | https://nodejs.org |
| Python | ≥ 3.10 | https://python.org |
| Ollama | any | https://ollama.com |

---

## ▶️ Launch

**Windows:**
```powershell
powershell -ExecutionPolicy Bypass -File Start-CrabDeck.ps1
```
Or double-click the **CrabDeck** desktop shortcut.

**Linux/WSL:**
```bash
./start.sh
```

Opens: **http://localhost:5173**

### Origin recovery (Cloudflare HTTP 521)

`521 Web Server Is Down` means Cloudflare reached its anycast edge but **nothing accepted TCP on the origin host** (`:80`/`:443`). This repo now binds the gateway on `0.0.0.0` (not loopback) and ships an origin edge.

On the machine Cloudflare is proxied to:

```bash
./scripts/origin-diagnose.sh          # process / port / local health / public 521
./start.sh --gateway-only             # restore :8765  →  curl http://127.0.0.1:8765/health
docker compose up -d                  # publish :80 via Caddy → gateway /health
```

`GET /`, `/health`, and `/ready` on the gateway all return `{"status":"ok",...}` so origin probes succeed. `ORIGIN_PORT=80,443` adds extra HTTP listeners when the process has bind privilege.

---

## 🏗️ Architecture

```
User (Browser)
     │
     ▼ http://localhost:5173
CrabDeck UI (React + Vite)
     │
     │ WebSocket ws://localhost:8765  (HELLO + GATEWAY_TOKEN required)
     ▼
CrabDeck Gateway (Node.js)
     │               │
     ▼               ▼
 Hermes Agent    OpenClaw Agent
 (Python)        (Python, shell exec OFF by default)
     │               │
     ▼               ▼
 Ollama :11434   System / OpenClaw.app
 (llama3/crabdeck)

Health REST:
  UI → GET /api/agents → Orchestrator :8000  (CORS locked to ALLOWED_ORIGINS)
```

---

## 🤖 Agents

### ⚡ Hermes — `agents/hermes_agent.py`
- Connects to Gateway as `hermes` (authenticates with `GATEWAY_TOKEN`)
- Receives `PROMPT` events → calls Ollama → sends `HERMES_RESPONSE`
- Supports model selection from the UI dropdown (prefers the custom
  `crabdeck` model if it's been built, falls back to whatever's installed)
- Heartbeats every 10 s

### 🦅 OpenClaw — `agents/openclaw_agent.py`
- Connects to Gateway as `openclaw` (authenticates with `GATEWAY_TOKEN`)
- Receives `TASK` events → reasons with Ollama → **can** execute shell
  commands, but only if `ENABLE_SHELL_EXEC=1` is set (default: off)
- Integrates with OpenClaw.app REST API if running on port 3131
- Reports `TASK_RESULT` back to the UI
- Heartbeats every 10 s

---

## 📡 Services

| Service | Port | Start |
|---------|------|-------|
| CrabDeck UI | 5173 | `cd ui && npm run dev` |
| Gateway | 8765 | `cd gateway && node server.js` |
| Orchestrator | 8000 | `cd orchestrator && .venv/Scripts/uvicorn main:app --port 8000` |
| Hermes Agent | — | `cd agents && .venv/Scripts/python hermes_agent.py` |
| OpenClaw Agent | — | `cd agents && .venv/Scripts/python openclaw_agent.py` |
| Ollama | 11434 | `ollama serve` |

---

## 🗂️ Project Layout

```
CrabDeck/
├── SECURITY.md                    ← read before deploying publicly
├── installer/
│   ├── Install-CrabDeck.ps1       ← Windows installer (generates GATEWAY_TOKEN)
│   └── Install-CrabDeck-Linux.sh  ← Linux/WSL installer
├── ui/                            React + Vite frontend
│   ├── src/CrabDeck.jsx           Main app (Hermes + OpenClaw tabs)
│   ├── .env.example
│   └── .env.local                 (created by installer)
├── gateway/
│   ├── server.js                  WebSocket agent bus (token-authed)
│   ├── package.json
│   └── .env.example
├── orchestrator/
│   ├── main.py                    FastAPI health tracker
│   ├── requirements.txt
│   └── .env.example
├── agents/
│   ├── hermes_agent.py
│   ├── openclaw_agent.py
│   ├── hermes.yaml
│   ├── openclaw.yaml
│   ├── requirements.txt           websockets + requests
│   └── .env.example
├── ollama/
│   └── Modelfile                  crabdeck custom model
├── Start-CrabDeck.ps1             repo-root dev launcher
├── Stop-CrabDeck.ps1
├── Manage-LLM.ps1                 Ollama watchdog
└── README.md
```

---

## 🔑 Environment Variables

Every service has a `.env.example` — copy to `.env` (or `.env.local` for
`ui/`) and fill in. The installer does this for you automatically and keeps
`GATEWAY_TOKEN` consistent across all four.

```
# gateway/.env, orchestrator/.env, agents/.env all share:
GATEWAY_TOKEN=<generated by installer>

# ui/.env.local additionally exposes it to the frontend as:
VITE_GATEWAY_TOKEN=<same value>
```

---

## 🛠️ Manual Start (individual services)

```powershell
# Terminal 1 — Ollama
ollama serve

# Terminal 2 — Gateway
cd gateway; node server.js

# Terminal 3 — Orchestrator
cd orchestrator; .venv\Scripts\uvicorn main:app --port 8000

# Terminal 4 — Hermes Agent
cd agents; .venv\Scripts\python hermes_agent.py

# Terminal 5 — OpenClaw Agent
cd agents; .venv\Scripts\python openclaw_agent.py

# Terminal 6 — UI
cd ui; npm run dev
```

---

## 🔌 OpenClaw.app Integration

If OpenClaw.app is running and exposes a REST API on `http://localhost:3131`,
the OpenClaw agent will automatically route tasks through it first before
falling back to Ollama reasoning.
