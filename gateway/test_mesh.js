const test = require('node:test')
const assert = require('node:assert/strict')
const mesh = require('./mesh')

// ── Capability handling ──────────────────────────────────────────────────────

test('normalizeCapabilities lowercases, dedupes, and caps', () => {
  assert.deepEqual(mesh.normalizeCapabilities([' Reasoning ', 'reasoning', 'RAG']), ['reasoning', 'rag'])
  assert.equal(mesh.normalizeCapabilities(Array(50).fill().map((_, i) => `cap${i}`)).length, mesh.MAX_CAPABILITIES)
})

test('normalizeCapabilities rejects hostile input', () => {
  assert.deepEqual(mesh.normalizeCapabilities('reasoning'), [])
  assert.deepEqual(mesh.normalizeCapabilities([null, 7, {}, '', '  ']), [])
  assert.deepEqual(mesh.normalizeCapabilities(['../etc/passwd', 'drop table', 'ok_cap']), ['ok_cap'])
  assert.deepEqual(mesh.normalizeCapabilities(['x'.repeat(64)]), [])
})

test('capabilityMatch reports coverage', () => {
  const match = mesh.capabilityMatch(['reasoning', 'llm'], ['reasoning', 'system'])
  assert.deepEqual(match.covered, ['reasoning'])
  assert.deepEqual(match.missing, ['system'])
  assert.equal(match.ratio, 0.5)
})

test('an empty requirement is an open call every peer can answer', () => {
  assert.equal(mesh.capabilityMatch(['anything'], []).ratio, 1)
})

// ── Contract Net scoring ─────────────────────────────────────────────────────

test('scoreBid rewards confidence and punishes load and cost', () => {
  const peer = { capabilities: ['reasoning'], load: 0 }
  const cheap = mesh.scoreBid({ confidence: 0.9, cost: 0 }, peer, ['reasoning'])
  const pricey = mesh.scoreBid({ confidence: 0.9, cost: 4 }, peer, ['reasoning'])
  const busy = mesh.scoreBid({ confidence: 0.9, cost: 0 }, { ...peer, load: 5 }, ['reasoning'])
  assert.ok(cheap > pricey)
  assert.ok(cheap > busy)
})

test('scoreBid zeroes a peer that cannot cover any required capability', () => {
  const score = mesh.scoreBid({ confidence: 1 }, { capabilities: ['cooking'], load: 0 }, ['system'])
  assert.equal(score, 0)
})

test('scoreBid cannot be gamed by claiming confidence alone', () => {
  const required = ['retrieval']
  const liar = mesh.scoreBid({ confidence: 1, capabilities: ['retrieval', 'other'] }, { capabilities: ['retrieval'], load: 9 }, required)
  const honest = mesh.scoreBid({ confidence: 0.6, capabilities: ['retrieval'] }, { capabilities: ['retrieval'], load: 0 }, required)
  assert.ok(honest > liar, 'an idle honest peer must beat an overloaded braggart')
})

test('scoreBid clamps out-of-range confidence', () => {
  const peer = { capabilities: ['a'], load: 0 }
  assert.equal(
    mesh.scoreBid({ confidence: 99 }, peer, ['a']),
    mesh.scoreBid({ confidence: 1 }, peer, ['a'])
  )
  assert.equal(mesh.scoreBid({ confidence: -5 }, peer, ['a']), 0)
})

test('selectWinner takes the max and breaks ties on name', () => {
  assert.equal(mesh.selectWinner([
    { peer: 'zeta', score: 0.5 },
    { peer: 'alpha', score: 0.5 },
    { peer: 'mid', score: 0.4 },
  ]).peer, 'alpha')
})

test('selectWinner returns null when nothing is eligible', () => {
  assert.equal(mesh.selectWinner([]), null)
  assert.equal(mesh.selectWinner([{ peer: 'a', score: 0 }]), null)
  assert.equal(mesh.selectWinner(null), null)
})

