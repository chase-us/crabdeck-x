---
name: crabdeck-operator-console
description: CrabDeck Vite/React/Tailwind operator UI, telemetry tab, and local proxies. Use when changing CrabDeck.jsx, Telemetry.jsx, vite.config.js, or operator CORS/origins.
---

# Operator console

`ui/` is Vite + React 18 + Tailwind 3. Main deck is inline-styled (`CrabDeck.jsx`); live swarm telemetry is Tailwind (`Telemetry.jsx`).

## Instructions

1. After UI behavior changes, verify in a browser: click tabs, confirm gateway connect, open Telemetry, search memory. A static screenshot is not enough.
2. Keep Vite proxies:

   | Prefix | Target |
   | ------ | ------ |
   | `/ws` | `ws://localhost:8765` (rewrite strip `/ws`) |
   | `/api` | `http://localhost:8000` |
   | `/vault` | `http://localhost:7070` |
   | `/gw` | `http://localhost:8765` |

3. Telemetry must fetch **relative** `/vault` and `/gw` so the browser stays same-origin.
4. Default gateway WS in the UI is `import.meta.env.VITE_GATEWAY_WS || 'ws://localhost:8765'`. Browser `Origin` is the page origin (`http://127.0.0.1:5173` vs `http://localhost:5173`). Both must be in `ALLOWED_ORIGINS` on gateway, vault, and orchestrator.
5. CrabDeck sidebar `crabdeck` opens the **telemetry** tab, not the system log.
6. Do not put secrets other than the shared bus token in `VITE_*`.

## Tabs

- Hermes — prompt → `TO_HERMES`
- OpenClaw — task → `TO_OPENCLAW`
- Telemetry — bHive slot, vault health, gateway metrics, vector search
- System log — raw bus frames

## Examples

**Telemetry poll (5s)**

```javascript
Promise.allSettled([
  fetch('/vault/health'),
  fetch('/vault/v1/bhive'),
  fetch('/gw/health'),
  fetch('/gw/metrics'),
])
```

**Memory search**

```javascript
fetch(`/vault/v1/memory/query?q=${encodeURIComponent(q)}&n=5`)
```

## Performance notes

- Telemetry poll every 5s is enough for watchdog (20s) visibility.
- Production `vite build` does **not** include the dev proxies. Preview against live paths or keep using `npm run dev` for local operator work.

## Troubleshooting

| Symptom | Fix |
| ------- | --- |
| WS 403 | Add page origin to gateway `ALLOWED_ORIGINS` |
| Telemetry vault offline | Vault not on 7070, or not using `npm run dev` proxies |
| Gateway connected but agents red | Agents not running; orchestrator `/api/agents` 500 if orchestrator down (UI swallows) |
| Tailwind classes missing | `index.css` `@tailwind` + `main.jsx` import `./index.css` |
