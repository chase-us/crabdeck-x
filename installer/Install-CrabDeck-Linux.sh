#!/usr/bin/env bash
# CrabDeck v2.2 — Linux/WSL Installer
# Usage: chmod +x installer/Install-CrabDeck-Linux.sh && ./installer/Install-CrabDeck-Linux.sh [install_dir] [--open-gateway]

set -e
INSTALL_DIR="${1:-$HOME/CrabDeck}"
OPEN_GATEWAY=false
for arg in "$@"; do
  [ "$arg" = "--open-gateway" ] && OPEN_GATEWAY=true
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_DIR="$(dirname "$SCRIPT_DIR")"

cyan()  { echo -e "\033[36m  🦀  $*\033[0m"; }
green() { echo -e "\033[32m  ✅  $*\033[0m"; }
warn()  { echo -e "\033[33m  ⚠   $*\033[0m"; }
fail()  { echo -e "\033[31m  ✗   $*\033[0m"; exit 1; }
info()  { echo -e "      $*"; }

echo ""
echo "  ████████████████████████████████████████████"
echo "  🦀  CRABDECK v2.2 INSTALLER (Linux/WSL)"
echo "      Hermes + OpenClaw Edition"
echo "  ████████████████████████████████████████████"
echo ""

# ── Prereqs ───────────────────────────────────────────────────────────────────
cyan "Checking prerequisites…"
command -v node    >/dev/null 2>&1 && green "Node.js $(node --version)" || fail "Node.js not found. Install: https://nodejs.org"
command -v npm     >/dev/null 2>&1 || fail "npm not found."
command -v python3 >/dev/null 2>&1 && green "Python $(python3 --version)" || fail "Python3 not found."

OLLAMA_OK=true
command -v ollama >/dev/null 2>&1 && green "Ollama $(ollama --version)" || { warn "Ollama not found — install: https://ollama.com"; OLLAMA_OK=false; }

# ── Copy files ────────────────────────────────────────────────────────────────
cyan "Installing to $INSTALL_DIR …"
mkdir -p "$INSTALL_DIR"
cp -r "$SOURCE_DIR"/. "$INSTALL_DIR/"
green "Files copied"

# ── npm installs ──────────────────────────────────────────────────────────────
cyan "Installing Gateway npm dependencies…"
(cd "$INSTALL_DIR/gateway" && npm install --prefer-offline) && green "Gateway npm done"

cyan "Installing UI npm dependencies…"
(cd "$INSTALL_DIR/ui" && npm install --prefer-offline) && green "UI npm done"

# ── Python venvs ──────────────────────────────────────────────────────────────
cyan "Creating Orchestrator Python venv…"
(cd "$INSTALL_DIR/orchestrator" \
  && python3 -m venv .venv \
  && .venv/bin/pip install -q -r requirements.txt) && green "Orchestrator venv ready"

cyan "Creating Agents Python venv (Hermes + OpenClaw)…"
(cd "$INSTALL_DIR/agents" \
  && python3 -m venv .venv \
  && .venv/bin/pip install -q -r requirements.txt) && green "Agents venv ready"

# ── Ollama model ──────────────────────────────────────────────────────────────
if [ "$OLLAMA_OK" = true ]; then
    cyan "Building Ollama 'crabdeck' model…"
    ollama pull llama3 && \
    ollama create crabdeck -f "$INSTALL_DIR/ollama/Modelfile" && \
    green "crabdeck model built" || warn "Model build failed — run manually: ollama create crabdeck -f ollama/Modelfile"
fi

# ── Generate shared GATEWAY_TOKEN and write .env files ────────────────────────
cyan "Generating gateway auth token and writing service configs…"
if [ "$OPEN_GATEWAY" = true ]; then
    TOKEN=""
    warn "Installed with --open-gateway: the gateway will run WITHOUT auth. Localhost-only use intended."
else
    TOKEN="$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))')"
    green "Generated GATEWAY_TOKEN (stored in each service's .env, not printed above)"
fi

cat > "$INSTALL_DIR/gateway/.env" << ENV
HOST=0.0.0.0
PORT=8765
GATEWAY_TOKEN=$TOKEN
ALLOWED_ORIGINS=http://localhost:5173
ENV

cat > "$INSTALL_DIR/orchestrator/.env" << ENV
GATEWAY_URL=ws://localhost:8765
GATEWAY_TOKEN=$TOKEN
ALLOWED_ORIGINS=http://localhost:5173
HOST=0.0.0.0
PORT=8000
ENV

cat > "$INSTALL_DIR/agents/.env" << ENV
GATEWAY_URL=ws://localhost:8765
GATEWAY_TOKEN=$TOKEN
OLLAMA_URL=http://localhost:11434
DEFAULT_MODEL=llama3
OPENCLAW_HTTP=http://localhost:3131
ENABLE_SHELL_EXEC=0
SHELL_ALLOWLIST=
ENV

cat > "$INSTALL_DIR/ui/.env.local" << ENV
VITE_GATEWAY_WS=ws://localhost:8765
VITE_OLLAMA_ENDPOINT=http://localhost:11434
VITE_ORCHESTRATOR=http://localhost:8000
VITE_GATEWAY_TOKEN=$TOKEN
ENV

green ".env files written for gateway, orchestrator, agents, ui"
[ "$OPEN_GATEWAY" = false ] && info "OpenClaw shell execution is OFF by default. See agents/openclaw_agent.py before setting ENABLE_SHELL_EXEC=1."

# ── Launcher scripts (repo start.sh binds HOST=0.0.0.0 for Cloudflare) ──────
chmod +x "$INSTALL_DIR/start.sh" "$INSTALL_DIR/stop.sh" "$INSTALL_DIR/scripts/origin-diagnose.sh"
green "start.sh / stop.sh ready"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  ████████████████████████████████████████████"
echo -e "\033[32m  ✅  CrabDeck v2.2 installed to $INSTALL_DIR\033[0m"
echo ""
echo "  Launch:  $INSTALL_DIR/start.sh"
echo ""
echo "  Services:"
echo "    UI           → http://localhost:5173"
echo "    Gateway      → ws://localhost:8765"
echo "    Orchestrator → http://localhost:8000"
echo "    Hermes       → python agents/hermes_agent.py"
echo "    OpenClaw     → python agents/openclaw_agent.py"
echo ""
echo "  Before exposing this on hermes-claw.ai, read SECURITY.md."
echo "  ████████████████████████████████████████████"
echo ""