// ── Stigmergy ────────────────────────────────────────────────────────────────

test('reinforce deposits on success and deducts on failure', () => {
  assert.ok(mesh.reinforce(1, 'success') > 1)
  assert.ok(mesh.reinforce(1, 'failure') < 1)
  assert.equal(mesh.reinforce(1, 'unknown'), 1)
})

test('trails stay inside bounds so no peer becomes absorbing', () => {
  let trail = mesh.TRAIL_INIT
  for (let i = 0; i < 100; i += 1) trail = mesh.reinforce(trail, 'success')
  assert.equal(trail, mesh.TRAIL_MAX)
  for (let i = 0; i < 200; i += 1) trail = mesh.reinforce(trail, 'failure')
  assert.equal(trail, mesh.TRAIL_MIN)
})

test('evaporate pulls trails back toward neutral', () => {
  assert.ok(mesh.evaporate(4) < 4)
  assert.ok(mesh.evaporate(0.1) > 0.1)
  assert.equal(mesh.evaporate(mesh.TRAIL_INIT), mesh.TRAIL_INIT)
})

test('evaporation eventually erases a stale win', () => {
  let trail = mesh.TRAIL_MAX
  for (let i = 0; i < 500; i += 1) trail = mesh.evaporate(trail)
  assert.ok(Math.abs(trail - mesh.TRAIL_INIT) < 0.01)
})

// ── Gossip ───────────────────────────────────────────────────────────────────

test('gossipTargets excludes the sender and respects fanout', () => {
  const targets = mesh.gossipTargets(['a', 'b', 'c', 'd'], 'a', 2)
  assert.equal(targets.length, 2)
  assert.ok(!targets.includes('a'))
})

test('gossipTargets rotates by sender so the whole mesh is reachable', () => {
  const peers = ['a', 'b', 'c', 'd', 'e']
  const reached = new Set()
  for (const from of peers) mesh.gossipTargets(peers, from, 2).forEach(t => reached.add(t))
  assert.equal(reached.size, peers.length)
})

test('gossipTargets copes with a lonely or empty mesh', () => {
  assert.deepEqual(mesh.gossipTargets(['a'], 'a', 3), [])
  assert.deepEqual(mesh.gossipTargets([], 'a'), [])
  assert.deepEqual(mesh.gossipTargets(null, 'a'), [])
})

test('clampTtl bounds hop counts', () => {
  assert.equal(mesh.clampTtl(99), mesh.TTL_MAX)
  assert.equal(mesh.clampTtl(-3), 0)
  assert.equal(mesh.clampTtl('lots'), mesh.TTL_MAX)
})

test('SeenSet dedupes and stays bounded', () => {
  const seen = new mesh.SeenSet(3)
  assert.equal(seen.add('g1'), true)
  assert.equal(seen.add('g1'), false, 'a repeat gossip id must not re-flood')
  seen.add('g2'); seen.add('g3'); seen.add('g4')
  assert.equal(seen.size, 3)
  assert.equal(seen.add('g1'), true, 'oldest id was evicted')
})

// ── Quorum consensus ─────────────────────────────────────────────────────────

test('quorum picks the cluster two peers independently agree on', () => {
  const agreed = mesh.quorum([
    { peer: 'hermes', result: 'the gateway watchdog trips after 20 seconds of silence', confidence: 0.7 },
    { peer: 'scribe', result: 'watchdog trips after 20 seconds of agent silence', confidence: 0.8 },
    { peer: 'openclaw', result: 'disk usage on the host is 41 percent', confidence: 0.9 },
  ])
  assert.equal(agreed.votes, 2)
  assert.equal(agreed.total, 3)
  assert.ok(agreed.confident)
  assert.deepEqual(agreed.peers.sort(), ['hermes', 'scribe'])
  assert.ok(agreed.answer.includes('20 seconds'))
})

