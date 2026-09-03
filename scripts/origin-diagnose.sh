#!/usr/bin/env bash
# Origin host diagnostics for hermesclaw.ai HTTP 521 recovery.
# Usage: ./scripts/origin-diagnose.sh
set -u

echo "=== 1. Process inspection ==="
ps aux | grep -E 'node |uvicorn|nginx|caddy|hermes_agent|openclaw_agent|ollama' | grep -v grep || echo "(no matching origin processes)"

echo ""
echo "=== 2. Port binding ==="
if command -v ss >/dev/null 2>&1; then
  ss -lntp | grep -E ':80 |:443 |:8765 |:8000 |:5173 |:7070 ' || echo "(no listeners on 80/443/8765/8000/5173/7070)"
else
  netstat -lntp 2>/dev/null | grep -E ':80 |:443 |:8765 |:8000 ' || echo "(ss/netstat listeners not found)"
fi

echo ""
echo "=== 3. Local health probe ==="
if curl -sS --max-time 5 http://127.0.0.1:8765/health; then
  echo ""
else
  echo "LOCAL HEALTH FAILED — origin is not bound on :8765"
fi

echo ""
echo "=== 4. Public Cloudflare origin ==="
curl -sSI --max-time 15 https://hermesclaw.ai/ 2>&1 | head -20 || true

echo ""
echo "If step 3 fails:  ./start.sh --gateway-only"
echo "If step 3 works but step 4 is HTTP 521: this host is not the Cloudflare"
echo "origin, or nothing is published on :80/:443. Run: docker compose up -d"
