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
const mesh = require('./mesh')
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
// role: 'ui' | 'openclaw' | 'hermes' | 'scribe' | 'orchestrator' | 'unknown'
const clients = new Map()   // id → { ws, role, authed, connectedAt, lastSeen }

// ── Swarm mesh ─────────────────────────────────────────────────────────────
// Agent roles are mesh peers; 'ui' observes and announces but never bids.
const AGENT_ROLES = new Set(['openclaw', 'hermes', 'scribe', 'orchestrator'])
const swarm = new mesh.MeshRegistry()
// Contract Net: peers get this long to bid before the gateway awards.
const BID_WINDOW_MS = Number(process.env.MESH_BID_WINDOW_MS || 1500)
const bidTimers = new Map()  // taskId → timeout

// ── Agent status store ─────────────────────────────────────────────────────
const agentStatus = {
  openclaw: 'offline',
  hermes:   'offline',
  scribe:   'offline',
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
    mesh:         { peers: swarm.peerNames(), contracts: swarm.contracts().length },
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
app.get('/mesh', (_req, res) => {
  res.json({ status: 'ok', slot: bhive.minuteSlot(), bidWindowMs: BID_WINDOW_MS, ...swarm.snapshot() })
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

// ── Mesh plumbing ──────────────────────────────────────────────────────────

/** Send to one mesh peer by name. Returns false when that peer is not connected. */
function sendToPeer(name, msg) {
  const peer = swarm.peer(name)
  if (!peer) return false
  const raw = JSON.stringify(msg)
  let delivered = false
  for (const [id, c] of clients) {
    if (id === peer.clientId && c.ws.readyState === WebSocket.OPEN) {
      c.ws.send(raw)
      delivered = true
    }
  }
  return delivered
}

function meshError(ws, code, message) {
  ws.send(JSON.stringify({ type: 'MESH_ERROR', code, message }))
}

/** Mesh membership requires an authenticated, non-UI agent role. */
function requireMeshPeer(client, ws) {
  if (!requireAuthed(client, ws)) return null
  const peer = swarm.peer(client.role)
  if (!peer || peer.clientId !== client.id) {
    meshError(ws, 'NOT_A_PEER', 'Only authenticated agent roles participate in the mesh.')
    return null
  }
  return peer
}

/**
 * Close bidding and award the contract.
 *
 * The winner is told to execute; everyone else is told the call is closed so
 * they can drop the reservation instead of holding capacity for a task they
 * did not win.
 */
function closeBidding(taskId) {
  const timer = bidTimers.get(taskId)
  if (timer) { clearTimeout(timer); bidTimers.delete(taskId) }

  const contract = swarm.contract(taskId)
  if (!contract || contract.state !== 'open') return

  const bidders = contract.bids.map(b => b.peer)
  const winner = swarm.award(taskId)

  if (!winner) {
    notifyAnnouncer(contract, {
      type: 'MESH_UNAWARDED',
      payload: { taskId, reason: contract.bids.length ? 'no eligible bid' : 'no bids', invited: contract.invited },
    })
    console.log(`[mesh] ${taskId} unawarded (${contract.invited.length} invited, 0 usable bids)`)
    return
  }

  sendToPeer(winner.peer, {
    type: 'MESH_AWARD',
    payload: {
      taskId,
      task: contract.task,
      capabilities: contract.capabilities,
      announcer: contract.announcer,
      score: Number(winner.score.toFixed(4)),
    },
  })
  for (const name of bidders) {
    if (name === winner.peer) continue
    sendToPeer(name, { type: 'MESH_CFP_CLOSED', payload: { taskId, winner: winner.peer } })
  }
  notifyAnnouncer(contract, {
    type: 'MESH_AWARDED',
    payload: {
      taskId,
      winner: winner.peer,
      score: Number(winner.score.toFixed(4)),
      bids: contract.bids.map(b => ({ peer: b.peer, score: Number(b.score.toFixed(4)) })),
    },
  })
  console.log(`[mesh] ${taskId} → ${winner.peer} (score ${winner.score.toFixed(3)} of ${contract.bids.length} bids)`)
}

/** Route a contract update back to whoever announced it, plus the operator UI. */
function notifyAnnouncer(contract, msg) {
  if (contract.announcer && contract.announcer !== 'ui') sendToPeer(contract.announcer, msg)
  sendTo('ui', msg)
}

function handleMeshMessage(client, ws, msg) {
  const { type } = msg
  const payload = msg.payload && typeof msg.payload === 'object' && !Array.isArray(msg.payload)
    ? msg.payload
    : {}

  switch (type) {
    // ── Peer discovery ──
    case 'MESH_PEERS': {
      if (!requireAuthed(client, ws)) return true
      ws.send(JSON.stringify({ type: 'MESH_STATE', payload: swarm.snapshot() }))
      return true
    }

    // ── Contract Net: call for proposals ──
    case 'MESH_ANNOUNCE': {
      if (!requireAuthed(client, ws)) return true
      const task = mesh.clampText(payload.task, mesh.MAX_TASK_LEN).trim()
      if (!task) { meshError(ws, 'BAD_TASK', 'payload.task must be a non-empty string'); return true }

      const capabilities = mesh.normalizeCapabilities(payload.capabilities)
      const taskId = mesh.clampText(payload.taskId, 64).trim() || `task-${randomUUID().slice(0, 8)}`
      if (swarm.contract(taskId)) { meshError(ws, 'DUPLICATE_TASK', `taskId ${taskId} is already open`); return true }

      const candidates = swarm.capablePeers(capabilities, { exclude: client.role })
      const contract = swarm.openContract(taskId, {
        task,
        capabilities,
        announcer: client.role,
        invited: candidates.map(p => p.name),
        quorumTarget: payload.quorum,
      })
      if (!contract) { meshError(ws, 'BAD_TASK_ID', 'taskId was rejected'); return true }

      for (const peer of candidates) {
        sendToPeer(peer.name, {
          type: 'MESH_CFP',
          payload: {
            taskId,
            task,
            capabilities,
            announcer: contract.announcer,
            deadlineMs: BID_WINDOW_MS,
          },
        })
      }
      ws.send(JSON.stringify({
        type: 'MESH_ANNOUNCED',
        payload: { taskId, invited: contract.invited, capabilities, quorum: contract.quorumTarget },
      }))
      sendTo('ui', {
        type: 'MESH_ANNOUNCED',
        payload: { taskId, task, announcer: contract.announcer, invited: contract.invited, capabilities },
      })
      console.log(`[mesh] CFP ${taskId} from ${contract.announcer} → ${contract.invited.join(', ') || '(nobody)'}`)

      if (contract.invited.length === 0) {
        closeBidding(taskId)
      } else {
        bidTimers.set(taskId, setTimeout(() => closeBidding(taskId), BID_WINDOW_MS))
      }
      return true
    }

    // ── Contract Net: bid ──
    case 'MESH_BID': {
      const peer = requireMeshPeer(client, ws)
      if (!peer) return true
      const taskId = mesh.clampText(payload.taskId, 64).trim()
      const contract = swarm.contract(taskId)
      if (!contract) { meshError(ws, 'NO_CONTRACT', `unknown taskId ${taskId}`); return true }
      const bid = swarm.addBid(taskId, peer.name, payload)
      if (!bid) { meshError(ws, 'BID_REJECTED', 'contract closed, bid duplicated, or bid cap reached'); return true }
      ws.send(JSON.stringify({ type: 'MESH_BID_ACK', payload: { taskId, score: Number(bid.score.toFixed(4)) } }))
      // Everyone invited has answered — no reason to wait out the window.
      if (contract.bids.length >= contract.invited.length) closeBidding(taskId)
      return true
    }

    // ── Contract Net: result + consensus ──
    case 'MESH_RESULT': {
      const peer = requireMeshPeer(client, ws)
      if (!peer) return true
      const taskId = mesh.clampText(payload.taskId, 64).trim()
      const contract = swarm.contract(taskId)
      if (!contract) { meshError(ws, 'NO_CONTRACT', `unknown taskId ${taskId}`); return true }

      const entry = swarm.recordResult(taskId, peer.name, {
        result: payload.result,
        confidence: payload.confidence,
        ok: payload.ok,
      })
      if (!entry) { meshError(ws, 'RESULT_REJECTED', 'result cap reached for this contract'); return true }

      const body = {
        taskId,
        peer: peer.name,
        result: entry.result,
        confidence: entry.confidence,
        ok: entry.ok,
        citations: Array.isArray(payload.citations) ? payload.citations.slice(0, 12) : [],
        trail: Number(swarm.trail(peer.name).toFixed(3)),
      }
      notifyAnnouncer(contract, { type: 'MESH_RESULT', payload: body })

      if (contract.state === 'complete') {
        const agreed = swarm.consensus(taskId)
        notifyAnnouncer(contract, {
          type: 'MESH_CONSENSUS',
          payload: { taskId, task: contract.task, ...agreed },
        })
        console.log(`[mesh] ${taskId} consensus ${agreed.votes}/${agreed.total} (${agreed.peers.join(', ')})`)
      }
      return true
    }

    // ── Peer-to-peer ──
    case 'MESH_DIRECT': {
      const peer = requireMeshPeer(client, ws)
      if (!peer) return true
      const to = mesh.clampText(payload.to, 32).trim().toLowerCase()
      if (!to) { meshError(ws, 'BAD_TARGET', 'payload.to must name a peer'); return true }
      const delivered = sendToPeer(to, {
        type: 'MESH_MESSAGE',
        payload: {
          from: peer.name,
          intent: mesh.clampText(payload.intent, 32) || 'message',
          replyTo: mesh.clampText(payload.replyTo, 64),
          body: payload.body,
        },
      })
      ws.send(JSON.stringify({ type: 'MESH_DIRECT_ACK', payload: { to, delivered } }))
      return true
    }

    // ── Gossip ──
    case 'MESH_GOSSIP': {
      const peer = requireMeshPeer(client, ws)
      if (!peer) return true
      const gossipId = mesh.clampText(payload.id, 64).trim() || `g-${randomUUID().slice(0, 12)}`
      if (!swarm.observeGossip(gossipId)) {
        // Already flooded — dropping here is what stops the storm.
        ws.send(JSON.stringify({ type: 'MESH_GOSSIP_ACK', payload: { id: gossipId, forwarded: 0, duplicate: true } }))
        return true
      }
      const ttl = mesh.clampTtl(payload.ttl)
      if (ttl <= 0) {
        ws.send(JSON.stringify({ type: 'MESH_GOSSIP_ACK', payload: { id: gossipId, forwarded: 0, expired: true } }))
        return true
      }
      const targets = mesh.gossipTargets(swarm.peerNames(), peer.name, payload.fanout)
      const forward = {
        type: 'MESH_GOSSIP',
        payload: {
          id: gossipId,
          ttl: ttl - 1,
          origin: mesh.clampText(payload.origin, 32) || peer.name,
          hop: peer.name,
          topic: mesh.clampText(payload.topic, 32) || 'notice',
          body: payload.body,
        },
      }
      let forwarded = 0
      for (const name of targets) if (sendToPeer(name, forward)) forwarded += 1
      sendTo('ui', forward)
      ws.send(JSON.stringify({ type: 'MESH_GOSSIP_ACK', payload: { id: gossipId, forwarded } }))
      return true
    }

    default:
      return false
  }
}

wss.on('connection', (ws) => {
  const id = randomUUID()
  clients.set(id, { id, ws, role: 'unknown', authed: !GATEWAY_TOKEN, connectedAt: Date.now(), lastSeen: Date.now() })
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
                   : msg.client === 'scribe'         ? 'scribe'
                   : msg.client === 'orchestrator'   ? 'orchestrator'
                   : 'unknown'
        client.role = role

        if (agentStatus[role] !== undefined) {
          agentStatus[role] = 'running'
          broadcast({ type: 'AGENT_STATUS', agent: role, status: 'running' })
          console.log(`[${role}] registered`)
        }

        // Mesh membership: authenticated agent roles only. An unauthenticated
        // client stays 'unknown' and never becomes addressable as a peer.
        let meshPeer = null
        if (AGENT_ROLES.has(role)) {
          meshPeer = swarm.join(role, {
            capabilities: msg.capabilities,
            clientId: id,
            version: msg.version,
          })
          if (meshPeer) {
            console.log(`[mesh] ${role} joined  caps=[${meshPeer.capabilities.join(', ')}]  trail=${swarm.trail(role).toFixed(2)}`)
            broadcast({
              type: 'MESH_JOIN',
              payload: { peer: role, capabilities: meshPeer.capabilities, trail: swarm.trail(role) },
            }, id)
          }
        }

        ws.send(JSON.stringify({
          type: 'ACK',
          clientId: id,
          role,
          mesh: meshPeer
            ? { joined: true, capabilities: meshPeer.capabilities, peers: swarm.peerNames() }
            : { joined: false },
        }))
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
        // Heartbeats carry the peer's queue depth, which feeds bid scoring.
        if (AGENT_ROLES.has(a)) swarm.touch(a, msg.load)
        ws.send(JSON.stringify({ type: 'HEARTBEAT_ACK', ts: Date.now(), bhive_slot: slot }))
        if (a && a !== 'unknown' && a !== 'ui') {
          void ingestHeartbeat({ agent: a, ts, slot, source: 'gateway' })
        }
        break
      }

      // ── Anything else: mesh protocol first, then generic broadcast ───────
      default:
        if (typeof type === 'string' && type.startsWith('MESH_')) {
          if (handleMeshMessage(client, ws, msg)) break
        }
        if (!requireAuthed(client, ws)) break
        broadcast({ type, agent, payload, from: id }, id)
    }
  })

  ws.on('close', () => {
    const c = clients.get(id)
    if (c && agentStatus[c.role] !== undefined && c.role !== 'crabdeck') {
      agentStatus[c.role] = 'offline'
      broadcast({ type: 'AGENT_STATUS', agent: c.role, status: 'offline' })
    }
    // Only drop the mesh peer if this socket still owns it — a reconnect that
    // already re-registered the role must not be unregistered by the old close.
    if (c && AGENT_ROLES.has(c.role)) {
      const peer = swarm.peer(c.role)
      if (peer && peer.clientId === id) {
        swarm.leave(c.role)
        broadcast({ type: 'MESH_LEAVE', payload: { peer: c.role } }, id)
        console.log(`[mesh] ${c.role} left`)
      }
    }
    clients.delete(id)
    console.log(`[-] Client ${id} disconnected  (${clients.size} total)`)
  })

  ws.on('error', err => console.error(`[ws] ${id} error:`, err.message))
})

// ── Heartbeat watchdog: mark agents offline after 20 s silence ────────────
// Same tick evaporates pheromone trails and sweeps finished contracts, so
// mesh routing decays toward neutral and neither map grows unbounded.
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
  swarm.tick()
  swarm.sweep()
}, 10_000)

// ── Start ─────────────────────────────────────────────────────────────────
httpServer.listen(PORT, () => {
  console.log(`
  ████████████████████████████████████████
  🦀  CRABDECK GATEWAY  v2.3  (swarm mesh)
      WebSocket : ws://localhost:${PORT}
      HTTP/health: http://localhost:${PORT}/health
      HTTP/metrics: http://localhost:${PORT}/metrics
      HTTP/mesh: http://localhost:${PORT}/mesh
      Auth: ${GATEWAY_TOKEN ? 'ENABLED (token required)' : 'DISABLED (dev mode — set GATEWAY_TOKEN for prod)'}
      Mesh: contract-net bid window ${BID_WINDOW_MS}ms
  ████████████████████████████████████████
  `)
})
