const test = require('node:test')
const assert = require('node:assert/strict')
const swarm = require('./swarm')

function clientsWith(...roles) {
  const m = new Map()
  roles.forEach((role, i) => m.set(`c${i}`, { role, authed: true }))
  return m
}

test('meshRoster dedupes roles and ignores ui/unknown/unauthed', () => {
  const clients = clientsWith('hermes', 'hermes', 'ui', 'unknown', 'openclaw')
  clients.set('x', { role: 'orchestrator', authed: false })
  assert.deepEqual(swarm.meshRoster(clients), ['hermes', 'openclaw'])
})

test('normalizeTask requires a goal and clamps rounds', () => {
  assert.equal(swarm.normalizeTask({}), null)
  assert.equal(swarm.normalizeTask({ goal: '   ' }), null)
  assert.equal(swarm.normalizeTask(['goal']), null)
  const t = swarm.normalizeTask({ goal: ' plan a release ', rounds: 99, model: 'llama3' })
  assert.equal(t.goal, 'plan a release')
  assert.equal(t.rounds, swarm.MAX_ROUNDS)
  assert.equal(t.model, 'llama3')
  assert.equal(swarm.normalizeTask({ goal: 'x', rounds: 0 }).rounds, 1)
  assert.equal(swarm.normalizeTask('bare string goal').goal, 'bare string goal')
  assert.equal(swarm.normalizeTask({ goal: 'x' }).rounds, swarm.DEFAULT_ROUNDS)
})

test('normalizeContribution rejects blank text or missing session', () => {
  assert.equal(swarm.normalizeContribution(null), null)
  assert.equal(swarm.normalizeContribution({ text: 'hi' }), null)
  assert.equal(swarm.normalizeContribution({ session_id: 's', text: '  ' }), null)
  const c = swarm.normalizeContribution({ session_id: ' s1 ', round: 2, text: ' ok ' })
  assert.deepEqual(c, { session_id: 's1', round: 2, text: 'ok' })
  assert.equal(swarm.normalizeContribution({ session_id: 's1', round: '2', text: 'ok' }).round, null)
  const long = swarm.normalizeContribution({ session_id: 's', text: 'a'.repeat(10_000) })
  assert.equal(long.text.length, swarm.MAX_TEXT_CHARS)
})

test('normalizeMesh requires a known peer and non-empty text', () => {
  assert.equal(swarm.normalizeMesh({ to: 'ui', payload: { text: 'x' } }), null)
  assert.equal(swarm.normalizeMesh({ to: 'hermes', payload: { text: '' } }), null)
  const m = swarm.normalizeMesh({ to: 'openclaw', payload: { intent: 'ask', text: 'status?', session_id: 's1' } })
  assert.deepEqual(m, { to: 'openclaw', payload: { intent: 'ask', text: 'status?', session_id: 's1' } })
  assert.equal(swarm.normalizeMesh({ to: 'hermes', payload: 'raw text' }).payload.intent, 'tell')
  assert.equal(swarm.normalizeMesh({ to: 'hermes', payload: { intent: 'delete', text: 'x' } }).payload.intent, 'tell')
})

test('trimContext keeps only well-formed hits and caps size', () => {
  const hits = [
    { id: 'a', text: 'x'.repeat(2000), metadata: { agent: 'hermes', kind: 'prompt_result' }, score: 0.987654 },
    { id: 'b', text: '   ' },
    'garbage',
    { id: 'c', text: 'ok', metadata: 'not-a-dict' },
  ]
  const ctx = swarm.trimContext(hits)
  assert.equal(ctx.length, 2)
  assert.equal(ctx[0].text.length, 600)
  assert.equal(ctx[0].agent, 'hermes')
  assert.equal(ctx[0].score, 0.9877)
  assert.equal(ctx[1].agent, 'unknown')
  assert.equal(swarm.trimContext(null).length, 0)
  const many = Array.from({ length: 20 }, (_, i) => ({ id: String(i), text: 'hit' }))
  assert.equal(swarm.trimContext(many).length, swarm.MAX_CONTEXT_HITS)
})

test('trimContext similarity gate drops weak hits', () => {
  const hits = [
    { id: 'strong', text: 'a', score: 0.91 },
    { id: 'weak', text: 'b', score: 0.3 },
    { id: 'unscored', text: 'c' },
  ]
  assert.deepEqual(swarm.trimContext(hits, 0.82).map((h) => h.id), ['strong'])
  assert.equal(swarm.trimContext(hits, 0).length, 3)
  assert.equal(swarm.trimContext(hits, 'not-a-number').length, 3)
})

test('createSession validates participants and roles', () => {
  assert.throws(() => swarm.createSession({ goal: 'g', participants: [] }))
  assert.throws(() => swarm.createSession({ goal: 'g', participants: ['ui'] }))
  assert.throws(() => swarm.createSession({ goal: '', participants: ['hermes'] }))
  const s = swarm.createSession({ goal: 'g', participants: ['hermes', 'openclaw'], rounds: 3 })
  assert.equal(s.status, 'running')
  assert.equal(s.round, 1)
  assert.equal(s.maxRounds, 3)
  assert.equal(s.rounds.length, 1)
})

