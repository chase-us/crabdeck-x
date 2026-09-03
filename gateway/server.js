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
 *
 * Swarm mesh (see artifacts/SWARM_MESH_PROTOCOL.md):
 *   - SWARM_TASK fans a goal out to every connected agent role as SWARM_ROUND,
 *     seeded with RAG context retrieved from Shell Cracked.
 *   - SWARM_CONTRIBUTION is echoed to the other peers (SWARM_PEER) and to the UI;
 *     when every peer has spoken (or the round times out) the next round opens
 *     with the previous round's contributions attached, so peers build on and
 *     critique each other. The final round is synthesized by Hermes and the
 *     result is written back into the vault as new memory.
 *   - MESH is a direct peer-to-peer frame between agent roles.
 */

const { WebSocketServer, WebSocket } = require('ws')
const http  = require('http')
const express = require('express')
const crypto = require('crypto')
const { randomUUID } = crypto
const bhive = require('./bhive')
const swarm = require('./swarm')
const { ingestHeartbeat, queryMemory, ingestMemory, upsertSession } = require('./vault_client')

const PORT          = process.env.PORT || 8765
const GATEWAY_TOKEN  = process.env.GATEWAY_TOKEN || null
const ALLOWED_ORIGINS = (process.env.ALLOWED_ORIGINS || 'http://localhost:5173,http://127.0.0.1:5173')
  .split(',').map(s => s.trim()).filter(Boolean)
const SWARM_RAG_HITS = Number.parseInt(process.env.SWARM_RAG_HITS || '5', 10) || 5
const SWARM_RAG_MIN_SCORE = Number.parseFloat(process.env.SWARM_RAG_MIN_SCORE || '0') || 0
const SWARM_ROUND_TIMEOUT_MS = Number.parseInt(process.env.SWARM_ROUND_TIMEOUT_MS || '', 10) || swarm.ROUND_TIMEOUT_MS

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

// ── Swarm mesh sessions ────────────────────────────────────────────────────
const swarms = new Map()        // session id → session (see swarm.js)
const swarmTimers = new Map()   // session id → round timeout handle

