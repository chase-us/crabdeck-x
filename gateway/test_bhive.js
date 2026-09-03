const test = require('node:test')
const assert = require('node:assert/strict')
const bhive = require('./bhive')

test('minuteSlot is floor(unix/60)', () => {
  assert.equal(bhive.minuteSlot(0), 0)
  assert.equal(bhive.minuteSlot(59_999), 0)
  assert.equal(bhive.minuteSlot(60_000), 1)
})

test('minuteSlot rejects bad input', () => {
  assert.throws(() => bhive.minuteSlot(-1))
  assert.throws(() => bhive.minuteSlot('now'))
})

test('watchdog trips after 20s', () => {
  assert.equal(bhive.missedWatchdog(0, 20_000), false)
  assert.equal(bhive.missedWatchdog(0, 20_001), true)
})

test('ingestHeartbeat rejects blank agent without throwing', async () => {
  const { ingestHeartbeat } = require('./vault_client')
  assert.equal(await ingestHeartbeat({ agent: '  ' }), null)
  assert.equal(await ingestHeartbeat({ agent: 12 }), null)
})

test('vault RAG helpers validate input without touching the network', async () => {
  const { queryMemory, ingestMemory, upsertSession } = require('./vault_client')
  assert.deepEqual(await queryMemory('   '), [])
  assert.deepEqual(await queryMemory(42), [])
  assert.equal(await ingestMemory({ agent: 'crabdeck', kind: '', text: 'x' }), null)
  assert.equal(await ingestMemory({ agent: 'crabdeck', kind: 'swarm_result', text: '  ' }), null)
  assert.equal(await upsertSession('', {}), null)
  assert.equal(await upsertSession('s1', ['not', 'a', 'dict']), null)
})
