'use strict'

const { test } = require('node:test')
const assert = require('node:assert/strict')
const { gatewayBindConfig, parsePort, parseOriginPorts } = require('./bind')

test('defaults bind to all interfaces on 8765 (Cloudflare-reachable)', () => {
  const cfg = gatewayBindConfig({})
  assert.deepEqual(cfg, {
    port: 8765,
    host: '0.0.0.0',
    originPorts: [],
    listeners: ['0.0.0.0:8765'],
  })
})

test('HOST and PORT override the default bind', () => {
  const cfg = gatewayBindConfig({ HOST: '127.0.0.1', PORT: '9000' })
  assert.equal(cfg.host, '127.0.0.1')
  assert.equal(cfg.port, 9000)
  assert.deepEqual(cfg.listeners, ['127.0.0.1:9000'])
})

test('ORIGIN_PORT adds extra public listeners and de-dupes PORT', () => {
  const cfg = gatewayBindConfig({ PORT: '8765', ORIGIN_PORT: '80,443,8765' })
  assert.deepEqual(cfg.originPorts, [80, 443])
  assert.deepEqual(cfg.listeners, ['0.0.0.0:8765', '0.0.0.0:80', '0.0.0.0:443'])
})

test('rejects invalid PORT', () => {
  assert.throws(() => parsePort('nope', 8765), /Invalid port/)
  assert.throws(() => gatewayBindConfig({ PORT: '70000' }), /Invalid port/)
})

test('rejects invalid ORIGIN_PORT entries', () => {
  assert.throws(() => parseOriginPorts('80,xyz'), /Invalid ORIGIN_PORT/)
})

test('gatewayBindConfig requires an env object', () => {
  assert.throws(() => gatewayBindConfig(null), /env must be an object/)
})

test('live server binds HOST and answers /health', async () => {
  const { spawn } = require('node:child_process')
  const path = require('node:path')
  const net = require('node:net')

  const port = await new Promise((resolve, reject) => {
    const probe = net.createServer()
    probe.listen(0, '127.0.0.1', () => {
      const addr = probe.address()
      const p = typeof addr === 'object' && addr ? addr.port : 0
      probe.close(() => resolve(p))
    })
    probe.on('error', reject)
  })

  const child = spawn(process.execPath, [path.join(__dirname, 'server.js')], {
    env: { ...process.env, HOST: '127.0.0.1', PORT: String(port) },
    stdio: ['ignore', 'pipe', 'pipe'],
  })

  try {
    let body = ''
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, 50))
      try {
        const res = await fetch(`http://127.0.0.1:${port}/health`)
        if (res.ok) {
          body = await res.text()
          break
        }
      } catch {
        // still binding
      }
    }
    assert.ok(body, 'health endpoint never became reachable')
    const payload = JSON.parse(body)
    assert.equal(payload.status, 'ok')
    assert.equal(payload.bind.host, '127.0.0.1')
    assert.equal(payload.bind.port, port)
    assert.ok(Array.isArray(payload.agentStatus ? Object.keys(payload.agentStatus) : []))
  } finally {
    child.kill('SIGTERM')
    await new Promise((r) => child.once('exit', r))
  }
})