test('full two-round swarm: contributions feed the next round, then synthesize', () => {
  const s = swarm.createSession({
    goal: 'harden the gateway',
    participants: ['hermes', 'openclaw', 'orchestrator'],
    rounds: 2,
    context: [{ id: 'm1', text: 'previous note', metadata: { agent: 'hermes', kind: 'prompt_result' }, score: 0.5 }],
    now: 1000,
  })
  const r1 = swarm.roundPayload(s)
  assert.equal(r1.round, 1)
  assert.deepEqual(r1.contributions, {})
  assert.equal(r1.context[0].text, 'previous note')

  let res = swarm.recordContribution(s, 'hermes', 1, 'h1', 1001)
  assert.deepEqual(res, { accepted: true, roundComplete: false })
  res = swarm.recordContribution(s, 'hermes', 1, 'h1-again', 1002)
  assert.equal(res.accepted, false)
  assert.equal(res.reason, 'duplicate')
  res = swarm.recordContribution(s, 'openclaw', 1, 'o1', 1003)
  assert.equal(res.roundComplete, false)
  res = swarm.recordContribution(s, 'orchestrator', 1, 'r1', 1004)
  assert.equal(res.roundComplete, true)

  assert.equal(swarm.advance(s, 1005), 'round')
  assert.equal(s.round, 2)
  const r2 = swarm.roundPayload(s)
  assert.deepEqual(r2.contributions, { hermes: 'h1', openclaw: 'o1', orchestrator: 'r1' })

  // stale round number is rejected
  assert.equal(swarm.recordContribution(s, 'hermes', 1, 'late').reason, 'stale_round')
  // null round means "current round"
  assert.equal(swarm.recordContribution(s, 'hermes', null, 'h2').accepted, true)
  swarm.recordContribution(s, 'openclaw', 2, 'o2')
  swarm.recordContribution(s, 'orchestrator', 2, 'r2')

  assert.equal(swarm.advance(s, 2000), 'synthesize')
  assert.equal(s.status, 'synthesizing')
  assert.equal(swarm.recordContribution(s, 'hermes', 2, 'x').reason, 'not_running')

  const syn = swarm.synthesizePayload(s)
  assert.equal(syn.transcript.length, 6)
  assert.deepEqual(syn.transcript[0], { round: 1, agent: 'hermes', text: 'h1' })

  assert.equal(swarm.finalize(s, ' final answer ', 'hermes', 3000), true)
  assert.equal(s.status, 'done')
  assert.equal(s.result, 'final answer')
  assert.equal(s.synthesizedBy, 'hermes')
  assert.equal(swarm.finalize(s, 'again', 'hermes'), false)
  const out = swarm.resultPayload(s)
  assert.equal(out.rounds, 2)
  assert.equal(out.result, 'final answer')
  assert.equal(out.finished_at, 3000)
})

test('advance on timeout records silent peers and still progresses', () => {
  const s = swarm.createSession({ goal: 'g', participants: ['hermes', 'openclaw'], rounds: 2 })
  swarm.recordContribution(s, 'hermes', 1, 'only me')
  assert.equal(swarm.advance(s), 'round')
  assert.deepEqual(s.rounds[0].silent, ['openclaw'])
  assert.deepEqual(swarm.previousContributions(s), { hermes: 'only me' })
})

test('advance with zero round-1 contributions fails the session', () => {
  const s = swarm.createSession({ goal: 'g', participants: ['hermes'], rounds: 2 })
  assert.equal(swarm.advance(s), 'failed')
  assert.equal(s.status, 'failed')
  assert.match(s.error, /no swarm peer/)
  assert.equal(swarm.advance(s), 'failed')
})

test('finalize with empty text falls back to a transcript digest', () => {
  const s = swarm.createSession({ goal: 'g', participants: ['openclaw'], rounds: 1 })
  swarm.recordContribution(s, 'openclaw', 1, 'do the thing')
  swarm.advance(s)
  swarm.finalize(s, '', 'nobody')
  assert.equal(s.synthesizedBy, 'gateway')
  assert.match(s.result, /do the thing/)
  assert.match(s.result, /round 1 · openclaw/)
})

test('non-participant contributions are rejected', () => {
  const s = swarm.createSession({ goal: 'g', participants: ['hermes'], rounds: 1 })
  assert.equal(swarm.recordContribution(s, 'openclaw', 1, 'x').reason, 'not_participant')
})

test('fail marks a running session failed once', () => {
  const s = swarm.createSession({ goal: 'g', participants: ['hermes'], rounds: 1 })
  assert.equal(swarm.fail(s, 'synthesizer vanished'), true)
  assert.equal(s.error, 'synthesizer vanished')
  assert.equal(swarm.fail(s, 'again'), false)
})

test('prune evicts oldest finished sessions first', () => {
  const sessions = new Map()
  for (let i = 0; i < 6; i++) {
    const s = swarm.createSession({ goal: `g${i}`, participants: ['hermes'], rounds: 1, now: i })
    if (i < 4) swarm.finalize(s, 'r', 'hermes', 100 + i)
    sessions.set(s.id, s)
  }
  const removed = swarm.prune(sessions, 3)
  assert.equal(removed, 3)
  assert.equal(sessions.size, 3)
  const goals = [...sessions.values()].map((s) => s.goal).sort()
  assert.deepEqual(goals, ['g3', 'g4', 'g5'])
})
