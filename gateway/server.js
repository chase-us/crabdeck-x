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

// ── Swarm Mesh Registry & Topology ──────────────────────────────────────────
// Keeps track of active swarm nodes, their capabilities, peers, and collaboration stats
const swarmNodes = new Map() // agentRole -> { id, role, capabilities: [], status, connectedAt, lastSeen }
const swarmTasks = new Map() // taskId -> { id, initiator, goal, assignedTo, status, createdAt, results: {} }

function getMeshTopology() {
  const nodes = []
  for (const [role, node] of swarmNodes) {
    nodes.push({
      agent: role,
      status: agentStatus[role] || 'offline',
      capabilities: node.capabilities || [],
      connectedAt: node.connectedAt,
      lastSeen: node.lastSeen,
    })
  }
  return {
    mesh_size: nodes.length,
    active_nodes: nodes.filter(n => n.status === 'running').length,
    nodes,
    active_tasks: Array.from(swarmTasks.values()).slice(-10),
  }
}

function healthPayload() {
  return {
    status:       'ok',
    authRequired: Boolean(GATEWAY_TOKEN),
    clients:      clients.size,
    agentStatus,
    swarmMesh:    getMeshTopology(),
    bhive_slot:   bhive.minuteSlot(),
    uptime:       process.uptime(),
    vault:        process.env.VAULT_URL || 'http://localhost:7070',
  }
}

