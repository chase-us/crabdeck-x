const test = require('node:test')
const assert = require('node:assert/strict')
const { isSwarmType, createMeshRouter, SWARM_TYPES } = require('./swarm')

test('isSwarmType recognizes mesh messages', () => {
  assert.equal(isSwarmType('SWARM_GOAL'), true)
  assert.equal(isSwarmType('SWARM_DELEGATE'), true)
  assert.equal(isSwarmType('PING'), false)
})

test('SWARM_TYPES includes all mesh message kinds', () => {
  assert.ok(SWARM_TYPES.has('SWARM_RESULT'))
  assert.ok(SWARM_TYPES.has('SWARM_PEER_QUERY'))
})

test('mesh router handles SWARM_MESH_STATUS', () => {
  const clients = new Map()
  const agentStatus = { hermes: 'offline', openclaw: 'offline', swarm: 'offline' }
  const sent = []
  const ws = { send: (raw) => sent.push(JSON.parse(raw)) }
  const client = { id: 'c1', role: 'ui', authed: true }
  const mesh = createMeshRouter(clients, agentStatus, () => {}, () => {})
  const handled = mesh.handleSwarmMessage(client, ws, { type: 'SWARM_MESH_STATUS' })
  assert.equal(handled, true)
  assert.equal(sent[0].type, 'SWARM_MESH_STATUS')
  assert.ok(Array.isArray(sent[0].agents))
})

test('mesh router rejects bad delegate target', () => {
  const clients = new Map()
  const agentStatus = {}
  const sent = []
  const ws = { send: (raw) => sent.push(JSON.parse(raw)) }
  const client = { id: 'c1', role: 'swarm', authed: true }
  const mesh = createMeshRouter(clients, agentStatus, () => {}, () => {})
  const handled = mesh.handleSwarmMessage(client, ws, {
    type: 'SWARM_DELEGATE',
    target: 'unknown-agent',
    task_id: 't1',
  })
  assert.equal(handled, true)
  assert.equal(sent[0].code, 'BAD_TARGET')
})