test('quorum reports low agreement when every peer disagrees', () => {
  const agreed = mesh.quorum([
    { peer: 'a', result: 'alpha alpha alpha' },
    { peer: 'b', result: 'bravo bravo bravo' },
    { peer: 'c', result: 'charlie charlie charlie' },
  ])
  assert.equal(agreed.votes, 1)
  assert.equal(agreed.confident, false, 'a 1-of-3 split must not be presented as consensus')
  assert.ok(Math.abs(agreed.agreement - 1 / 3) < 1e-9)
})

test('quorum lets the most confident member speak for its cluster', () => {
  const agreed = mesh.quorum([
    { peer: 'a', result: 'mesh awarded task one to openclaw', confidence: 0.4 },
    { peer: 'b', result: 'mesh awarded task one to openclaw agent', confidence: 0.95 },
  ])
  assert.equal(agreed.answer, 'mesh awarded task one to openclaw agent')
})

test('quorum ignores empty and malformed results', () => {
  const agreed = mesh.quorum([null, { peer: 'a', result: '   ' }, { peer: 'b' }, 7])
  assert.equal(agreed.votes, 0)
  assert.equal(agreed.answer, null)
  assert.equal(agreed.confident, false)
})

test('a single result is never sold as consensus', () => {
  const agreed = mesh.quorum([{ peer: 'a', result: 'only answer', confidence: 1 }])
  assert.equal(agreed.agreement, 1)
  assert.equal(agreed.confident, false, 'minVotes=2 guards against a lone voice')
})

// ── Registry ─────────────────────────────────────────────────────────────────

function registry() {
  let clock = 1_000
  const reg = new mesh.MeshRegistry({ now: () => clock })
  return { reg, advance: ms => { clock += ms } }
}

test('peers join with normalized capabilities and a neutral trail', () => {
  const { reg } = registry()
  const peer = reg.join('Hermes', { capabilities: ['Reasoning', 'llm'], clientId: 'c1' })
  assert.equal(peer.name, 'hermes')
  assert.deepEqual(peer.capabilities, ['reasoning', 'llm'])
  assert.equal(reg.trail('hermes'), mesh.TRAIL_INIT)
})

test('join rejects unusable peer names', () => {
  const { reg } = registry()
  assert.equal(reg.join(''), null)
  assert.equal(reg.join('   '), null)
  assert.equal(reg.join('../evil'), null)
  assert.equal(reg.join(42), null)
})

test('capablePeers filters on capability and excludes the announcer', () => {
  const { reg } = registry()
  reg.join('hermes', { capabilities: ['reasoning'] })
  reg.join('openclaw', { capabilities: ['system'] })
  reg.join('scribe', { capabilities: ['retrieval'] })
  assert.deepEqual(reg.capablePeers(['retrieval']).map(p => p.name), ['scribe'])
  assert.deepEqual(
    reg.capablePeers([], { exclude: 'hermes' }).map(p => p.name).sort(),
    ['openclaw', 'scribe']
  )
})

test('a full contract-net round awards the strongest bid', () => {
  const { reg } = registry()
  reg.join('hermes', { capabilities: ['reasoning'], clientId: 'c1' })
  reg.join('scribe', { capabilities: ['reasoning', 'retrieval'], clientId: 'c2' })
  reg.openContract('task-1', {
    task: 'summarize the mesh protocol',
    capabilities: ['reasoning'],
    announcer: 'ui',
    invited: ['hermes', 'scribe'],
  })
  reg.addBid('task-1', 'hermes', { confidence: 0.5 })
  reg.addBid('task-1', 'scribe', { confidence: 0.9 })
  const winner = reg.award('task-1')
  assert.equal(winner.peer, 'scribe')
  assert.equal(reg.contract('task-1').state, 'awarded')
  assert.equal(reg.peer('scribe').awarded, 1)
})

