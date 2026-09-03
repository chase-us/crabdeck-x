/**
 * End-to-end mesh protocol over real WebSockets.
 *
 * Unit tests cover the algorithms; this covers the wiring — auth gating, CFP
 * fan-out, bid windows, awards, consensus, gossip dedup, and peer teardown as
 * they actually behave against a live gateway process.
 */

const test = require('node:test')
const assert = require('node:assert/strict')
const { spawn } = require('node:child_process')
const path = require('node:path')
const { WebSocket } = require('ws')

const TOKEN = 'test-mesh-token'
const PORT = 8799
const BASE = `http://127.0.0.1:${PORT}`
const WS_URL = `ws://127.0.0.1:${PORT}`

let server

function waitForHealth(timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs
  return new Promise((resolve, reject) => {
    const attempt = async () => {
      try {
        const res = await fetch(`${BASE}/health`)
        if (res.ok) return resolve()
      } catch { /* not up yet */ }
      if (Date.now() > deadline) return reject(new Error('gateway did not start'))
      setTimeout(attempt, 100)
    }
    attempt()
  })
}

test.before(async () => {
  server = spawn(process.execPath, [path.join(__dirname, 'server.js')], {
    env: {
      ...process.env,
      PORT: String(PORT),
      GATEWAY_TOKEN: TOKEN,
      MESH_BID_WINDOW_MS: '400',
      // Point the vault at a closed port: ingest must stay fail-open.
      VAULT_URL: 'http://127.0.0.1:9',
    },
    stdio: 'ignore',
  })
  await waitForHealth()
})

test.after(() => { if (server) server.kill('SIGKILL') })

/** A test peer that records every frame it receives. */
class Peer {
  constructor(name) {
    this.name = name
    this.frames = []
    this.ws = null
  }

  connect({ token = TOKEN, capabilities = [] } = {}) {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(WS_URL)
      this.ws = ws
      ws.on('message', raw => {
        let msg
        try { msg = JSON.parse(raw) } catch { return }
        this.frames.push(msg)
      })
      ws.on('error', reject)
      ws.on('open', () => {
        const hello = { type: 'HELLO', client: this.name, version: '2.3', capabilities }
        if (token) hello.token = token
        ws.send(JSON.stringify(hello))
      })
      this.waitFor('ACK', 4000).then(resolve).catch(reject)
    })
  }

  send(msg) { this.ws.send(JSON.stringify(msg)) }

  /** Resolve with the first frame of `type` (including ones already buffered). */
  waitFor(type, timeoutMs = 4000, predicate = () => true) {
    const found = this.frames.find(f => f.type === type && predicate(f))
    if (found) return Promise.resolve(found)
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.ws.off('message', onMessage)
        reject(new Error(`${this.name} timed out waiting for ${type}; saw ${this.frames.map(f => f.type).join(',')}`))
      }, timeoutMs)
      const onMessage = raw => {
        let msg
        try { msg = JSON.parse(raw) } catch { return }
        if (msg.type === type && predicate(msg)) {
          clearTimeout(timer)
          this.ws.off('message', onMessage)
          resolve(msg)
        }
      }
      this.ws.on('message', onMessage)
    })
  }

  close() {
    return new Promise(resolve => {
      if (!this.ws || this.ws.readyState === WebSocket.CLOSED) return resolve()
      this.ws.once('close', resolve)
      this.ws.close()
    })
  }
}

test('health and mesh endpoints expose swarm state', async () => {
  const health = await (await fetch(`${BASE}/health`)).json()
  assert.equal(health.status, 'ok')
  assert.equal(health.authRequired, true)
  assert.ok(Array.isArray(health.mesh.peers))

  const meshState = await (await fetch(`${BASE}/mesh`)).json()
  assert.equal(meshState.status, 'ok')
  assert.equal(meshState.bidWindowMs, 400)
  assert.ok(Array.isArray(meshState.peers))
})

test('an unauthenticated client cannot join the mesh or announce', async () => {
  const rogue = new Peer('hermes')
  const ack = await rogue.connect({ token: 'wrong-token' }).catch(err => err)
  // A bad token is rejected outright, so ACK never arrives.
  assert.ok(ack instanceof Error, 'gateway must not ACK a bad token')

  const state = await (await fetch(`${BASE}/mesh`)).json()
  assert.ok(!state.peers.some(p => p.name === 'hermes'), 'rejected client must not appear as a peer')
  await rogue.close()
})

