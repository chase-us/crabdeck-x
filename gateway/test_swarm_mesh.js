const test = require('node:test')
const assert = require('node:assert/strict')
const { SwarmMesh, normalizeTask } = require('./swarm_mesh')

test('creates a task for unique active agents', () => {
  const mesh = new SwarmMesh()
  const task = mesh.create({ task: 'Review deployment safety', model: 'llama3' }, ['hermes', 'openclaw', 'hermes'])

  assert.equal(task.participants.length, 2)
  assert.equal(task.task, 'Review deployment safety')
})

test('requires two collaborating agents', () => {
  const mesh = new SwarmMesh()
  assert.throws(() => mesh.create({ task: 'Only one' }, ['hermes']), /at least two/)
})

test('collects one result per assigned agent', () => {
  const mesh = new SwarmMesh()
  const task = mesh.create({ task: 'Compare options' }, ['hermes', 'openclaw'])

  const first = mesh.submit('hermes', { taskId: task.taskId, result: 'LLM analysis' })
  assert.equal(first.complete, false)
  assert.deepEqual(first.pending, ['openclaw'])

  const second = mesh.submit('openclaw', { taskId: task.taskId, result: 'System analysis' })
  assert.equal(second.complete, true)
  assert.deepEqual(second.results, { hermes: 'LLM analysis', openclaw: 'System analysis' })
  assert.throws(() => mesh.submit('openclaw', { taskId: task.taskId, result: 'Duplicate' }), /already submitted/)
})

test('rejects malformed swarm tasks', () => {
  assert.throws(() => normalizeTask({ task: '  ' }), /non-empty/)
  assert.throws(() => normalizeTask('not-an-object'), /must be an object/)
})