test('one bid per peer per contract', () => {
  const { reg } = registry()
  reg.join('hermes', { capabilities: ['reasoning'] })
  reg.openContract('t', { task: 'x', capabilities: ['reasoning'], announcer: 'ui', invited: ['hermes'] })
  assert.ok(reg.addBid('t', 'hermes', { confidence: 0.2 }))
  assert.equal(reg.addBid('t', 'hermes', { confidence: 0.9 }), null)
})

test('bids from unknown peers and closed contracts are refused', () => {
  const { reg } = registry()
  reg.join('hermes', { capabilities: ['reasoning'] })
  reg.openContract('t', { task: 'x', capabilities: [], announcer: 'ui', invited: ['hermes'] })
  assert.equal(reg.addBid('t', 'ghost', { confidence: 1 }), null)
  assert.equal(reg.addBid('missing', 'hermes', { confidence: 1 }), null)
  reg.award('t')
  assert.equal(reg.addBid('t', 'hermes', { confidence: 1 }), null)
})

test('a contract with no bids is marked unawarded, not awarded', () => {
  const { reg } = registry()
  reg.openContract('t', { task: 'x', capabilities: [], announcer: 'ui', invited: [] })
  assert.equal(reg.award('t'), null)
  assert.equal(reg.contract('t').state, 'unawarded')
})

test('award is idempotent', () => {
  const { reg } = registry()
  reg.join('hermes', { capabilities: ['reasoning'] })
  reg.openContract('t', { task: 'x', capabilities: [], announcer: 'ui', invited: ['hermes'] })
  reg.addBid('t', 'hermes', { confidence: 0.8 })
  assert.equal(reg.award('t').peer, 'hermes')
  assert.equal(reg.award('t'), null)
  assert.equal(reg.peer('hermes').awarded, 1)
})

test('results move the trail and completion follows the quorum target', () => {
  const { reg } = registry()
  reg.join('hermes', { capabilities: ['reasoning'] })
  reg.join('scribe', { capabilities: ['reasoning'] })
  reg.openContract('t', {
    task: 'x', capabilities: ['reasoning'], announcer: 'ui',
    invited: ['hermes', 'scribe'], quorumTarget: 2,
  })
  reg.recordResult('t', 'hermes', { result: 'watchdog trips at 20 seconds', confidence: 0.7 })
  assert.equal(reg.contract('t').state, 'open')
  assert.ok(reg.trail('hermes') > mesh.TRAIL_INIT)

  reg.recordResult('t', 'scribe', { result: 'the watchdog trips after 20 seconds', confidence: 0.8 })
  assert.equal(reg.contract('t').state, 'complete')
  const agreed = reg.consensus('t')
  assert.equal(agreed.votes, 2)
  assert.ok(agreed.confident)
})

test('a failed result costs trail', () => {
  const { reg } = registry()
  reg.join('openclaw', { capabilities: ['system'] })
  reg.openContract('t', { task: 'x', capabilities: [], announcer: 'ui', invited: ['openclaw'] })
  reg.recordResult('t', 'openclaw', { result: 'ollama unreachable', ok: false })
  assert.ok(reg.trail('openclaw') < mesh.TRAIL_INIT)
})

test('a proven peer outbids a fresh one at equal confidence', () => {
  const { reg } = registry()
  reg.join('hermes', { capabilities: ['reasoning'] })
  reg.join('scribe', { capabilities: ['reasoning'] })
  // scribe earns trail on an earlier contract
  reg.openContract('past', { task: 'x', capabilities: ['reasoning'], announcer: 'ui', invited: ['scribe'] })
  reg.recordResult('past', 'scribe', { result: 'done', ok: true })

  reg.openContract('now', { task: 'y', capabilities: ['reasoning'], announcer: 'ui', invited: ['hermes', 'scribe'] })
  reg.addBid('now', 'hermes', { confidence: 0.7 })
  reg.addBid('now', 'scribe', { confidence: 0.7 })
  assert.equal(reg.award('now').peer, 'scribe', 'stigmergy must break the tie toward the proven peer')
})

