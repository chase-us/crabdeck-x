# CrabDeck edge Worker

Public Cloudflare Worker for `hermesclaw.ai`. It is the **orange-cloud origin**:
health probes hit this isolate, not a VPS. A down local swarm returns **502/503**
from the Worker — not Cloudflare **521**.

This Worker does **not** replace the Node gateway bus, Python agents, vault, or
Ollama. Those stay on the operator host. The Worker:

- Streams HTTP to `ORIGIN_URL` / vault / orchestrator (no unbounded `response.text()`)
- Passes WebSocket upgrades through to the Node gateway (the mesh still lives there)
- Rate-limits via the Workers Rate Limit binding (not a module-level `Map`)
- Compares `EDGE_TOKEN` with SHA-256 + `timingSafeEqual`
- Emits structured JSON logs and enables Workers Logs/Traces in `wrangler.jsonc`

## Local

```bash
cd workers/crabdeck-edge
cp .dev.vars.example .dev.vars   # optional EDGE_TOKEN
npm install
npx wrangler types
npm run check
npx wrangler dev                 # development only — never wrangler deploy from this VM
```

Point the UI WebSocket at the wrangler port for an edge-in-the-loop check, or
keep `ws://127.0.0.1:8765` for local-only operator work.

## Secrets

```bash
npx wrangler secret put EDGE_TOKEN
```

`EDGE_TOKEN` is **not** `GATEWAY_TOKEN`. The browser still sends `GATEWAY_TOKEN`
on WebSocket `HELLO` to the Node bus. `EDGE_TOKEN` gates proxied HTTP (`/gw`,
`/vault`, `/api`) when set. Leave it empty only on localhost.

## Custom domain (clears 521)

1. Deploy from a machine with Cloudflare account auth (`npx wrangler deploy` is
   **production only**).
2. Uncomment `routes` in `wrangler.jsonc` (`custom_domain: true` for
   `hermesclaw.ai`) **or** add the domain in the dashboard.
3. Set `ORIGIN_URL` to the real swarm host (Tunnel / VPS). This checkout is not
   that origin.

Until step 2, `https://hermesclaw.ai/` can still 521 — the Worker is not attached.
