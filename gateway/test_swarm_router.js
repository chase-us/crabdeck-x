const { describe, it } = require('node:test')
const assert = require('node:assert/strict')
const express = require('express')
const http = require('http')
const { createSwarmRouter } = require('./swarm-router')

function listen(app) {
  return new Promise((resolve) => {
    const server = http.createServer(app)
    server.listen(0, () => {
      const { port } = server.address()
      resolve({ server, port, base: `http://127.0.0.1:${port}` })
    })
  })
}

describe('swarm-router', () => {
  it('rejects empty goal', async () => {
    const sent = []
    const { router } = createSwarmRouter({
      agentStatus: {},
      sendToAgent: (role, msg) => { sent.push({ role, msg }); return true },
      meshOnlineStatus: () => ({ swarm: 'running', hermes: 'running' }),
    })
    const app = express()
    app.use(express.json())
    app.use('/api/swarm', router)
    const { server, base } = await listen(app)
    try {
      const res = await fetch(`${base}/api/swarm/dispatch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal: '   ' }),
      })
      assert.equal(res.status, 400)
      const body = await res.json()
      assert.equal(body.error, 'EMPTY_GOAL')
    } finally {
      server.close()
    }
  })

  it('returns 503 when swarm offline', async () => {
    const { router } = createSwarmRouter({
      agentStatus: {},
      sendToAgent: () => true,
      meshOnlineStatus: () => ({ swarm: 'offline' }),
    })
    const app = express()
    app.use(express.json())
    app.use('/api/swarm', router)
    const { server, base } = await listen(app)
    try {
      const res = await fetch(`${base}/api/swarm/dispatch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal: 'test', async: true }),
      })
      assert.equal(res.status, 503)
      const body = await res.json()
      assert.equal(body.error, 'SWARM_OFFLINE')
    } finally {
      server.close()
    }
  })

  it('async dispatch returns 202', async () => {
    const { router } = createSwarmRouter({
      agentStatus: {},
      sendToAgent: () => true,
      meshOnlineStatus: () => ({ swarm: 'running' }),
    })
    const app = express()
    app.use(express.json())
    app.use('/api/swarm', router)
    const { server, base } = await listen(app)
    try {
      const res = await fetch(`${base}/api/swarm/dispatch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal: 'parallel multitask', async: true }),
      })
      assert.equal(res.status, 202)
      const body = await res.json()
      assert.equal(body.status, 'accepted')
      assert.ok(body.task_id)
    } finally {
      server.close()
    }
  })
})
