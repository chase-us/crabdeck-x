/**
 * CrabDeck Gateway Server v2.2
 * WebSocket agent bus — bridges CrabDeck UI ↔ OpenClaw ↔ Hermes (Ollama)
 *
 * Run: node server.js
 * Port: 8765 (override with PORT env var)
 *
 * v2.2 changes (security hardening for publication):
 *   - HELLO now requires a shared secret (GATEWAY_TOKEN). Any client that doesn't
 *     present it is kept in role 'unknown' and can only PING — it cannot register
 *     as an agent or route tasks. This closes the v2.1 hole where any websocket
 *     client could call itself "openclaw" or "hermes" and receive TASK/PROMPT
 *     traffic, or call itself "crabdeck-ui" and push TASK/PROMPT to agents.
 *   - Role is locked once set (no re-HELLO to swap identity mid-session).
 *   - Optional Origin allow-list for browser-originated (UI) connections.
 */

const { WebSocketServer, WebSocket } = require('ws')
const http  = require('http')
const crypto = require('crypto')
const { randomUUID } = crypto
const { gatewayBindConfig } = require('./bind')

const BIND          = gatewayBindConfig(process.env)
const PORT          = BIND.port
const HOST          = BIND.host
const GATEWAY_TOKEN  = process.env.GATEWAY_TOKEN || null
const ALLOWED_ORIGINS = (process.env.ALLOWED_ORIGINS || 'http://localhost:5173')
  .split(',').map(s => s.trim()).filter(Boolean)

if (!GATEWAY_TOKEN) {
  console.warn(`
  ⚠️  GATEWAY_TOKEN is not set. The gateway will run in OPEN mode:
      any websocket client can register as 'openclaw' or 'hermes' and
      receive TASK / PROMPT traffic. This is fine for local dev on
      localhost only. DO NOT run this way on a public domain.
      Set GATEWAY_TOKEN before deploying to hermes-claw.ai.
  `)
}

// ── Client registry ────────────────────────────────────────────────────────
// role: 'ui' | 'openclaw' | 'hermes' | 'orchestrator' | 'unknown'
const clients = new Map()   // id → { ws, role, authed, connectedAt, lastSeen }

// ── Agent status store ─────────────────────────────────────────────────────
const agentStatus = {
  openclaw: 'offline',
  hermes:   'offline',
  crabdeck: 'running',
}

function healthPayload() {
  return {
    status:       'ok',
    bind:         { host: HOST, port: PORT, originPorts: BIND.originPorts },
    listeners:    BIND.listeners,
    authRequired: Boolean(GATEWAY_TOKEN),
    clients:      clients.size,
    agentStatus,
    uptime:       process.uptime(),
  }
}

function handleHttp(req, res) {
  const url = (req.url || '/').split('?')[0]
  if (url === '/health' || url === '/ready' || url === '/') {
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify(healthPayload()))
    return
  }
  res.writeHead(404)
  res.end()
}

// ── HTTP health endpoint (also answers `/` so Cloudflare origin probes succeed)
const httpServer = http.createServer(handleHttp)

// ── WebSocket server ───────────────────────────────────────────────────────
const wss = new WebSocketServer({
  server: httpServer,
  verifyClient: (info, done) => {
    // Browser (UI) connections send an Origin header; agent processes (Python/node) don't.
    // Only enforce the allow-list when an Origin is actually present.
    const origin = info.req.headers.origin
    if (origin && ALLOWED_ORIGINS.length && !ALLOWED_ORIGINS.includes(origin)) {
      console.warn(`[ws] rejected connection from disallowed origin: ${origin}`)
      return done(false, 403, 'Origin not allowed')
    }
    done(true)
  },
})

function broadcast(msg, excludeId = null) {
  const raw = JSON.stringify(msg)
  for (const [id, c] of clients) {
    if (id !== excludeId && c.ws.readyState === WebSocket.OPEN) {
      c.ws.send(raw)
    }
  }
}

