#!/usr/bin/env bash
# CrabDeck origin launcher — binds services on 0.0.0.0 so Cloudflare / a
# reverse proxy can handshake the host. Run from the repo root:
#   ./start.sh
#   ./start.sh --gateway-only
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

GATEWAY_ONLY=false
NO_AGENTS=false
NO_UI=false
NO_OLLAMA=false
for arg in "$@"; do
  case "$arg" in
    --gateway-only) GATEWAY_ONLY=true ;;
    --no-agents)    NO_AGENTS=true ;;
    --no-ui)        NO_UI=true ;;
    --no-ollama)    NO_OLLAMA=true ;;
    -h|--help)
      echo "Usage: ./start.sh [--gateway-only] [--no-agents] [--no-ui] [--no-ollama]"
      exit 0
      ;;
  esac
done

RUN_DIR="$ROOT/.crabdeck"
LOG_DIR="$RUN_DIR/logs"
PID_DIR="$RUN_DIR/pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

load_env() {
  local file="$1"
  if [ -f "$file" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$file"
    set +a
  fi
}

already_running() {
  local name="$1" pidfile="$PID_DIR/$name.pid"
  if [ -f "$pidfile" ]; then
    local pid
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    rm -f "$pidfile"
  fi
  return 1
}

spawn() {
  local name="$1"
  shift
  if already_running "$name"; then
    echo "  [skip] $name already running (pid $(cat "$PID_DIR/$name.pid"))"
    return 0
  fi
  echo "  [start] $name"
  nohup "$@" >"$LOG_DIR/$name.log" 2>&1 &
  echo $! >"$PID_DIR/$name.pid"
}

wait_http() {
  local url="$1" label="$2" attempts="${3:-30}"
  local i
  for i in $(seq 1 "$attempts"); do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      echo "  [ok]   $label  $url"
      return 0
    fi
    sleep 0.4
  done
  echo "  [fail] $label did not become ready: $url" >&2
  echo "         log: $LOG_DIR/${label}.log" >&2
  return 1
}

echo ""
echo "  🦀  CrabDeck origin start"
echo "      bind host defaults to 0.0.0.0 (required to clear Cloudflare 521)"
echo ""

# 1. Ollama (optional)
if [ "$GATEWAY_ONLY" = false ] && [ "$NO_OLLAMA" = false ] && command -v ollama >/dev/null 2>&1; then
  spawn ollama ollama serve
  sleep 1
fi

# 2. Gateway — this is the Cloudflare origin health target (:8765)
unset HOST PORT ORIGIN_PORT GATEWAY_TOKEN ALLOWED_ORIGINS || true
load_env "$ROOT/gateway/.env"
export HOST="${HOST:-0.0.0.0}"
GW_PORT="${PORT:-8765}"
export PORT="$GW_PORT"
if [ ! -d "$ROOT/gateway/node_modules" ]; then
  echo "  [deps] gateway npm install"
  (cd "$ROOT/gateway" && npm install --prefer-offline --no-fund --no-audit)
fi
spawn gateway /usr/bin/env node "$ROOT/gateway/server.js"
wait_http "http://127.0.0.1:${GW_PORT}/health" gateway || {
  echo "  Gateway failed to bind. Last log lines:" >&2
  tail -n 40 "$LOG_DIR/gateway.log" >&2 || true
  exit 1
}

if [ "$GATEWAY_ONLY" = true ]; then
  echo ""
  echo "  Gateway-only mode. Origin health: http://127.0.0.1:${GW_PORT}/health"
  echo "  Stop: ./stop.sh"
  echo ""
  exit 0
fi

# 3. Orchestrator — must bind 0.0.0.0 (uvicorn defaults to 127.0.0.1)
unset HOST PORT || true
load_env "$ROOT/orchestrator/.env"
ORCH_HOST="${HOST:-0.0.0.0}"
ORCH_PORT="${PORT:-8000}"
export HOST="$ORCH_HOST"
export PORT="$ORCH_PORT"
if [ ! -x "$ROOT/orchestrator/.venv/bin/uvicorn" ]; then
  echo "  [deps] orchestrator venv"
  python3 -m venv "$ROOT/orchestrator/.venv"
  "$ROOT/orchestrator/.venv/bin/pip" install -q -r "$ROOT/orchestrator/requirements.txt"
fi
spawn orchestrator "$ROOT/orchestrator/.venv/bin/uvicorn" main:app \
  --host "${ORCH_HOST}" --port "${ORCH_PORT}" --app-dir "$ROOT/orchestrator"
wait_http "http://127.0.0.1:${ORCH_PORT}/health" orchestrator || true

# 4. Agents
if [ "$NO_AGENTS" = false ]; then
  load_env "$ROOT/agents/.env"
  if [ ! -x "$ROOT/agents/.venv/bin/python" ]; then
    echo "  [deps] agents venv"
    python3 -m venv "$ROOT/agents/.venv"
    "$ROOT/agents/.venv/bin/pip" install -q -r "$ROOT/agents/requirements.txt"
  fi
  spawn hermes   "$ROOT/agents/.venv/bin/python" "$ROOT/agents/hermes_agent.py"
  spawn openclaw "$ROOT/agents/.venv/bin/python" "$ROOT/agents/openclaw_agent.py"
fi

# 5. UI
if [ "$NO_UI" = false ]; then
  if [ ! -d "$ROOT/ui/node_modules" ]; then
    echo "  [deps] ui npm install"
    (cd "$ROOT/ui" && npm install --prefer-offline --no-fund --no-audit)
  fi
  spawn ui /usr/bin/env npm --prefix "$ROOT/ui" run dev
fi

echo ""
echo "  ✅  CrabDeck origin is listening"
echo "      Gateway      → http://0.0.0.0:8765/health"
echo "      Orchestrator → http://0.0.0.0:8000/health"
echo "      UI           → http://localhost:5173"
echo ""
echo "  Cloudflare 521 clears only after THIS host is the orange-cloud origin"
echo "  and something answers on :80/:443 (docker compose origin service)."
echo "  Stop: ./stop.sh"
echo ""