const app = express()
app.disable('x-powered-by')
app.use(express.json({ limit: '32kb' }))
app.get('/health', (_req, res) => { res.json(healthPayload()) })
app.get('/mesh', (_req, res) => { res.json(getMeshTopology()) })
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
  res.json({ agentStatus, clients: list, slot: bhive.minuteSlot(now), mesh: getMeshTopology() })
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
          swarmNodes.set('openclaw', {
            id,
            role: 'openclaw',
            capabilities: msg.capabilities || ['system_exec', 'task_reasoning', 'agent_collab'],
            status: 'running',
            connectedAt: Date.now(),
            lastSeen: Date.now(),
          })
          broadcast({ type: 'AGENT_STATUS', agent: 'openclaw', status: 'running' })
          broadcast({ type: 'SWARM_PEER_JOIN', peer: 'openclaw', topology: getMeshTopology() })
          console.log('[openclaw] registered in swarm mesh')
        }
        if (role === 'hermes') {
          agentStatus.hermes = 'running'
          swarmNodes.set('hermes', {
            id,
            role: 'hermes',
            capabilities: msg.capabilities || ['llm_synthesis', 'rag_retrieval', 'tool_routing'],
            status: 'running',
            connectedAt: Date.now(),
            lastSeen: Date.now(),
          })
          broadcast({ type: 'AGENT_STATUS', agent: 'hermes', status: 'running' })
          broadcast({ type: 'SWARM_PEER_JOIN', peer: 'hermes', topology: getMeshTopology() })
          console.log('[hermes] registered in swarm mesh')
        }
        if (role === 'orchestrator') {
          swarmNodes.set('orchestrator', {
            id,
            role: 'orchestrator',
            capabilities: ['topology_monitoring', 'task_coordination', 'health_check'],
            status: 'running',
            connectedAt: Date.now(),
            lastSeen: Date.now(),
          })
          broadcast({ type: 'SWARM_PEER_JOIN', peer: 'orchestrator', topology: getMeshTopology() })
        }
        ws.send(JSON.stringify({ type: 'ACK', clientId: id, role, mesh: getMeshTopology() }))
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
        if (a && swarmNodes.has(a)) {
          const node = swarmNodes.get(a)
          node.lastSeen = Date.now()
          node.status = 'running'
        }
        const ts = typeof msg.ts === 'number' ? msg.ts : Date.now() / 1000
        const slot = typeof msg.bhive_slot === 'number' ? msg.bhive_slot : bhive.minuteSlot()
        ws.send(JSON.stringify({ type: 'HEARTBEAT_ACK', ts: Date.now(), bhive_slot: slot }))
        if (a && a !== 'unknown' && a !== 'ui') {
          void ingestHeartbeat({ agent: a, ts, slot, source: 'gateway' })
        }
        break
      }

      // ── Swarm Mesh: Point-to-Point Agent Message ──────────────────────────
      case 'SWARM_MESSAGE': {
        if (!requireAuthed(client, ws)) break
        const target = msg.target // e.g. 'hermes', 'openclaw', 'orchestrator'
        const fromAgent = msg.from || client.role
        console.log(`[swarm] P2P: ${fromAgent} → ${target} (action: ${msg.action})`)
        sendTo(target, {
          type: 'SWARM_MESSAGE',
          from: fromAgent,
          target,
          action: msg.action,
          payload: msg.payload,
          corrId: msg.corrId || randomUUID(),
          ts: Date.now(),
        })
        // Also inform UI of swarm interaction event
        sendTo('ui', {
          type: 'SWARM_EVENT',
          kind: 'P2P_MESSAGE',
          from: fromAgent,
          target,
          action: msg.action,
          payload: msg.payload,
          ts: Date.now(),
        })
        break
      }

      // ── Swarm Mesh: Swarm Broadcast ───────────────────────────────────────
      case 'SWARM_BROADCAST': {
        if (!requireAuthed(client, ws)) break
        const fromAgent = msg.from || client.role
        console.log(`[swarm] Broadcast from ${fromAgent}: ${msg.topic}`)
        broadcast({
          type: 'SWARM_BROADCAST',
          from: fromAgent,
          topic: msg.topic,
          payload: msg.payload,
          corrId: msg.corrId || randomUUID(),
          ts: Date.now(),
        })
        break
      }

      // ── Swarm Mesh: Coordinated Multi-Agent Task ──────────────────────────
      case 'SWARM_COORDINATE': {
        if (!requireAuthed(client, ws)) break
        const taskId = msg.taskId || randomUUID()
        const goal = msg.goal || msg.payload?.goal || 'Collaborative task'
        const from = client.role
        console.log(`[swarm] New coordinated swarm task ${taskId}: "${goal}"`)
        const taskRecord = {
          id: taskId,
          initiator: from,
          goal,
          status: 'in_progress',
          createdAt: Date.now(),
          results: {},
        }
        swarmTasks.set(taskId, taskRecord)

        // Broadcast task dispatch to all agent nodes in the mesh
        broadcast({
          type: 'SWARM_TASK_DISPATCH',
          taskId,
          goal,
          initiator: from,
          assignedAgents: ['hermes', 'openclaw'],
          payload: msg.payload,
          ts: Date.now(),
        })
        break
      }

      // ── Swarm Mesh: Agent contribution to coordinated task ────────────────
      case 'SWARM_TASK_CONTRIBUTION': {
        if (!requireAuthed(client, ws)) break
        const { taskId, contribution } = msg
        const agent = msg.agent || client.role
        if (taskId && swarmTasks.has(taskId)) {
          const t = swarmTasks.get(taskId)
          t.results[agent] = contribution
          console.log(`[swarm] Task ${taskId} contribution received from ${agent}`)

          // Check if both hermes & openclaw contributed (or all required)
          const hasHermes = Boolean(t.results.hermes)
          const hasClaw = Boolean(t.results.openclaw)
          if (hasHermes && hasClaw) {
            t.status = 'completed'
            t.completedAt = Date.now()
          }

          broadcast({
            type: 'SWARM_TASK_UPDATE',
            taskId,
            task: t,
            agent,
            contribution,
          })
        }
        break
      }

      // ── Swarm Mesh: Request Mesh Topology ─────────────────────────────────
      case 'GET_MESH_TOPOLOGY': {
        if (!requireAuthed(client, ws)) break
        ws.send(JSON.stringify({
          type: 'MESH_TOPOLOGY',
          topology: getMeshTopology(),
        }))
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
    if (c && (c.role === 'openclaw' || c.role === 'hermes' || c.role === 'orchestrator')) {
      agentStatus[c.role] = 'offline'
      if (swarmNodes.has(c.role)) {
        swarmNodes.get(c.role).status = 'offline'
      }
      broadcast({ type: 'AGENT_STATUS', agent: c.role, status: 'offline' })
      broadcast({ type: 'SWARM_PEER_LEAVE', peer: c.role, topology: getMeshTopology() })
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