function sendTo(role, msg) {
  for (const [, c] of clients) {
    if (c.role === role && c.authed && c.ws.readyState === WebSocket.OPEN) {
      c.ws.send(JSON.stringify(msg))
    }
  }
}

function requireAuthed(client, ws) {
  if (!client.authed) {
    ws.send(JSON.stringify({
      type: 'ERROR',
      code: 'UNAUTHENTICATED',
      message: 'Send HELLO with a valid token before issuing this command.',
    }))
    return false
  }
  return true
}

wss.on('connection', (ws) => {
  const id = randomUUID()
  clients.set(id, { ws, role: 'unknown', authed: !GATEWAY_TOKEN, connectedAt: Date.now(), lastSeen: Date.now() })
  console.log(`[+] Client ${id} connected  (${clients.size} total)`)

  ws.send(JSON.stringify({
    type: 'WELCOME',
    clientId: id,
    agentStatus,
    authRequired: Boolean(GATEWAY_TOKEN),
    message: '🦀 CrabDeck Gateway v2.2',
  }))

  ws.on('message', (raw) => {
    let msg
    try { msg = JSON.parse(raw) } catch { return }

    const client = clients.get(id)
    if (!client) return
    client.lastSeen = Date.now()

    const { type, agent, payload } = msg

    switch (type) {

      // ── Handshake ─────────────────────────────────────────────────────────
      case 'HELLO': {
        // Role can only be set once per connection — no re-HELLO identity swap.
        if (client.role !== 'unknown') {
          ws.send(JSON.stringify({ type: 'ACK', clientId: id, role: client.role }))
          break
        }

        if (GATEWAY_TOKEN && msg.token !== GATEWAY_TOKEN) {
          console.warn(`[auth] client ${id} sent HELLO with invalid/missing token`)
          ws.send(JSON.stringify({ type: 'ERROR', code: 'BAD_TOKEN', message: 'Invalid gateway token' }))
          ws.close(4001, 'invalid token')
          break
        }
        client.authed = true

        const role = msg.client === 'crabdeck-ui'   ? 'ui'
                   : msg.client === 'openclaw'       ? 'openclaw'
                   : msg.client === 'hermes'         ? 'hermes'
                   : msg.client === 'orchestrator'   ? 'orchestrator'
                   : 'unknown'
        client.role = role

        if (role === 'openclaw') {
          agentStatus.openclaw = 'running'
          broadcast({ type: 'AGENT_STATUS', agent: 'openclaw', status: 'running' })
          console.log('[openclaw] registered')
        }
        if (role === 'hermes') {
          agentStatus.hermes = 'running'
          broadcast({ type: 'AGENT_STATUS', agent: 'hermes', status: 'running' })
          console.log('[hermes] registered')
        }
        ws.send(JSON.stringify({ type: 'ACK', clientId: id, role }))
        break
      }

      // ── Ping / Pong (always allowed — harmless) ─────────────────────────────
      case 'PING':
        ws.send(JSON.stringify({ type: 'PONG', ts: Date.now(), agentStatus }))
        break

      // ── Route to OpenClaw ─────────────────────────────────────────────────
      case 'TO_OPENCLAW':
        if (!requireAuthed(client, ws)) break
        console.log(`[route] ${client.role} → OpenClaw  ${JSON.stringify(payload).slice(0, 80)}`)
        sendTo('openclaw', { type: 'TASK', from: client.role, payload })
        break

      // ── Route to Hermes (Ollama bridge) ───────────────────────────────────
      case 'TO_HERMES':
        if (!requireAuthed(client, ws)) break
        console.log(`[route] ${client.role} → Hermes  ${JSON.stringify(payload).slice(0, 80)}`)
        sendTo('hermes', { type: 'PROMPT', from: client.role, payload })
        break

      // ── Hermes response — broadcast to UI clients only ───────────────────
      case 'HERMES_RESPONSE':
        if (!requireAuthed(client, ws) || client.role !== 'hermes') break
        console.log(`[hermes] response  ${String(payload).slice(0, 80)}`)
        sendTo('ui', { type: 'HERMES_RESPONSE', payload })
        break

      // ── OpenClaw task result — broadcast to UI clients only ──────────────
      case 'TASK_RESULT':
        if (!requireAuthed(client, ws) || client.role !== 'openclaw') break
        sendTo('ui', { type: 'TASK_RESULT', payload })
        break

      // ── Agent heartbeat ───────────────────────────────────────────────────
      case 'HEARTBEAT': {
        if (!requireAuthed(client, ws)) break
        const a = client.role
        if (a && agentStatus[a] !== undefined) {
          agentStatus[a] = 'running'
          broadcast({ type: 'AGENT_STATUS', agent: a, status: 'running' }, id)
        }
        ws.send(JSON.stringify({ type: 'HEARTBEAT_ACK', ts: Date.now() }))
        break
      }

      // ── Anything else: only authenticated clients may broadcast ──────────
      default:
        if (!requireAuthed(client, ws)) break
        broadcast({ type, agent, payload, from: id }, id)
    }
  })

  ws.on('close', () => {
    const c = clients.get(id)
    if (c && (c.role === 'openclaw' || c.role === 'hermes')) {
      agentStatus[c.role] = 'offline'
      broadcast({ type: 'AGENT_STATUS', agent: c.role, status: 'offline' })
    }
    clients.delete(id)
    console.log(`[-] Client ${id} disconnected  (${clients.size} total)`)
  })

  ws.on('error', err => console.error(`[ws] ${id} error:`, err.message))
})

