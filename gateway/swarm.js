/**
 * CrabDeck swarm mesh — session state machine.
 *
 * Pure logic, no sockets or timers. server.js owns I/O and timeouts and
 * calls into this module so the round/quorum rules stay unit-testable.
 *
 * Topology: every connected agent role is a mesh peer. A SWARM_TASK fans
 * out to all peers as SWARM_ROUND 1 along with RAG context pulled from
 * Shell Cracked. Each SWARM_CONTRIBUTION is echoed to the other peers
 * (SWARM_PEER) and fed into the next round, so peers critique and build on
 * each other. When the last round closes, Hermes synthesizes the transcript
 * into a SWARM_RESULT that is written back to the vault as new memory.
 */

const { randomUUID } = require('crypto')

const SWARM_ROLES = Object.freeze(['hermes', 'openclaw', 'orchestrator'])

const DEFAULT_ROUNDS = 2
const MAX_ROUNDS = 4
const MAX_GOAL_CHARS = 4000
const MAX_TEXT_CHARS = 6000
const MAX_CONTEXT_HITS = 6
const MAX_CONTEXT_CHARS = 600
const MAX_SESSIONS = 50
const ROUND_TIMEOUT_MS = 45_000

function isRole(value) {
  return typeof value === 'string' && SWARM_ROLES.includes(value)
}

function clampText(value, max) {
  if (typeof value !== 'string') return ''
  return value.length > max ? value.slice(0, max) : value
}

/** Roles currently connected + authed, deduped, in canonical order. */
function meshRoster(clients) {
  const seen = new Set()
  for (const c of clients.values()) {
    if (c && c.authed && isRole(c.role)) seen.add(c.role)
  }
  return SWARM_ROLES.filter((r) => seen.has(r))
}

/** Validate an inbound SWARM_TASK payload. Returns null when unusable. */
function normalizeTask(payload) {
  const raw = payload && typeof payload === 'object' && !Array.isArray(payload)
    ? payload
    : { goal: payload }
  const goal = typeof raw.goal === 'string' ? raw.goal.trim() : ''
  if (!goal) return null
  let rounds = Number.isInteger(raw.rounds) ? raw.rounds : DEFAULT_ROUNDS
  if (rounds < 1) rounds = 1
  if (rounds > MAX_ROUNDS) rounds = MAX_ROUNDS
  const model = typeof raw.model === 'string' && raw.model.trim() ? raw.model.trim() : null
  return { goal: clampText(goal, MAX_GOAL_CHARS), rounds, model }
}

/** Validate an inbound SWARM_CONTRIBUTION / SWARM_SYNTHESIS payload. */
function normalizeContribution(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null
  const sessionId = typeof payload.session_id === 'string' ? payload.session_id.trim() : ''
  if (!sessionId) return null
  const round = Number.isInteger(payload.round) ? payload.round : null
  const text = typeof payload.text === 'string' ? payload.text.trim() : ''
  if (!text) return null
  return { session_id: sessionId, round, text: clampText(text, MAX_TEXT_CHARS) }
}

/** Validate an inbound MESH (peer-to-peer) frame. */
function normalizeMesh(msg) {
  if (!msg || typeof msg !== 'object') return null
  if (!isRole(msg.to)) return null
  const raw = msg.payload
  const payload = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : { text: raw }
  const text = typeof payload.text === 'string' ? payload.text.trim() : String(payload.text ?? '').trim()
  if (!text) return null
  const intent = payload.intent === 'ask' ? 'ask' : 'tell'
  const out = { to: msg.to, payload: { intent, text: clampText(text, MAX_TEXT_CHARS) } }
  if (typeof payload.session_id === 'string' && payload.session_id.trim()) {
    out.payload.session_id = payload.session_id.trim()
  }
  return out
}

/**
 * Trim vault hits into a compact RAG context block.
 * `minScore` gates low-similarity hits (default 0 = keep everything, because
 * the default blake2 embeddings are hash similarity, not semantics).
 */
