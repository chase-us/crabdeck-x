#!/usr/bin/env bash
# CrabDeck swarm mesh smoke test — health probes + unit tests + optional live dispatch.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:8765}"
VAULT_URL="${VAULT_URL:-http://127.0.0.1:7070}"
ORCH_URL="${ORCH_URL:-http://127.0.0.1:8000}"
PYTHON="${CRABDECK_PYTHON:-python3}"

pass() { printf '\033[32m✓\033[0m %s\n' "$*"; }
fail() { printf '\033[31m✗\033[0m %s\n' "$*"; exit 1; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }

probe() {
  local name="$1" url="$2"
  if curl -sf --max-time 3 "$url" >/dev/null; then
    pass "$name reachable ($url)"
    return 0
  fi
  warn "$name not reachable ($url)"
  return 1
}

cd "$ROOT"

echo "=== CrabDeck Swarm Smoke ==="

probe "Gateway" "$GATEWAY_URL/health" || true
probe "Vault" "$VAULT_URL/health" || true
probe "Orchestrator" "$ORCH_URL/health" || true

echo "--- Python unit tests ---"
cd "$ROOT/agents"
"$PYTHON" -m pytest test_swarm_rag.py test_load_balancer.py -q

echo "--- Gateway unit tests ---"
cd "$ROOT/gateway"
node --test test_swarm.js test_swarm_router.js

if curl -sf --max-time 2 "$GATEWAY_URL/health" >/dev/null; then
  echo "--- Swarm REST status ---"
  curl -sf "$GATEWAY_URL/api/swarm/status" | "$PYTHON" -m json.tool || warn "swarm status endpoint unavailable"
fi

if [[ "${SMOKE_LIVE_DISPATCH:-0}" == "1" ]]; then
  echo "--- Live dispatch (async) ---"
  curl -sf -X POST "$GATEWAY_URL/api/swarm/dispatch" \
    -H 'Content-Type: application/json' \
    -d '{"goal":"smoke test ping","async":true}' | "$PYTHON" -m json.tool \
    || warn "live dispatch skipped (swarm offline?)"
fi

pass "Smoke complete"