// ── Heartbeat watchdog: mark agents offline after 20 s silence ────────────
setInterval(() => {
  const now = Date.now()
  for (const [, c] of clients) {
    if (c.role !== 'ui' && c.role !== 'unknown') {
      if (now - c.lastSeen > 20_000 && agentStatus[c.role] === 'running') {
        agentStatus[c.role] = 'missed_heartbeat'
        broadcast({ type: 'AGENT_STATUS', agent: c.role, status: 'missed_heartbeat' })
      }
    }
  }
}, 10_000)

function listenOn(server, port, host) {
  return new Promise((resolve, reject) => {
    const onError = (err) => reject(err)
    server.once('error', onError)
    server.listen(port, host, () => {
      server.off('error', onError)
      resolve(server.address())
    })
  })
}

// Extra public HTTP sockets (ORIGIN_PORT=80,443) so Cloudflare can handshake
// :80/:443 while the agent bus stays on 8765. These answer /health only.
async function listenOriginPorts() {
  for (const originPort of BIND.originPorts) {
    const extra = http.createServer(handleHttp)
    try {
      await listenOn(extra, originPort, HOST)
      console.log(`[origin] extra listener ${HOST}:${originPort}`)
    } catch (err) {
      // Privileged ports (:80/:443) fail without root/cap_net_bind_service.
      // Keep the bus on PORT alive so Caddy/nginx can still reverse-proxy.
      console.error(`[origin] failed to bind ${HOST}:${originPort}: ${err.message} (gateway still listening on ${HOST}:${PORT})`)
    }
  }
}

// ── Start ─────────────────────────────────────────────────────────────────
listenOn(httpServer, PORT, HOST)
  .then(async (addr) => {
    await listenOriginPorts()
    const shown = addr && typeof addr === 'object' ? `${addr.address}:${addr.port}` : `${HOST}:${PORT}`
    console.log(`
  ████████████████████████████████████████
  🦀  CRABDECK GATEWAY  v2.2
      Bind       : ${shown}
      Listeners  : ${BIND.listeners.join(', ')}
      WebSocket  : ws://${HOST}:${PORT}
      HTTP/health: http://${HOST}:${PORT}/health
      Auth: ${GATEWAY_TOKEN ? 'ENABLED (token required)' : 'DISABLED (dev mode — set GATEWAY_TOKEN for prod)'}
  ████████████████████████████████████████
  `)
  })
  .catch((err) => {
    console.error(`[origin] gateway failed to bind ${HOST}:${PORT}: ${err.message}`)
    process.exit(1)
  })
