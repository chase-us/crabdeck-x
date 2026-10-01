# CrabDeck v2.1 → v2.2 — Changes

## Fixed bugs
- **`ui/.env.local` was dead config.** The installer wrote it, but
  `CrabDeck.jsx` hardcoded `ws://localhost:8765` / `http://localhost:11434`
  instead of reading `import.meta.env`. Now reads
  `VITE_GATEWAY_WS` / `VITE_OLLAMA_ENDPOINT` / `VITE_GATEWAY_TOKEN` with
  localhost fallbacks.
- **Two different `requirements.txt` files collided.** The flat file bundle
  you get when a multi-folder repo is exported without its directory
  structure had silently merged `agents/requirements.txt`
  (`websockets`, `requests`) and `orchestrator/requirements.txt`
  (`fastapi`, `uvicorn`, `pydantic`, `psutil`, `websockets`) into one file —
  only the agents version survived. Reconstructed both into their correct
  folders from the actual document content.
- **`vite.config.js` had a leftover typo** in a comment
  (`FastAPI / kkkkk`) — cosmetic, removed.
- **Missing `package.json`** for both `gateway/` and `ui/` — neither had
  one, so `npm install` had nothing to install against. Added both;
  verified `npm install` + a real `vite build` succeed.
- **`gateway/server.js` used the `uuid` package** for client IDs when
  Node's built-in `crypto.randomUUID()` does the same thing — dropped the
  extra dependency.
- **`orchestrator/main.py` used the deprecated `@app.on_event("startup")`**
  hook — migrated to FastAPI's `lifespan` context manager.

## Security fixes (see SECURITY.md for the full writeup)
- **Gateway had no authentication.** Any WebSocket client could claim to be
  `openclaw` or `hermes` and receive real task/prompt traffic, or claim to
  be the UI and push tasks to agents. Added a shared-secret `GATEWAY_TOKEN`
  required on `HELLO`; role is now locked once set. Verified live with a
  4-case handshake test (reject-no-token, accept-good-token,
  block-unauthenticated-routing, authenticated-routing-works).
- **OpenClaw's shell executor was always on**, with no auth in front of it —
  an unauthenticated caller could get the LLM to emit a `<CMD>` block and
  have it run with the agent's OS permissions. `ENABLE_SHELL_EXEC` now
  defaults to `0`, with an optional `SHELL_ALLOWLIST` when you do turn it on.
- **Orchestrator CORS was `allow_origins=["*"]`.** Now reads
  `ALLOWED_ORIGINS` (default `http://localhost:5173`); verified live that a
  disallowed origin gets no `Access-Control-Allow-Origin` header back.
- Installers now generate one `GATEWAY_TOKEN` per install and write it
  consistently into `gateway/.env`, `orchestrator/.env`, `agents/.env`, and
  `ui/.env.local`. `Start-CrabDeck.ps1` / `start.sh` load each `.env` before
  spawning the corresponding process.

## Removed from the uploads (not part of CrabDeck)
These were in the upload batch but have nothing to do with this project —
excluded entirely, not modified or analyzed further:
- `open.java`, `MainActivity.java`, `AndroidManifest.xml`, `strings.xml`,
  `styles.xml`, `layout_main.xml` — auto-generated Android template
  boilerplate (package name is literally the placeholder string
  `"java -jar engine.jar -i /path/to/your/sample.apk"`).
- `package.txt` — despite the name, this is obfuscated Lua bytecode, not a
  `package.json`.
- `new_4_.py` — despite the `.py` extension, this is NDJSON internet-scan
  data (SSL chain hashes, HTTP banners, host IPs), not a Python script.
- `dax-default.xml` — Dolby Audio (DAX) driver EQ calibration data.
- `DQE_coef_data.xml` — display panel HDR/color calibration data.
- `SKILL.md` (from the project knowledge, not the uploads) — this is
  Anthropic's own `mcp-builder` skill documentation, not a CrabDeck file.

## Heads-up: architecture mismatch with project memory
What you uploaded/had on file is the **v2.1 "Hermes + OpenClaw Edition"**
stack: plain Vite + React UI, a single Node `ws` gateway, FastAPI
orchestrator, two Python agent processes, Ollama. That's what this v2.2
package cleans up and hardens.

Separately, prior context describes a **different, more advanced
architecture** already in progress — npm workspaces monorepo, a Tauri v2
"Bhive UI", Redis (ioredis) pub/sub event bus with AJV-validated JSON
Schema, `better-sqlite3` persistence, a five-agent model (Hermes /
OpenClaw / Gateway / Orchestrator / Quantum Core) — described as having a
Phase 1–2 "data highway" already production-ready. None of that code was
in this upload, so it isn't part of this package. If hermes-claw.ai is
meant to ship the Redis/Tauri/monorepo version rather than this v2.1/v2.2
ws-gateway version, say so and that's a different (larger) packaging job.