function trimContext(hits, minScore = 0) {
  if (!Array.isArray(hits)) return []
  const gate = typeof minScore === 'number' && Number.isFinite(minScore) ? minScore : 0
  const out = []
  for (const hit of hits) {
    if (!hit || typeof hit !== 'object') continue
    const text = typeof hit.text === 'string' ? hit.text.trim() : ''
    if (!text) continue
    const score = typeof hit.score === 'number' && Number.isFinite(hit.score) ? hit.score : 0
    if (score < gate) continue
    const meta = hit.metadata && typeof hit.metadata === 'object' ? hit.metadata : {}
    out.push({
      id: typeof hit.id === 'string' ? hit.id : '',
      text: clampText(text, MAX_CONTEXT_CHARS),
      agent: typeof meta.agent === 'string' ? meta.agent : 'unknown',
      kind: typeof meta.kind === 'string' ? meta.kind : 'memory',
      score: Number(score.toFixed(4)),
    })
    if (out.length >= MAX_CONTEXT_HITS) break
  }
  return out
}

function createSession({ goal, rounds, model, participants, context, from, minScore = 0, now = Date.now() }) {
  if (typeof goal !== 'string' || !goal.trim()) throw new Error('goal must be a non-empty string')
  if (!Array.isArray(participants) || participants.length === 0) throw new Error('participants must be non-empty')
  for (const p of participants) if (!isRole(p)) throw new Error(`unknown swarm role: ${p}`)
  const maxRounds = Number.isInteger(rounds) && rounds >= 1 ? Math.min(rounds, MAX_ROUNDS) : DEFAULT_ROUNDS
  return {
    id: randomUUID(),
    goal,
    model: typeof model === 'string' && model ? model : null,
    from: typeof from === 'string' ? from : 'ui',
    participants: [...participants],
    maxRounds,
    round: 1,
    status: 'running',            // running | synthesizing | done | failed
    context: trimContext(context, minScore),
    rounds: [{ index: 1, startedAt: now, contributions: {} }],
    result: null,
    synthesizedBy: null,
    createdAt: now,
    finishedAt: null,
    error: null,
  }
}

function currentRound(session) {
  return session.rounds[session.rounds.length - 1]
}

function roundComplete(session) {
  const cur = currentRound(session)
  return session.participants.every((p) => Object.prototype.hasOwnProperty.call(cur.contributions, p))
}

/**
 * Record a peer's contribution for the current round.
 * Returns { accepted, reason?, roundComplete }.
 */
function recordContribution(session, role, round, text, now = Date.now()) {
  if (session.status !== 'running') return { accepted: false, reason: 'not_running', roundComplete: false }
  if (!session.participants.includes(role)) return { accepted: false, reason: 'not_participant', roundComplete: false }
  if (round !== null && round !== session.round) return { accepted: false, reason: 'stale_round', roundComplete: false }
  const cur = currentRound(session)
  if (Object.prototype.hasOwnProperty.call(cur.contributions, role)) {
    return { accepted: false, reason: 'duplicate', roundComplete: roundComplete(session) }
  }
  cur.contributions[role] = { text: clampText(text, MAX_TEXT_CHARS), ts: now }
  return { accepted: true, roundComplete: roundComplete(session) }
}

/** Contributions of the round before the current one, as role → text. */
function previousContributions(session) {
  if (session.rounds.length < 2) return {}
  const prev = session.rounds[session.rounds.length - 2]
  const out = {}
  for (const [role, c] of Object.entries(prev.contributions)) out[role] = c.text
  return out
}

/** Payload agents receive for a round. */
function roundPayload(session) {
  return {
    session_id: session.id,
    goal: session.goal,
    model: session.model,
    round: session.round,
    max_rounds: session.maxRounds,
    peers: [...session.participants],
    context: session.context,
    contributions: previousContributions(session),
  }
}

/**
 * Close the current round. Missing peers are recorded as silent.
 * Returns 'round' when a new round opened, 'synthesize' when the last round
 * closed, or 'failed' when nobody contributed in round 1.
 */