test('trail survives a reconnect but the socket binding does not', () => {
  const { reg } = registry()
  reg.join('scribe', { capabilities: ['retrieval'], clientId: 'sock-1' })
  reg.openContract('t', { task: 'x', capabilities: [], announcer: 'ui', invited: ['scribe'] })
  reg.recordResult('t', 'scribe', { result: 'ok', ok: true })
  const earned = reg.trail('scribe')

  reg.leave('scribe')
  assert.equal(reg.peer('scribe'), null)
  reg.join('scribe', { capabilities: ['retrieval'], clientId: 'sock-2' })
  assert.equal(reg.trail('scribe'), earned)
  assert.equal(reg.peer('scribe').clientId, 'sock-2')
})

test('touch records queue depth for bid scoring', () => {
  const { reg } = registry()
  reg.join('hermes', { capabilities: ['reasoning'] })
  assert.equal(reg.touch('hermes', 3).load, 3)
  assert.equal(reg.touch('hermes', -1).load, 3, 'a negative load is ignored')
  assert.equal(reg.touch('ghost', 1), null)
})

test('contract text and results are clamped', () => {
  const { reg } = registry()
  reg.join('hermes', { capabilities: ['reasoning'] })
  const contract = reg.openContract('t', { task: 'x'.repeat(99_999), capabilities: [], announcer: 'ui', invited: ['hermes'] })
  assert.equal(contract.task.length, mesh.MAX_TASK_LEN)
  const entry = reg.recordResult('t', 'hermes', { result: 'y'.repeat(99_999) })
  assert.equal(entry.result.length, mesh.MAX_RESULT_LEN)
})

test('the contract map is bounded by evicting the oldest', () => {
  let clock = 0
  const reg = new mesh.MeshRegistry({ now: () => (clock += 1), maxContracts: 3 })
  for (let i = 0; i < 5; i += 1) reg.openContract(`t${i}`, { task: 'x', announcer: 'ui' })
  assert.ok(reg.contracts().length <= 3)
  assert.equal(reg.contract('t0'), null)
  assert.ok(reg.contract('t4'))
})

test('sweep clears stale contracts', () => {
  const { reg, advance } = registry()
  reg.openContract('old', { task: 'x', announcer: 'ui' })
  advance(400_000)
  reg.openContract('fresh', { task: 'y', announcer: 'ui' })
  assert.equal(reg.sweep(), 1)
  assert.equal(reg.contract('old'), null)
  assert.ok(reg.contract('fresh'))
})

test('tick evaporates every trail at once', () => {
  const { reg } = registry()
  reg.join('hermes', { capabilities: ['reasoning'] })
  reg.openContract('t', { task: 'x', announcer: 'ui', invited: ['hermes'] })
  reg.recordResult('t', 'hermes', { result: 'ok', ok: true })
  const before = reg.trail('hermes')
  reg.tick()
  assert.ok(reg.trail('hermes') < before)
})

test('observeGossip is true once per id', () => {
  const { reg } = registry()
  assert.equal(reg.observeGossip('g1'), true)
  assert.equal(reg.observeGossip('g1'), false)
})

test('snapshot is JSON-serializable for /mesh', () => {
  const { reg } = registry()
  reg.join('hermes', { capabilities: ['reasoning'], clientId: 'c1' })
  reg.openContract('t', { task: 'x', capabilities: ['reasoning'], announcer: 'ui', invited: ['hermes'] })
  reg.addBid('t', 'hermes', { confidence: 0.6 })
  const snap = JSON.parse(JSON.stringify(reg.snapshot()))
  assert.equal(snap.peers[0].name, 'hermes')
  assert.equal(snap.contracts[0].taskId, 't')
  assert.equal(snap.contracts[0].bids[0].peer, 'hermes')
  assert.ok(Number.isFinite(snap.trails.hermes))
})