test('authenticated agents join the mesh with their advertised capabilities', async () => {
  const hermes = new Peer('hermes')
  const ack = await hermes.connect({ capabilities: ['reasoning', 'llm'] })
  assert.equal(ack.role, 'hermes')
  assert.equal(ack.mesh.joined, true)
  assert.deepEqual(ack.mesh.capabilities, ['reasoning', 'llm'])

  const state = await (await fetch(`${BASE}/mesh`)).json()
  const peer = state.peers.find(p => p.name === 'hermes')
  assert.deepEqual(peer.capabilities, ['reasoning', 'llm'])
  assert.equal(peer.trail, 1)
  await hermes.close()
})

test('the UI observes the mesh but is not addressable as a peer', async () => {
  const ui = new Peer('crabdeck-ui')
  const ack = await ui.connect()
  assert.equal(ack.role, 'ui')
  assert.equal(ack.mesh.joined, false)

  // A UI trying to bid is refused: it never joined as a peer.
  ui.send({ type: 'MESH_BID', payload: { taskId: 'nope', confidence: 1 } })
  const err = await ui.waitFor('MESH_ERROR')
  assert.equal(err.code, 'NOT_A_PEER')
  await ui.close()
})

test('full round: announce → CFP → bids → award → results → consensus', async () => {
  const ui = new Peer('crabdeck-ui')
  const hermes = new Peer('hermes')
  const scribe = new Peer('scribe')
  const openclaw = new Peer('openclaw')
  await ui.connect()
  await hermes.connect({ capabilities: ['reasoning', 'llm'] })
  await scribe.connect({ capabilities: ['reasoning', 'retrieval'] })
  await openclaw.connect({ capabilities: ['system'] })

  ui.send({
    type: 'MESH_ANNOUNCE',
    payload: {
      taskId: 'task-e2e',
      task: 'when does the gateway watchdog trip?',
      capabilities: ['reasoning'],
      quorum: 2,
    },
  })

  const announced = await ui.waitFor('MESH_ANNOUNCED')
  assert.deepEqual(announced.payload.invited.sort(), ['hermes', 'scribe'])

  // Only capability-matching peers are invited.
  const cfpHermes = await hermes.waitFor('MESH_CFP')
  assert.equal(cfpHermes.payload.taskId, 'task-e2e')
  await scribe.waitFor('MESH_CFP')
  assert.ok(!openclaw.frames.some(f => f.type === 'MESH_CFP'), 'system-only peer must not get a reasoning CFP')

  hermes.send({ type: 'MESH_BID', payload: { taskId: 'task-e2e', confidence: 0.5 } })
  scribe.send({ type: 'MESH_BID', payload: { taskId: 'task-e2e', confidence: 0.95 } })
  await hermes.waitFor('MESH_BID_ACK')
  await scribe.waitFor('MESH_BID_ACK')

  // Both invitees bid, so the award fires without waiting out the window.
  const award = await scribe.waitFor('MESH_AWARD')
  assert.equal(award.payload.taskId, 'task-e2e')
  const closed = await hermes.waitFor('MESH_CFP_CLOSED')
  assert.equal(closed.payload.winner, 'scribe')

  const awarded = await ui.waitFor('MESH_AWARDED')
  assert.equal(awarded.payload.winner, 'scribe')
  assert.equal(awarded.payload.bids.length, 2)

  // Two peers answer independently; the mesh clusters them.
  scribe.send({
    type: 'MESH_RESULT',
    payload: { taskId: 'task-e2e', result: 'the watchdog trips after 20 seconds of silence', confidence: 0.9 },
  })
  hermes.send({
    type: 'MESH_RESULT',
    payload: { taskId: 'task-e2e', result: 'watchdog trips at 20 seconds of agent silence', confidence: 0.6 },
  })

  const consensus = await ui.waitFor('MESH_CONSENSUS')
  assert.equal(consensus.payload.taskId, 'task-e2e')
  assert.equal(consensus.payload.votes, 2)
  assert.equal(consensus.payload.confident, true)
  assert.ok(consensus.payload.answer.includes('20 seconds'))

  // A delivered result deposits pheromone on the responder.
  const state = await (await fetch(`${BASE}/mesh`)).json()
  assert.ok(state.trails.scribe > 1, 'accepted result must deposit trail')

  await Promise.all([ui.close(), hermes.close(), scribe.close(), openclaw.close()])
})