function advance(session, now = Date.now()) {
  if (session.status !== 'running') return session.status
  const cur = currentRound(session)
  cur.closedAt = now
  cur.silent = session.participants.filter((p) => !Object.prototype.hasOwnProperty.call(cur.contributions, p))
  const spoke = Object.keys(cur.contributions).length
  if (spoke === 0 && session.round === 1) {
    session.status = 'failed'
    session.error = 'no swarm peer contributed in round 1'
    session.finishedAt = now
    return 'failed'
  }
  if (session.round < session.maxRounds) {
    session.round += 1
    session.rounds.push({ index: session.round, startedAt: now, contributions: {} })
    return 'round'
  }
  session.status = 'synthesizing'
  return 'synthesize'
}

/** Flat transcript for synthesis prompts and UI. */
function transcript(session) {
  const out = []
  for (const r of session.rounds) {
    for (const [role, c] of Object.entries(r.contributions)) {
      out.push({ round: r.index, agent: role, text: c.text })
    }
  }
  return out
}

function synthesizePayload(session) {
  return {
    session_id: session.id,
    goal: session.goal,
    model: session.model,
    peers: [...session.participants],
    context: session.context,
    transcript: transcript(session),
  }
}

/** Deterministic fallback when no synthesizer peer is available. */
function fallbackSynthesis(session) {
  const lines = [`Swarm result for: ${session.goal}`]
  for (const t of transcript(session)) lines.push(`\n[round ${t.round} · ${t.agent}]\n${t.text}`)
  return lines.join('\n')
}

function finalize(session, text, synthesizedBy, now = Date.now()) {
  if (session.status === 'done' || session.status === 'failed') return false
  const body = typeof text === 'string' && text.trim() ? clampText(text.trim(), MAX_TEXT_CHARS) : fallbackSynthesis(session)
  session.result = body
  session.synthesizedBy = isRole(synthesizedBy) ? synthesizedBy : 'gateway'
  session.status = 'done'
  session.finishedAt = now
  return true
}

function fail(session, reason, now = Date.now()) {
  if (session.status === 'done' || session.status === 'failed') return false
  session.status = 'failed'
  session.error = typeof reason === 'string' && reason ? reason : 'swarm failed'
  session.finishedAt = now
  return true
}

function resultPayload(session) {
  return {
    session_id: session.id,
    goal: session.goal,
    status: session.status,
    peers: [...session.participants],
    rounds: session.rounds.length,
    result: session.result,
    synthesized_by: session.synthesizedBy,
    transcript: transcript(session),
    context: session.context,
    error: session.error,
    started_at: session.createdAt,
    finished_at: session.finishedAt,
  }
}

function summary(session) {
  return {
    session_id: session.id,
    goal: session.goal.slice(0, 160),
    status: session.status,
    round: session.round,
    max_rounds: session.maxRounds,
    peers: [...session.participants],
    created_at: session.createdAt,
    finished_at: session.finishedAt,
  }
}

/** Keep the in-memory session map bounded; oldest finished sessions go first. */
function prune(sessions, max = MAX_SESSIONS) {
  if (sessions.size <= max) return 0
  const finished = [...sessions.values()]
    .filter((s) => s.status === 'done' || s.status === 'failed')
    .sort((a, b) => (a.finishedAt ?? 0) - (b.finishedAt ?? 0))
  let removed = 0
  for (const s of finished) {
    if (sessions.size <= max) break
    sessions.delete(s.id)
    removed += 1
  }
  return removed
}

module.exports = {
  SWARM_ROLES,
  DEFAULT_ROUNDS,
  MAX_ROUNDS,
  MAX_TEXT_CHARS,
  MAX_CONTEXT_HITS,
  MAX_SESSIONS,
  ROUND_TIMEOUT_MS,
  isRole,
  meshRoster,
  normalizeTask,
  normalizeContribution,
  normalizeMesh,
  trimContext,
  createSession,
  currentRound,
  roundComplete,
  recordContribution,
  previousContributions,
  roundPayload,
  advance,
  transcript,
  synthesizePayload,
  fallbackSynthesis,
  finalize,
  fail,
  resultPayload,
  summary,
  prune,
}
