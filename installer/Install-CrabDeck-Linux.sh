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

cyan "Creating Shell Cracked vault Python venv…"
(cd "$INSTALL_DIR/vault" \
  && python3 -m venv .venv \
  && .venv/bin/pip install -q -r requirements.txt) && green "Vault venv ready"

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
PORT=8765
GATEWAY_TOKEN=$TOKEN
ALLOWED_ORIGINS=http://localhost:5173
VAULT_URL=http://localhost:7070
VAULT_TOKEN=$TOKEN
ENV

cat > "$INSTALL_DIR/orchestrator/.env" << ENV
GATEWAY_URL=ws://localhost:8765
GATEWAY_TOKEN=$TOKEN
ALLOWED_ORIGINS=http://localhost:5173
VAULT_URL=http://localhost:7070
VAULT_TOKEN=$TOKEN
ENV

cat > "$INSTALL_DIR/agents/.env" << ENV
GATEWAY_URL=ws://localhost:8765
GATEWAY_TOKEN=$TOKEN
OLLAMA_URL=http://localhost:11434
DEFAULT_MODEL=llama3
OPENCLAW_HTTP=http://localhost:3131
ENABLE_SHELL_EXEC=0
SHELL_ALLOWLIST=
VAULT_URL=http://localhost:7070
VAULT_TOKEN=$TOKEN
ENV

cat > "$INSTALL_DIR/ui/.env.local" << ENV
VITE_GATEWAY_WS=ws://localhost:8765
VITE_OLLAMA_ENDPOINT=http://localhost:11434
VITE_ORCHESTRATOR=http://localhost:8000
VITE_GATEWAY_TOKEN=$TOKEN
ENV

cat > "$INSTALL_DIR/vault/.env" << ENV
PORT=7070
VAULT_TOKEN=$TOKEN
ALLOWED_ORIGINS=http://localhost:5173
VAULT_DATA_DIR=./data
VAULT_VECTOR_BACKEND=sqlite
ENV

green ".env files written for gateway, orchestrator, agents, vault, ui"
[ "$OPEN_GATEWAY" = false ] && info "OpenClaw shell execution is OFF by default. See agents/openclaw_agent.py before setting ENABLE_SHELL_EXEC=1."

# ── Launcher script (sources every .env before starting each service) ───────
cat > "$INSTALL_DIR/start.sh" << LAUNCH
#!/usr/bin/env bash
cd "$INSTALL_DIR"
echo "🦀 CrabDeck v2.2 — Starting all services…"

load_env() { [ -f "\$1" ] && { set -a; source "\$1"; set +a; }; }

# 1. Ollama
if command -v ollama >/dev/null 2>&1; then
    echo "  [1] Starting Ollama…"
    ollama serve &>/tmp/ollama.log &
    sleep 2
fi

# 2. Gateway
load_env gateway/.env
echo "  [2] Starting Gateway (port 8765)…"
(cd gateway && node server.js) &>/tmp/gateway.log &
sleep 1

# 3. Shell Cracked vault
load_env vault/.env
echo "  [3] Starting Shell Cracked vault (port 7070)…"
(cd vault && .venv/bin/uvicorn app:app --host 0.0.0.0 --port 7070) &>/tmp/vault.log &
sleep 1

# 4. Orchestrator
load_env orchestrator/.env
echo "  [4] Starting Orchestrator (port 8000)…"
(cd orchestrator && .venv/bin/uvicorn main:app --port 8000) &>/tmp/orchestrator.log &
sleep 1

# 5. Hermes
load_env agents/.env
echo "  [5] Starting Hermes Agent…"
(cd agents && .venv/bin/python hermes_agent.py) &>/tmp/hermes.log &
sleep 1

# 6. OpenClaw
echo "  [6] Starting OpenClaw Agent…"
(cd agents && .venv/bin/python openclaw_agent.py) &>/tmp/openclaw.log &
sleep 2

# 7. UI
echo "  [7] Starting UI (port 5173)…"
(cd ui && npm run dev) &>/tmp/ui.log &
sleep 3

echo ""
echo "  ✅  CrabDeck is running!"
echo "      UI           → http://localhost:5173"
echo "      Gateway      → ws://localhost:8765"
echo "      Vault        → http://localhost:7070"
echo "      Orchestrator → http://localhost:8000"
echo ""
echo "  Logs: /tmp/{gateway,vault,orchestrator,hermes,openclaw,ui}.log"
echo "  Stop: kill \\\$(lsof -ti :5173 :8765 :8000 :7070) 2>/dev/null; pkill -f hermes_agent; pkill -f openclaw_agent"
LAUNCH
chmod +x "$INSTALL_DIR/start.sh"
green "start.sh written"

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
echo "    Vault        → http://localhost:7070/health"
echo "    Orchestrator → http://localhost:8000"
echo "    Hermes       → python agents/hermes_agent.py"
echo "    OpenClaw     → python agents/openclaw_agent.py"
echo ""
echo "  Before exposing this on hermes-claw.ai, read SECURITY.md."
echo "  ████████████████████████████████████████████"
echo ""