test('a CFP nobody can serve closes as unawarded', async () => {
  const ui = new Peer('crabdeck-ui')
  await ui.connect()
  ui.send({
    type: 'MESH_ANNOUNCE',
    payload: { taskId: 'task-orphan', task: 'fly to the moon', capabilities: ['propulsion'] },
  })
  const announced = await ui.waitFor('MESH_ANNOUNCED')
  assert.deepEqual(announced.payload.invited, [])
  const unawarded = await ui.waitFor('MESH_UNAWARDED')
  assert.equal(unawarded.payload.reason, 'no bids')
  await ui.close()
})

test('the bid window awards without every invitee answering', async () => {
  const ui = new Peer('crabdeck-ui')
  const hermes = new Peer('hermes')
  const scribe = new Peer('scribe')
  await ui.connect()
  await hermes.connect({ capabilities: ['reasoning'] })
  await scribe.connect({ capabilities: ['reasoning'] })

  ui.send({
    type: 'MESH_ANNOUNCE',
    payload: { taskId: 'task-slow', task: 'summarize', capabilities: ['reasoning'] },
  })
  await ui.waitFor('MESH_ANNOUNCED')
  await hermes.waitFor('MESH_CFP')
  // scribe stays silent; the window must still resolve.
  hermes.send({ type: 'MESH_BID', payload: { taskId: 'task-slow', confidence: 0.4 } })

  const awarded = await ui.waitFor('MESH_AWARDED', 4000)
  assert.equal(awarded.payload.winner, 'hermes')
  await Promise.all([ui.close(), hermes.close(), scribe.close()])
})

test('duplicate task ids and unknown contracts are refused', async () => {
  const ui = new Peer('crabdeck-ui')
  const hermes = new Peer('hermes')
  await ui.connect()
  await hermes.connect({ capabilities: ['reasoning'] })

  ui.send({ type: 'MESH_ANNOUNCE', payload: { taskId: 'task-dup', task: 'first', capabilities: ['reasoning'] } })
  await ui.waitFor('MESH_ANNOUNCED')
  ui.send({ type: 'MESH_ANNOUNCE', payload: { taskId: 'task-dup', task: 'second', capabilities: ['reasoning'] } })
  const err = await ui.waitFor('MESH_ERROR')
  assert.equal(err.code, 'DUPLICATE_TASK')

  hermes.send({ type: 'MESH_BID', payload: { taskId: 'task-ghost', confidence: 1 } })
  const bidErr = await hermes.waitFor('MESH_ERROR', 4000, f => f.code === 'NO_CONTRACT')
  assert.equal(bidErr.code, 'NO_CONTRACT')
  await Promise.all([ui.close(), hermes.close()])
})

test('an empty task is rejected', async () => {
  const ui = new Peer('crabdeck-ui')
  await ui.connect()
  ui.send({ type: 'MESH_ANNOUNCE', payload: { task: '   ' } })
  const err = await ui.waitFor('MESH_ERROR')
  assert.equal(err.code, 'BAD_TASK')
  await ui.close()
})

test('peers exchange direct messages by name', async () => {
  const hermes = new Peer('hermes')
  const scribe = new Peer('scribe')
  await hermes.connect({ capabilities: ['reasoning'] })
  await scribe.connect({ capabilities: ['retrieval'] })

  hermes.send({
    type: 'MESH_DIRECT',
    payload: { to: 'scribe', intent: 'retrieve', replyTo: 'task-7', body: { query: 'watchdog timing' } },
  })
  const ack = await hermes.waitFor('MESH_DIRECT_ACK')
  assert.equal(ack.payload.delivered, true)

  const received = await scribe.waitFor('MESH_MESSAGE')
  assert.equal(received.payload.from, 'hermes')
  assert.equal(received.payload.intent, 'retrieve')
  assert.equal(received.payload.body.query, 'watchdog timing')

  // An absent peer reports undelivered rather than erroring.
  hermes.send({ type: 'MESH_DIRECT', payload: { to: 'nobody', body: {} } })
  const miss = await hermes.waitFor('MESH_DIRECT_ACK', 4000, f => f.payload.to === 'nobody')
  assert.equal(miss.payload.delivered, false)

  await Promise.all([hermes.close(), scribe.close()])
})

