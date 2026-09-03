#!/usr/bin/env bash
# Stop processes started by ./start.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PID_DIR="$ROOT/.crabdeck/pids"

if [ ! -d "$PID_DIR" ]; then
  echo "  No .crabdeck/pids directory — nothing to stop."
  exit 0
fi

stopped=0
for pidfile in "$PID_DIR"/*.pid; do
  [ -f "$pidfile" ] || continue
  name="$(basename "$pidfile" .pid)"
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
    echo "  [stop] $name ($pid)"
    kill "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    stopped=$((stopped + 1))
  fi
  rm -f "$pidfile"
done

echo "  Stopped $stopped CrabDeck origin process(es)."