function swarmSummary() {
  let active = 0
  for (const s of swarms.values()) if (s.status === 'running' || s.status === 'synthesizing') active += 1
  return { active, total: swarms.size, peers: swarm.meshRoster(clients) }
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
    swarm:        swarmSummary(),
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
app.get('/swarm', (_req, res) => {
  const sessions = [...swarms.values()]
    .sort((a, b) => b.createdAt - a.createdAt)
    .map(swarm.summary)
  res.json({ ...swarmSummary(), sessions })
})
app.get('/swarm/:id', (req, res) => {
  const s = swarms.get(String(req.params.id))
  if (!s) return res.status(404).json({ error: 'swarm session not found' })
  res.json(swarm.resultPayload(s))
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

function sendError(ws, code, message) {
  ws.send(JSON.stringify({ type: 'ERROR', code, message }))
}

// ── Mesh roster ────────────────────────────────────────────────────────────
function broadcastPeers() {
  const peers = swarm.meshRoster(clients)
  const frame = { type: 'MESH_PEERS', payload: { peers, bhive_slot: bhive.minuteSlot() } }
  for (const role of swarm.SWARM_ROLES) sendTo(role, frame)
  sendTo('ui', frame)
}

// ── Swarm orchestration ────────────────────────────────────────────────────
function clearSwarmTimer(sessionId) {
  const t = swarmTimers.get(sessionId)
  if (t) clearTimeout(t)
  swarmTimers.delete(sessionId)
}

function armSwarmTimer(session) {
  clearSwarmTimer(session.id)
  const round = session.round
  swarmTimers.set(session.id, setTimeout(() => {
    const live = swarms.get(session.id)
    if (!live || live.status !== 'running' || live.round !== round) return
    const cur = swarm.currentRound(live)
    const silent = live.participants.filter((p) => !Object.prototype.hasOwnProperty.call(cur.contributions, p))
    console.log(`[swarm] ${live.id.slice(0, 8)} round ${round} timed out — silent: ${silent.join(', ') || 'none'}`)
    advanceSwarm(live)
  }, SWARM_ROUND_TIMEOUT_MS))
}

function openSwarmRound(session) {
  const payload = swarm.roundPayload(session)
  for (const role of session.participants) sendTo(role, { type: 'SWARM_ROUND', payload })
  sendTo('ui', { type: 'SWARM_ROUND', payload })
  armSwarmTimer(session)
}

function persistSwarm(session) {
  const result = swarm.resultPayload(session)
  void upsertSession(`swarm:${session.id}`, result)
  if (session.status === 'done' && session.result) {
    void ingestMemory({
      agent: 'crabdeck',
      kind: 'swarm_result',
      text: `${session.goal.slice(0, 1200)}\n---\n${session.result.slice(0, 6000)}`,
      metadata: {
        session_id: session.id,
        peers: session.participants.join(','),
        rounds: session.rounds.length,
        synthesized_by: session.synthesizedBy,
      },
    })
  }
}

function finishSwarm(session, text, synthesizedBy) {
  clearSwarmTimer(session.id)
  if (!swarm.finalize(session, text, synthesizedBy)) return
  const payload = swarm.resultPayload(session)
  sendTo('ui', { type: 'SWARM_RESULT', payload })
  for (const role of session.participants) sendTo(role, { type: 'SWARM_RESULT', payload })
  console.log(`[swarm] ${session.id.slice(0, 8)} done via ${session.synthesizedBy} (${payload.transcript.length} contributions)`)
  persistSwarm(session)
  swarm.prune(swarms)
}

function failSwarm(session, reason) {
  clearSwarmTimer(session.id)
  if (!swarm.fail(session, reason)) return
  const payload = swarm.resultPayload(session)
  sendTo('ui', { type: 'SWARM_RESULT', payload })
  console.warn(`[swarm] ${session.id.slice(0, 8)} failed: ${session.error}`)
  persistSwarm(session)
  swarm.prune(swarms)
}

function requestSynthesis(session) {
  clearSwarmTimer(session.id)
  const roster = swarm.meshRoster(clients)
  if (!roster.includes('hermes')) {
    finishSwarm(session, '', 'gateway')
    return
  }
  const payload = swarm.synthesizePayload(session)
  sendTo('hermes', { type: 'SWARM_SYNTHESIZE', payload })
  sendTo('ui', { type: 'SWARM_SYNTHESIZING', payload: { session_id: session.id, synthesizer: 'hermes' } })
  swarmTimers.set(session.id, setTimeout(() => {
    const live = swarms.get(session.id)
    if (live && live.status === 'synthesizing') {
      console.warn(`[swarm] ${live.id.slice(0, 8)} synthesizer timed out — falling back to transcript digest`)
      finishSwarm(live, '', 'gateway')
    }
  }, SWARM_ROUND_TIMEOUT_MS))
}

function advanceSwarm(session) {
  const next = swarm.advance(session)
  if (next === 'round') {
    console.log(`[swarm] ${session.id.slice(0, 8)} → round ${session.round}/${session.maxRounds}`)
    openSwarmRound(session)
  } else if (next === 'synthesize') {
    requestSynthesis(session)
  } else if (next === 'failed') {
    failSwarm(session, session.error)
  }
}

async function startSwarm(client, ws, payload) {
  const task = swarm.normalizeTask(payload)
  if (!task) {
    sendError(ws, 'BAD_SWARM_TASK', 'SWARM_TASK requires a non-empty goal.')
    return
  }
  const participants = swarm.meshRoster(clients)
  if (participants.length === 0) {
    sendError(ws, 'NO_SWARM_PEERS', 'No agent peers are connected to the mesh.')
    return
  }
  // RAG: seed the swarm with what the vault already knows about this goal. Fail-open.
  const hits = await queryMemory(task.goal, SWARM_RAG_HITS)
  const session = swarm.createSession({
    goal: task.goal,
    rounds: task.rounds,
    model: task.model,
    participants,
    context: hits,
    minScore: SWARM_RAG_MIN_SCORE,
    from: client.role,
  })
  swarms.set(session.id, session)
  swarm.prune(swarms)
  console.log(`[swarm] ${session.id.slice(0, 8)} started by ${client.role}: "${task.goal.slice(0, 60)}" peers=${participants.join(',')} rag=${session.context.length}`)
  sendTo('ui', {
    type: 'SWARM_STARTED',
    payload: { ...swarm.summary(session), context: session.context, model: session.model },
  })
  openSwarmRound(session)
}

function onSwarmContribution(client, ws, payload) {
  const c = swarm.normalizeContribution(payload)
  if (!c) {
    sendError(ws, 'BAD_SWARM_CONTRIBUTION', 'SWARM_CONTRIBUTION requires session_id and non-empty text.')
    return
  }
  const session = swarms.get(c.session_id)
  if (!session) {
    sendError(ws, 'UNKNOWN_SWARM', `No swarm session ${c.session_id}.`)
    return
  }
  const res = swarm.recordContribution(session, client.role, c.round, c.text)
  if (!res.accepted) {
    console.log(`[swarm] ${session.id.slice(0, 8)} ignored ${client.role} contribution: ${res.reason}`)
    return
  }
  const frame = { session_id: session.id, round: session.round, from: client.role, text: c.text }
  sendTo('ui', { type: 'SWARM_CONTRIBUTION', payload: frame })
  for (const role of session.participants) {
    if (role !== client.role) sendTo(role, { type: 'SWARM_PEER', payload: frame })
  }
  console.log(`[swarm] ${session.id.slice(0, 8)} r${session.round} ${client.role}: ${c.text.slice(0, 60)}`)
  if (res.roundComplete) advanceSwarm(session)
}

function onSwarmSynthesis(client, ws, payload) {
  const c = swarm.normalizeContribution(payload)
  if (!c) {
    sendError(ws, 'BAD_SWARM_SYNTHESIS', 'SWARM_SYNTHESIS requires session_id and non-empty text.')
    return
  }
  const session = swarms.get(c.session_id)
  if (!session || session.status !== 'synthesizing') return
  finishSwarm(session, c.text, client.role)
}

function onMesh(client, ws, msg) {
  const m = swarm.normalizeMesh(msg)
  if (!m) {
    sendError(ws, 'BAD_MESH', 'MESH requires `to` (a swarm role) and non-empty payload.text.')
    return
  }
  const frame = { type: 'MESH', from: client.role, to: m.to, payload: m.payload }
  sendTo(m.to, frame)
  sendTo('ui', { type: 'MESH_TRACE', payload: { from: client.role, to: m.to, ...m.payload } })
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
        if (swarm.isRole(role) || role === 'ui') broadcastPeers()
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

      // ── Swarm mesh: goal fan-out to every connected peer ─────────────────
      case 'SWARM_TASK':
        if (!requireAuthed(client, ws)) break
        void startSwarm(client, ws, payload)
        break

      // ── Swarm mesh: a peer's round contribution ──────────────────────────
      case 'SWARM_CONTRIBUTION':
        if (!requireAuthed(client, ws) || !swarm.isRole(client.role)) break
        onSwarmContribution(client, ws, payload)
        break

      // ── Swarm mesh: synthesizer (Hermes) closes the session ──────────────
      case 'SWARM_SYNTHESIS':
        if (!requireAuthed(client, ws) || client.role !== 'hermes') break
        onSwarmSynthesis(client, ws, payload)
        break

      // ── Peer-to-peer mesh frame between agent roles ──────────────────────
      case 'MESH':
        if (!requireAuthed(client, ws) || !swarm.isRole(client.role)) break
        onMesh(client, ws, msg)
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
    if (c && swarm.isRole(c.role)) {
      broadcastPeers()
      // A departed peer should not stall a round: close any round it was the last holdout on.
      const roster = swarm.meshRoster(clients)
      for (const s of swarms.values()) {
        if (s.status !== 'running') continue
        const cur = swarm.currentRound(s)
        const waitingOn = s.participants.filter((p) => !Object.prototype.hasOwnProperty.call(cur.contributions, p))
        if (waitingOn.length > 0 && waitingOn.every((p) => !roster.includes(p))) advanceSwarm(s)
      }
    }
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
  🦀  CRABDECK GATEWAY  v2.3 — swarm mesh
      WebSocket : ws://localhost:${PORT}
      HTTP/health: http://localhost:${PORT}/health
      HTTP/metrics: http://localhost:${PORT}/metrics
      HTTP/swarm : http://localhost:${PORT}/swarm
      Swarm: rounds time out after ${SWARM_ROUND_TIMEOUT_MS}ms · RAG top-${SWARM_RAG_HITS} (min score ${SWARM_RAG_MIN_SCORE})
      Auth: ${GATEWAY_TOKEN ? 'ENABLED (token required)' : 'DISABLED (dev mode — set GATEWAY_TOKEN for prod)'}
  ████████████████████████████████████████
  `)
})