test('gossip floods once, then dedupes by id', async () => {
  const hermes = new Peer('hermes')
  const scribe = new Peer('scribe')
  const openclaw = new Peer('openclaw')
  await hermes.connect({ capabilities: ['reasoning'] })
  await scribe.connect({ capabilities: ['retrieval'] })
  await openclaw.connect({ capabilities: ['system'] })

  hermes.send({
    type: 'MESH_GOSSIP',
    payload: { id: 'gossip-1', ttl: 2, topic: 'ollama', body: { status: 'degraded' } },
  })
  const ack = await hermes.waitFor('MESH_GOSSIP_ACK')
  assert.ok(ack.payload.forwarded >= 1)

  const seen = await scribe.waitFor('MESH_GOSSIP', 4000).catch(() => openclaw.waitFor('MESH_GOSSIP', 1000))
  assert.equal(seen.payload.ttl, 1, 'TTL must decrement on each hop')
  assert.equal(seen.payload.origin, 'hermes')

  // Replaying the same id is dropped — this is the loop guard.
  hermes.send({ type: 'MESH_GOSSIP', payload: { id: 'gossip-1', ttl: 2, body: {} } })
  const dup = await hermes.waitFor('MESH_GOSSIP_ACK', 4000, f => f.payload.duplicate === true)
  assert.equal(dup.payload.forwarded, 0)

  // An exhausted TTL stops travelling.
  hermes.send({ type: 'MESH_GOSSIP', payload: { id: 'gossip-2', ttl: 0, body: {} } })
  const expired = await hermes.waitFor('MESH_GOSSIP_ACK', 4000, f => f.payload.expired === true)
  assert.equal(expired.payload.forwarded, 0)

  await Promise.all([hermes.close(), scribe.close(), openclaw.close()])
})

test('a departing peer leaves the mesh and stops being invited', async () => {
  const hermes = new Peer('hermes')
  await hermes.connect({ capabilities: ['reasoning'] })
  let state = await (await fetch(`${BASE}/mesh`)).json()
  assert.ok(state.peers.some(p => p.name === 'hermes'))

  await hermes.close()
  await new Promise(r => setTimeout(r, 150))
  state = await (await fetch(`${BASE}/mesh`)).json()
  assert.ok(!state.peers.some(p => p.name === 'hermes'), 'closed socket must deregister the peer')
  // Trail persists across the restart so learned routing is not lost.
  assert.ok(Number.isFinite(state.trails.hermes))
})

test('legacy TO_HERMES / TO_OPENCLAW routing still works', async () => {
  const ui = new Peer('crabdeck-ui')
  const hermes = new Peer('hermes')
  const openclaw = new Peer('openclaw')
  await ui.connect()
  await hermes.connect({ capabilities: ['reasoning'] })
  await openclaw.connect({ capabilities: ['system'] })

  ui.send({ type: 'TO_HERMES', payload: { prompt: 'ping', model: 'llama3' } })
  const prompt = await hermes.waitFor('PROMPT')
  assert.equal(prompt.payload.prompt, 'ping')

  ui.send({ type: 'TO_OPENCLAW', payload: { task: 'disk usage' } })
  const task = await openclaw.waitFor('TASK')
  assert.equal(task.payload.task, 'disk usage')

  hermes.send({ type: 'HERMES_RESPONSE', payload: 'pong' })
  const reply = await ui.waitFor('HERMES_RESPONSE')
  assert.equal(reply.payload, 'pong')

  await Promise.all([ui.close(), hermes.close(), openclaw.close()])
})

test('heartbeats still ACK with a dead vault and carry mesh load', async () => {
  const hermes = new Peer('hermes')
  await hermes.connect({ capabilities: ['reasoning'] })
  const ts = Date.now() / 1000
  hermes.send({ type: 'HEARTBEAT', agent: 'hermes', ts, bhive_slot: Math.floor(ts / 60), load: 2 })
  const ack = await hermes.waitFor('HEARTBEAT_ACK')
  assert.ok(Number.isFinite(ack.bhive_slot), 'vault at a closed port must not block the ACK')

  const state = await (await fetch(`${BASE}/mesh`)).json()
  assert.equal(state.peers.find(p => p.name === 'hermes').load, 2)
  await hermes.close()
})
