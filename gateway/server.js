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
const express = require('express')
const crypto = require('crypto')
const { randomUUID } = crypto
const bhive = require('./bhive')
const { ingestHeartbeat } = require('./vault_client')

const PORT          = process.env.PORT || 8765
const GATEWAY_TOKEN  = process.env.GATEWAY_TOKEN || null
const ALLOWED_ORIGINS = (process.env.ALLOWED_ORIGINS || 'http://localhost:5173,http://127.0.0.1:5173')
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
    authRequired: Boolean(GATEWAY_TOKEN),
    clients:      clients.size,
    agentStatus,
    bhive_slot:   bhive.minuteSlot(),
    uptime:       process.uptime(),
    vault:        process.env.VAULT_URL || 'http://localhost:7070',
  }
}

const app = express()
app.disable('x-powered-by')
app.use(express.json({ limit: '32kb' }))
app.get('/health', (_req, res) => { res.json(healthPayload()) })
app.get('/metrics', (_req, res) => {
  const now = Date.now()
  const list = []
  for (const [id, c] of clients) {
    list.push({
      id,
      role: c.role,
      lastSeen: c.lastSeen,
      watchdog_miss: bhive.missedWatchdog(c.lastSeen, now),
    })
  }
  res.json({ agentStatus, clients: list, slot: bhive.minuteSlot(now) })
})

const httpServer = http.createServer(app)

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
        const ts = typeof msg.ts === 'number' ? msg.ts : Date.now() / 1000
        const slot = typeof msg.bhive_slot === 'number' ? msg.bhive_slot : bhive.minuteSlot()
        ws.send(JSON.stringify({ type: 'HEARTBEAT_ACK', ts: Date.now(), bhive_slot: slot }))
        if (a && a !== 'unknown' && a !== 'ui') {
          void ingestHeartbeat({ agent: a, ts, slot, source: 'gateway' })
        }
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
      if (bhive.missedWatchdog(c.lastSeen, now) && agentStatus[c.role] === 'running') {
        agentStatus[c.role] = 'missed_heartbeat'
        broadcast({ type: 'AGENT_STATUS', agent: c.role, status: 'missed_heartbeat' })
      }
    }
  }
}, 10_000)

// ── Start ─────────────────────────────────────────────────────────────────
httpServer.listen(PORT, () => {
  console.log(`
  ████████████████████████████████████████
  🦀  CRABDECK GATEWAY  v2.2
      WebSocket : ws://localhost:${PORT}
      HTTP/health: http://localhost:${PORT}/health
      HTTP/metrics: http://localhost:${PORT}/metrics
      Auth: ${GATEWAY_TOKEN ? 'ENABLED (token required)' : 'DISABLED (dev mode — set GATEWAY_TOKEN for prod)'}
  ████████████████████████████████████████
  `)
})
