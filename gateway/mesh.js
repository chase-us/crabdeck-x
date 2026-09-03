/**
 * CrabDeck swarm mesh — peer coordination primitives.
 *
 * Four classic multi-agent mechanisms, kept as pure functions plus one
 * bounded registry so the whole coordination layer is unit testable without
 * a socket:
 *
 *   1. Contract Net Protocol — announce → CFP → bid → award. Task allocation
 *      is decided by the peers that know their own load, not by the gateway
 *      guessing.
 *   2. Stigmergy (ant-colony pheromone trails) — awards that produce accepted
 *      results deposit trail on the winner; every trail evaporates. Allocation
 *      therefore improves from observed outcomes instead of static priority.
 *   3. Gossip — bounded-fanout flood with TTL and a seen-set, so mesh-wide
 *      notices propagate without a broadcast storm.
 *   4. Quorum consensus — independent peer answers are clustered by token
 *      overlap; the largest cluster wins and reports its agreement ratio.
 *
 * Hardening (see .cursor/rules/crabdeck-payloads.mdc): every input here is
 * untrusted. Capabilities, ids, TTLs, and collection sizes are all clamped,
 * and no map grows without a cap.
 */

'use strict'

// ── Limits ────────────────────────────────────────────────────────────────────
const MAX_CAPABILITIES = 12
const MAX_CAPABILITY_LEN = 32
const MAX_TASK_LEN = 4000
const MAX_RESULT_LEN = 8000
const TTL_MAX = 4
const SEEN_MAX = 2048
const MAX_CONTRACTS = 128
const MAX_BIDS = 32
const MAX_RESULTS = 16
const GOSSIP_FANOUT = 3

// ── Pheromone (ACO) constants ────────────────────────────────────────────────
const TRAIL_MIN = 0.1
const TRAIL_MAX = 4.0
const TRAIL_INIT = 1.0
const EVAPORATION = 0.1   // rho
const DEPOSIT = 0.6       // reward for an accepted result
const PENALTY = 0.3       // deduction for a failed/withdrawn award

const CAPABILITY_RE = /^[a-z0-9][a-z0-9_.-]*$/
const TOKEN_RE = /[a-z0-9][a-z0-9_+.#-]*/g

// ── Validation helpers ───────────────────────────────────────────────────────

function normalizeCapabilities(raw) {
  if (!Array.isArray(raw)) return []
  const out = []
  for (const entry of raw) {
    if (typeof entry !== 'string') continue
    const cap = entry.trim().toLowerCase()
    if (!cap || cap.length > MAX_CAPABILITY_LEN) continue
    if (!CAPABILITY_RE.test(cap)) continue
    if (!out.includes(cap)) out.push(cap)
    if (out.length >= MAX_CAPABILITIES) break
  }
  return out
}

function clampText(value, max) {
  if (typeof value !== 'string') return ''
  return value.length > max ? value.slice(0, max) : value
}

function clampTtl(value) {
  const n = Number.isFinite(value) ? Math.floor(value) : TTL_MAX
  if (n < 0) return 0
  return n > TTL_MAX ? TTL_MAX : n
}

function clampUnit(value, fallback = 0.5) {
  if (!Number.isFinite(value)) return fallback
  if (value < 0) return 0
  return value > 1 ? 1 : value
}

// ── Capability matching ──────────────────────────────────────────────────────

/**
 * Coverage of `required` by `offered`. An empty requirement matches every peer
 * (an open call for proposals), which keeps generalists reachable.
 */
function capabilityMatch(offered, required) {
  const have = normalizeCapabilities(offered)
  const need = normalizeCapabilities(required)
  if (need.length === 0) return { covered: [], missing: [], ratio: have.length ? 1 : 0.5 }
  const covered = need.filter(cap => have.includes(cap))
  const missing = need.filter(cap => !have.includes(cap))
  return { covered, missing, ratio: covered.length / need.length }
}

// ── Contract Net scoring ─────────────────────────────────────────────────────

/**
 * Utility of one bid.
 *
 *   score = confidence × capabilityRatio × trail ÷ (1 + load) ÷ (1 + cost)
 *
 * Confidence and load are self-reported by the peer (it alone knows its
 * queue); capability ratio and trail are the gateway's own observations, so a
 * peer cannot win purely by claiming confidence 1.0.
 */
function scoreBid(bid, peer, required, trail = TRAIL_INIT) {
  const confidence = clampUnit(bid && bid.confidence, 0.5)
  const cost = Math.max(0, Number.isFinite(bid && bid.cost) ? bid.cost : 0)
  const load = Math.max(0, Number.isFinite(peer && peer.load) ? peer.load : 0)
  const offered = (bid && bid.capabilities) || (peer && peer.capabilities) || []
  const { ratio } = capabilityMatch(offered, required)
  const weight = Math.min(TRAIL_MAX, Math.max(TRAIL_MIN, Number.isFinite(trail) ? trail : TRAIL_INIT))
  if (ratio === 0) return 0
  return (confidence * ratio * weight) / ((1 + load) * (1 + cost))
}

/** Highest scoring bid. Ties break on peer name so awards are reproducible. */
function selectWinner(scored) {
  if (!Array.isArray(scored) || scored.length === 0) return null
  let best = null
  for (const entry of scored) {
    if (!entry || typeof entry.peer !== 'string' || !(entry.score > 0)) continue
    if (
      best === null ||
      entry.score > best.score ||
      (entry.score === best.score && entry.peer < best.peer)
    ) {
      best = entry
    }
  }
  return best
}

// ── Stigmergy ────────────────────────────────────────────────────────────────

/** Deposit on success, deduct on failure; clamped so no peer becomes absorbing. */
function reinforce(trail, outcome) {
  const base = Number.isFinite(trail) ? trail : TRAIL_INIT
  const delta = outcome === 'success' ? DEPOSIT : outcome === 'failure' ? -PENALTY : 0
  return Math.min(TRAIL_MAX, Math.max(TRAIL_MIN, base + delta))
}

/** Pull every trail toward TRAIL_INIT so stale wins stop dominating. */
function evaporate(trail, rho = EVAPORATION) {
  const base = Number.isFinite(trail) ? trail : TRAIL_INIT
  const rate = clampUnit(rho, EVAPORATION)
  const next = base + (TRAIL_INIT - base) * rate
  return Math.min(TRAIL_MAX, Math.max(TRAIL_MIN, next))
}

// ── Gossip ───────────────────────────────────────────────────────────────────

/**
 * Bounded fan-out. Peers are picked from a rotating offset derived from the
 * sender, so repeated gossip from one peer still reaches the whole mesh
 * instead of hammering the same three neighbours.
 */
function gossipTargets(peerIds, fromId, fanout = GOSSIP_FANOUT) {
  if (!Array.isArray(peerIds)) return []
  const pool = peerIds.filter(id => typeof id === 'string' && id && id !== fromId).sort()
  const width = Math.max(1, Math.min(Number.isFinite(fanout) ? fanout : GOSSIP_FANOUT, pool.length))
  if (pool.length === 0) return []
  let offset = 0
  for (const ch of String(fromId || '')) offset = (offset * 31 + ch.charCodeAt(0)) % pool.length
  const out = []
  for (let i = 0; i < width; i += 1) out.push(pool[(offset + i) % pool.length])
  return out
}

/** LRU-bounded dedup set. `add` returns false when the id was already seen. */
class SeenSet {
  constructor(max = SEEN_MAX) {
    this._max = Math.max(1, max)
    this._ids = new Set()
  }

  add(id) {
    if (typeof id !== 'string' || !id) return false
    if (this._ids.has(id)) return false
    this._ids.add(id)
    if (this._ids.size > this._max) {
      // Insertion-ordered: the first key is the oldest.
      this._ids.delete(this._ids.values().next().value)
    }
    return true
  }

  get size() { return this._ids.size }
}

// ── Quorum consensus ─────────────────────────────────────────────────────────

function tokenSet(text) {
  if (typeof text !== 'string') return new Set()
  return new Set(text.toLowerCase().match(TOKEN_RE) || [])
}

function jaccard(a, b) {
  if (a.size === 0 || b.size === 0) return 0
  let shared = 0
  for (const token of a) if (b.has(token)) shared += 1
  return shared / (a.size + b.size - shared)
}

/**
 * Cluster independent answers by token overlap and return the largest cluster.
 *
 * Two peers that independently reach the same conclusion are stronger evidence
 * than one confident peer, which is the whole point of asking several. The
 * caller decides what to do with a low `agreement`.
 */
function quorum(results, { threshold = 0.45, minVotes = 2 } = {}) {
  const usable = (Array.isArray(results) ? results : []).filter(
    r => r && typeof r.result === 'string' && r.result.trim()
  )
  if (usable.length === 0) {
    return { answer: null, agreement: 0, votes: 0, total: 0, peers: [], confident: false }
  }

  const sets = usable.map(r => tokenSet(r.result))
  const clusters = []
  for (let i = 0; i < usable.length; i += 1) {
    let placed = false
    for (const cluster of clusters) {
      if (jaccard(sets[i], sets[cluster.seed]) >= threshold) {
        cluster.members.push(i)
        placed = true
        break
      }
    }
    if (!placed) clusters.push({ seed: i, members: [i] })
  }

  clusters.sort((a, b) => {
    if (b.members.length !== a.members.length) return b.members.length - a.members.length
    return bestConfidence(usable, b.members) - bestConfidence(usable, a.members)
  })

  const winner = clusters[0]
  // Speak for the cluster with its most confident member.
  const spokesman = winner.members.reduce(
    (best, i) => (confidenceOf(usable[i]) > confidenceOf(usable[best]) ? i : best),
    winner.members[0]
  )
  const votes = winner.members.length
  return {
    answer: usable[spokesman].result,
    agreement: votes / usable.length,
    votes,
    total: usable.length,
    peers: winner.members.map(i => usable[i].peer).filter(Boolean),
    confident: votes >= minVotes && votes / usable.length > 0.5,
  }
}

function confidenceOf(entry) {
  return clampUnit(entry && entry.confidence, 0.5)
}

function bestConfidence(list, members) {
  return members.reduce((max, i) => Math.max(max, confidenceOf(list[i])), 0)
}

// ── Registry ─────────────────────────────────────────────────────────────────

/**
 * Live mesh state: who is present, what they can do, which contracts are open,
 * and how much trail each peer carries. Bounded on every axis.
 */
class MeshRegistry {
  constructor({ now = () => Date.now(), maxContracts = MAX_CONTRACTS } = {}) {
    this._now = now
    this._maxContracts = Math.max(1, maxContracts)
    this._peers = new Map()      // name → peer
    this._contracts = new Map()  // taskId → contract
    this._trails = new Map()     // name → pheromone
    this._seen = new SeenSet()
  }

  // ── Peers ──
  join(name, { capabilities = [], clientId = null, version = '' } = {}) {
    if (typeof name !== 'string' || !name.trim()) return null
    const id = name.trim().toLowerCase()
    if (!CAPABILITY_RE.test(id)) return null
    const peer = {
      name: id,
      clientId,
      capabilities: normalizeCapabilities(capabilities),
      version: clampText(version, 32),
      load: 0,
      joinedAt: this._now(),
      lastSeen: this._now(),
      awarded: 0,
      completed: 0,
    }
    this._peers.set(id, peer)
    if (!this._trails.has(id)) this._trails.set(id, TRAIL_INIT)
    return peer
  }

  leave(name) {
    if (typeof name !== 'string') return false
    // Trail survives a reconnect on purpose: an agent restart should not
    // discard what the mesh learned about it.
    return this._peers.delete(name.trim().toLowerCase())
  }

  touch(name, load) {
    const peer = this._peers.get(String(name || '').toLowerCase())
    if (!peer) return null
    peer.lastSeen = this._now()
    if (Number.isFinite(load) && load >= 0) peer.load = Math.min(64, Math.floor(load))
    return peer
  }

  peer(name) { return this._peers.get(String(name || '').toLowerCase()) || null }
  peers() { return [...this._peers.values()] }
  peerNames() { return [...this._peers.keys()] }

  trail(name) {
    const value = this._trails.get(String(name || '').toLowerCase())
    return Number.isFinite(value) ? value : TRAIL_INIT
  }

  trails() { return Object.fromEntries(this._trails) }

  /** Peers whose capabilities cover at least part of `required`. */
  capablePeers(required, { exclude = null } = {}) {
    const need = normalizeCapabilities(required)
    return this.peers().filter(peer => {
      if (exclude && peer.name === exclude) return false
      if (need.length === 0) return true
      return capabilityMatch(peer.capabilities, need).ratio > 0
    })
  }

  // ── Contracts (Contract Net) ──
  openContract(taskId, { task, capabilities = [], announcer, invited = [], quorumTarget = 1 }) {
    if (typeof taskId !== 'string' || !taskId.trim()) return null
    const id = taskId.trim().slice(0, 64)
    if (this._contracts.size >= this._maxContracts) {
      // Evict the oldest open contract rather than refusing new work.
      const oldest = [...this._contracts.entries()].sort(
        (a, b) => a[1].openedAt - b[1].openedAt
      )[0]
      if (oldest) this._contracts.delete(oldest[0])
    }
    const contract = {
      taskId: id,
      task: clampText(task, MAX_TASK_LEN),
      capabilities: normalizeCapabilities(capabilities),
      announcer: typeof announcer === 'string' ? announcer : null,
      invited: invited.filter(n => typeof n === 'string').slice(0, MAX_BIDS),
      bids: [],
      results: [],
      winner: null,
      state: 'open',
      quorumTarget: Math.max(1, Math.min(MAX_RESULTS, Math.floor(quorumTarget) || 1)),
      openedAt: this._now(),
    }
    this._contracts.set(id, contract)
    return contract
  }

  contract(taskId) { return this._contracts.get(String(taskId || '').slice(0, 64)) || null }
  contracts() { return [...this._contracts.values()] }

  addBid(taskId, peerName, bid) {
    const contract = this.contract(taskId)
    if (!contract || contract.state !== 'open') return null
    const name = String(peerName || '').toLowerCase()
    if (!this._peers.has(name)) return null
    if (contract.bids.length >= MAX_BIDS) return null
    if (contract.bids.some(b => b.peer === name)) return null  // one bid per peer
    const entry = {
      peer: name,
      confidence: clampUnit(bid && bid.confidence, 0.5),
      cost: Math.max(0, Number.isFinite(bid && bid.cost) ? bid.cost : 0),
      capabilities: normalizeCapabilities(
        (bid && bid.capabilities) || this._peers.get(name).capabilities
      ),
      note: clampText(bid && bid.note, 200),
      at: this._now(),
    }
    entry.score = scoreBid(entry, this._peers.get(name), contract.capabilities, this.trail(name))
    contract.bids.push(entry)
    return entry
  }

  /** Close bidding and award to the best bid. Idempotent. */
  award(taskId) {
    const contract = this.contract(taskId)
    if (!contract || contract.state !== 'open') return null
    const winner = selectWinner(contract.bids)
    if (!winner) {
      contract.state = 'unawarded'
      return null
    }
    contract.winner = winner.peer
    contract.state = 'awarded'
    const peer = this._peers.get(winner.peer)
    if (peer) peer.awarded += 1
    return winner
  }

  /** Record a peer's answer. Trail moves on the outcome, not on the award. */
  recordResult(taskId, peerName, { result, confidence = 0.5, ok = true } = {}) {
    const contract = this.contract(taskId)
    if (!contract) return null
    const name = String(peerName || '').toLowerCase()
    if (contract.results.length >= MAX_RESULTS) return null
    const entry = {
      peer: name,
      result: clampText(result, MAX_RESULT_LEN),
      confidence: clampUnit(confidence, 0.5),
      ok: ok !== false,
      at: this._now(),
    }
    contract.results.push(entry)
    const peer = this._peers.get(name)
    if (peer) peer.completed += 1
    this._trails.set(name, reinforce(this.trail(name), entry.ok ? 'success' : 'failure'))
    if (contract.results.length >= contract.quorumTarget) contract.state = 'complete'
    return entry
  }

  /** Consensus over a contract's collected results. */
  consensus(taskId, options) {
    const contract = this.contract(taskId)
    if (!contract) return null
    return quorum(contract.results, options)
  }

  /** Periodic decay. Call on the same cadence as the heartbeat watchdog. */
  tick(rho = EVAPORATION) {
    for (const [name, trail] of this._trails) this._trails.set(name, evaporate(trail, rho))
  }

  /** Drop contracts that finished or went stale, so the map stays bounded. */
  sweep(maxAgeMs = 300_000) {
    const cutoff = this._now() - Math.max(1000, maxAgeMs)
    let removed = 0
    for (const [id, contract] of this._contracts) {
      const finished = contract.state === 'complete' || contract.state === 'unawarded'
      if (contract.openedAt < cutoff || (finished && contract.openedAt < this._now() - 60_000)) {
        this._contracts.delete(id)
        removed += 1
      }
    }
    return removed
  }

  /** True the first time this gossip id is seen. */
  observeGossip(id) { return this._seen.add(id) }

  snapshot() {
    return {
      peers: this.peers().map(peer => ({
        name: peer.name,
        capabilities: peer.capabilities,
        load: peer.load,
        awarded: peer.awarded,
        completed: peer.completed,
        trail: Number(this.trail(peer.name).toFixed(3)),
        lastSeen: peer.lastSeen,
      })),
      contracts: this.contracts().map(c => ({
        taskId: c.taskId,
        task: c.task.slice(0, 160),
        capabilities: c.capabilities,
        announcer: c.announcer,
        state: c.state,
        winner: c.winner,
        bids: c.bids.map(b => ({ peer: b.peer, score: Number(b.score.toFixed(4)), confidence: b.confidence })),
        results: c.results.length,
        openedAt: c.openedAt,
      })),
      trails: this.trails(),
      gossipSeen: this._seen.size,
    }
  }
}

module.exports = {
  DEPOSIT,
  EVAPORATION,
  GOSSIP_FANOUT,
  MAX_CAPABILITIES,
  MAX_RESULT_LEN,
  MAX_TASK_LEN,
  MeshRegistry,
  PENALTY,
  SeenSet,
  TRAIL_INIT,
  TRAIL_MAX,
  TRAIL_MIN,
  TTL_MAX,
  capabilityMatch,
  clampTtl,
  clampText,
  clampUnit,
  evaporate,
  gossipTargets,
  jaccard,
  normalizeCapabilities,
  quorum,
  reinforce,
  scoreBid,
  selectWinner,
  tokenSet,
}
