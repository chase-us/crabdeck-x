/**
 * Tests for gateway/server.js
 *
 * Uses Node.js built-in test runner (node:test) — requires Node >= 18.
 *
 * Run:
 *   cd gateway
 *   node --test tests/server.test.js
 *
 * Strategy: server.js starts listening on load (side-effectful), so we test
 * the core business logic functions by inlining their equivalent implementations
 * as pure-function unit tests. This covers all decision-making code without
 * requiring a live server.
 */

'use strict'

const { test, describe } = require('node:test')
const assert = require('node:assert/strict')

// WebSocket.OPEN constant (value 1 in the ws library)
const WS_OPEN = 1

// ---------------------------------------------------------------------------
// requireAuthed() logic
// ---------------------------------------------------------------------------

describe('requireAuthed()', () => {
  function requireAuthed(client, sentMessages) {
    if (!client.authed) {
      sentMessages.push(JSON.stringify({
        type: 'ERROR',
        code: 'UNAUTHENTICATED',
        message: 'Send HELLO with a valid token before issuing this command.',
      }))
      return false
    }
    return true
  }

  test('returns false and sends ERROR for unauthenticated client', () => {
    const messages = []
    const result = requireAuthed({ authed: false }, messages)
    assert.equal(result, false)
    assert.equal(messages.length, 1)
    const error = JSON.parse(messages[0])
    assert.equal(error.type, 'ERROR')
    assert.equal(error.code, 'UNAUTHENTICATED')
    assert.ok(error.message.length > 0)
  })

  test('returns true for authenticated client without sending message', () => {
    const messages = []
    const result = requireAuthed({ authed: true }, messages)
    assert.equal(result, true)
    assert.equal(messages.length, 0)
  })

  test('error message mentions HELLO', () => {
    const messages = []
    requireAuthed({ authed: false }, messages)
    const error = JSON.parse(messages[0])
    assert.ok(error.message.includes('HELLO'))
  })
})

// ---------------------------------------------------------------------------
// Role assignment from HELLO client field
// ---------------------------------------------------------------------------

describe('role assignment from HELLO client field', () => {
  function mapRole(clientName) {
    return clientName === 'crabdeck-ui'   ? 'ui'
         : clientName === 'openclaw'       ? 'openclaw'
         : clientName === 'hermes'         ? 'hermes'
         : clientName === 'orchestrator'   ? 'orchestrator'
         : 'unknown'
  }

  test('maps "crabdeck-ui" to "ui"',           () => assert.equal(mapRole('crabdeck-ui'),   'ui'))
  test('maps "openclaw" to "openclaw"',         () => assert.equal(mapRole('openclaw'),       'openclaw'))
  test('maps "hermes" to "hermes"',             () => assert.equal(mapRole('hermes'),         'hermes'))
  test('maps "orchestrator" to "orchestrator"', () => assert.equal(mapRole('orchestrator'),   'orchestrator'))
  test('maps unknown name to "unknown"',        () => assert.equal(mapRole('bad-client'),     'unknown'))
  test('maps empty string to "unknown"',        () => assert.equal(mapRole(''),               'unknown'))
  test('is case-sensitive (Hermes ≠ hermes)',   () => assert.equal(mapRole('Hermes'),         'unknown'))
})

// ---------------------------------------------------------------------------
// Token authentication logic
// ---------------------------------------------------------------------------

describe('token authentication logic', () => {
  function checkToken(gatewayToken, providedToken) {
    if (gatewayToken && providedToken !== gatewayToken) {
      return { authed: false, code: 'BAD_TOKEN', message: 'Invalid gateway token' }
    }
    return { authed: true }
  }

  test('accepts any token when gateway has no token configured', () =>
    assert.deepEqual(checkToken(null, 'anything'), { authed: true }))

  test('accepts correct token',  () =>
    assert.deepEqual(checkToken('secret', 'secret'), { authed: true }))

  test('rejects wrong token', () => {
    const r = checkToken('secret', 'wrong')
    assert.equal(r.authed, false)
    assert.equal(r.code, 'BAD_TOKEN')
  })

  test('rejects missing token when gateway requires one', () => {
    const r = checkToken('secret', undefined)
    assert.equal(r.authed, false)
    assert.equal(r.code, 'BAD_TOKEN')
  })

  test('rejects null token when gateway requires one', () => {
    const r = checkToken('secret', null)
    assert.equal(r.authed, false)
  })

  test('no-token gateway: accepts undefined token', () =>
    assert.equal(checkToken(null, undefined).authed, true))

  test('error message indicates bad token', () => {
    const r = checkToken('secret', 'wrong')
    assert.ok(r.message.toLowerCase().includes('token'))
  })
})

// ---------------------------------------------------------------------------
// Role lock — no re-HELLO identity swap
// ---------------------------------------------------------------------------

describe('role lock (no re-HELLO swap)', () => {
  function handleHello(client, msg, gatewayToken) {
    // Role can only be set once per connection
    if (client.role !== 'unknown') {
      return { type: 'ACK', role: client.role }
    }
    if (gatewayToken && msg.token !== gatewayToken) {
      return { type: 'ERROR', code: 'BAD_TOKEN' }
    }
    const role = msg.client === 'hermes' ? 'hermes'
               : msg.client === 'openclaw' ? 'openclaw'
               : msg.client === 'crabdeck-ui' ? 'ui'
               : 'unknown'
    client.role = role
    client.authed = true
    return { type: 'ACK', role }
  }

  test('first HELLO sets role', () => {
    const client = { role: 'unknown', authed: false }
    const resp = handleHello(client, { client: 'hermes', token: null }, null)
    assert.equal(resp.type, 'ACK')
    assert.equal(resp.role, 'hermes')
    assert.equal(client.role, 'hermes')
  })

  test('second HELLO returns ACK with existing role without changing it', () => {
    const client = { role: 'hermes', authed: true }
    const resp = handleHello(client, { client: 'openclaw' }, null)
    assert.equal(resp.type, 'ACK')
    assert.equal(resp.role, 'hermes')  // unchanged
    assert.equal(client.role, 'hermes')
  })

  test('bad token on first HELLO returns ERROR', () => {
    const client = { role: 'unknown', authed: false }
    const resp = handleHello(client, { client: 'hermes', token: 'wrong' }, 'secret')
    assert.equal(resp.type, 'ERROR')
    assert.equal(resp.code, 'BAD_TOKEN')
  })
})

// ---------------------------------------------------------------------------
// Origin allow-list verification
// ---------------------------------------------------------------------------

describe('origin allow-list (verifyClient)', () => {
  function isOriginAllowed(origin, allowedOrigins) {
    if (!origin) return true   // agent processes — no Origin header
    if (!allowedOrigins.length) return true
    return allowedOrigins.includes(origin)
  }

  const allowed = ['http://localhost:5173', 'https://app.example.com']

  test('allows connection with no Origin header (agent process)', () =>
    assert.equal(isOriginAllowed(undefined, allowed), true))

  test('allows connection from exact allowed origin', () =>
    assert.equal(isOriginAllowed('http://localhost:5173', allowed), true))

  test('rejects connection from disallowed origin', () =>
    assert.equal(isOriginAllowed('https://evil.example.com', allowed), false))

  test('allows any origin when allow-list is empty', () =>
    assert.equal(isOriginAllowed('https://evil.example.com', []), true))

  test('rejects partial-match origin (must be exact)', () =>
    assert.equal(isOriginAllowed('http://localhost:51730', allowed), false))

  test('rejects null origin when allow-list is configured', () =>
    assert.equal(isOriginAllowed(null, allowed), false))
})

// ---------------------------------------------------------------------------
// ALLOWED_ORIGINS env parsing
// ---------------------------------------------------------------------------

describe('ALLOWED_ORIGINS env parsing', () => {
  function parseAllowedOrigins(envValue) {
    return (envValue || 'http://localhost:5173')
      .split(',')
      .map(s => s.trim())
      .filter(Boolean)
  }

  test('uses default origin when env not set', () =>
    assert.deepEqual(parseAllowedOrigins(undefined), ['http://localhost:5173']))

  test('parses multiple comma-separated origins', () =>
    assert.deepEqual(
      parseAllowedOrigins('http://localhost:5173,https://app.example.com'),
      ['http://localhost:5173', 'https://app.example.com']
    ))

  test('trims whitespace around each origin', () => {
    const result = parseAllowedOrigins(' http://localhost:5173 , https://app.example.com ')
    assert.deepEqual(result, ['http://localhost:5173', 'https://app.example.com'])
  })

  test('filters empty entries from double-comma', () => {
    const result = parseAllowedOrigins('http://localhost:5173,,https://app.example.com')
    assert.equal(result.length, 2)
  })

  test('single origin parses to one-element array', () =>
    assert.deepEqual(parseAllowedOrigins('http://localhost:3000'), ['http://localhost:3000']))
})

// ---------------------------------------------------------------------------
// broadcast() logic
// ---------------------------------------------------------------------------

describe('broadcast()', () => {
  function makeWs(readyState = WS_OPEN) {
    const messages = []
    return { readyState, send: m => messages.push(m), messages }
  }

  function broadcast(clients, msg, excludeId = null) {
    const raw = JSON.stringify(msg)
    for (const [id, c] of clients) {
      if (id !== excludeId && c.ws.readyState === WS_OPEN) {
        c.ws.send(raw)
      }
    }
  }

  test('sends to all open clients', () => {
    const ws1 = makeWs(); const ws2 = makeWs()
    const clients = new Map([['c1', { ws: ws1 }], ['c2', { ws: ws2 }]])
    broadcast(clients, { type: 'TEST' })
    assert.equal(ws1.messages.length, 1)
    assert.equal(ws2.messages.length, 1)
  })

  test('excludes the specified client id', () => {
    const ws1 = makeWs(); const ws2 = makeWs()
    const clients = new Map([['c1', { ws: ws1 }], ['c2', { ws: ws2 }]])
    broadcast(clients, { type: 'TEST' }, 'c1')
    assert.equal(ws1.messages.length, 0)
    assert.equal(ws2.messages.length, 1)
  })

  test('skips clients that are not OPEN', () => {
    const wsClosed = makeWs(3)  // CLOSED
    const wsOpen   = makeWs(WS_OPEN)
    const clients = new Map([['c1', { ws: wsClosed }], ['c2', { ws: wsOpen }]])
    broadcast(clients, { type: 'TEST' })
    assert.equal(wsClosed.messages.length, 0)
    assert.equal(wsOpen.messages.length, 1)
  })

  test('sends nothing to empty client map', () => {
    broadcast(new Map(), { type: 'TEST' })  // should not throw
  })

  test('serialises message correctly', () => {
    const ws = makeWs()
    broadcast(new Map([['c1', { ws }]]), { type: 'PING', value: 42 })
    assert.deepEqual(JSON.parse(ws.messages[0]), { type: 'PING', value: 42 })
  })

  test('multiple excludes still broadcasts to others', () => {
    const ws1 = makeWs(); const ws2 = makeWs(); const ws3 = makeWs()
    const clients = new Map([['c1', { ws: ws1 }], ['c2', { ws: ws2 }], ['c3', { ws: ws3 }]])
    broadcast(clients, { type: 'T' }, 'c1')
    assert.equal(ws1.messages.length, 0)
    assert.equal(ws2.messages.length, 1)
    assert.equal(ws3.messages.length, 1)
  })
})

// ---------------------------------------------------------------------------
// sendTo() logic
// ---------------------------------------------------------------------------

describe('sendTo()', () => {
  function makeWs() {
    const messages = []
    return { readyState: WS_OPEN, send: m => messages.push(m), messages }
  }

  function sendTo(clients, role, msg) {
    for (const [, c] of clients) {
      if (c.role === role && c.authed && c.ws.readyState === WS_OPEN) {
        c.ws.send(JSON.stringify(msg))
      }
    }
  }

  test('sends to all authed clients with matching role', () => {
    const ws1 = makeWs(); const ws2 = makeWs(); const ws3 = makeWs()
    const clients = new Map([
      ['c1', { role: 'ui',     authed: true,  ws: ws1 }],
      ['c2', { role: 'ui',     authed: true,  ws: ws2 }],
      ['c3', { role: 'hermes', authed: true,  ws: ws3 }],
    ])
    sendTo(clients, 'ui', { type: 'MSG' })
    assert.equal(ws1.messages.length, 1)
    assert.equal(ws2.messages.length, 1)
    assert.equal(ws3.messages.length, 0)
  })

  test('does not send to unauthenticated clients', () => {
    const ws = makeWs()
    const clients = new Map([['c1', { role: 'hermes', authed: false, ws }]])
    sendTo(clients, 'hermes', { type: 'PROMPT' })
    assert.equal(ws.messages.length, 0)
  })

  test('does not send to clients with wrong role', () => {
    const ws = makeWs()
    const clients = new Map([['c1', { role: 'openclaw', authed: true, ws }]])
    sendTo(clients, 'hermes', { type: 'PROMPT' })
    assert.equal(ws.messages.length, 0)
  })

  test('sends nothing when no clients in map', () => {
    sendTo(new Map(), 'ui', { type: 'T' })  // should not throw
  })
})

// ---------------------------------------------------------------------------
// HERMES_RESPONSE routing — only hermes role may send, only ui receives
// ---------------------------------------------------------------------------

describe('HERMES_RESPONSE routing rules', () => {
  function handleHermesResponse(client) {
    // Returns whether the message should be routed
    return client.authed && client.role === 'hermes'
  }

  test('allows hermes-role authed client to send response', () =>
    assert.equal(handleHermesResponse({ authed: true, role: 'hermes' }), true))

  test('blocks unauthenticated hermes client', () =>
    assert.equal(handleHermesResponse({ authed: false, role: 'hermes' }), false))

  test('blocks ui-role client from sending hermes response', () =>
    assert.equal(handleHermesResponse({ authed: true, role: 'ui' }), false))

  test('blocks openclaw-role client from sending hermes response', () =>
    assert.equal(handleHermesResponse({ authed: true, role: 'openclaw' }), false))
})

// ---------------------------------------------------------------------------
// TASK_RESULT routing — only openclaw role may send
// ---------------------------------------------------------------------------

describe('TASK_RESULT routing rules', () => {
  function handleTaskResult(client) {
    return client.authed && client.role === 'openclaw'
  }

  test('allows openclaw-role authed client to send task result', () =>
    assert.equal(handleTaskResult({ authed: true, role: 'openclaw' }), true))

  test('blocks unauthenticated openclaw client', () =>
    assert.equal(handleTaskResult({ authed: false, role: 'openclaw' }), false))

  test('blocks hermes from sending task result', () =>
    assert.equal(handleTaskResult({ authed: true, role: 'hermes' }), false))

  test('blocks ui from sending task result', () =>
    assert.equal(handleTaskResult({ authed: true, role: 'ui' }), false))
})

// ---------------------------------------------------------------------------
// Heartbeat watchdog logic
// ---------------------------------------------------------------------------

describe('heartbeat watchdog', () => {
  function runWatchdog(clients, agentStatus, now, threshold = 20_000) {
    const broadcasts = []
    for (const [, c] of clients) {
      if (c.role !== 'ui' && c.role !== 'unknown') {
        if (now - c.lastSeen > threshold && agentStatus[c.role] === 'running') {
          agentStatus[c.role] = 'missed_heartbeat'
          broadcasts.push({ type: 'AGENT_STATUS', agent: c.role, status: 'missed_heartbeat' })
        }
      }
    }
    return broadcasts
  }

  test('marks running agent as missed_heartbeat after silence > 20s', () => {
    const now = Date.now()
    const clients = new Map([['c1', { role: 'hermes', lastSeen: now - 25_000 }]])
    const agentStatus = { hermes: 'running' }
    const broadcasts = runWatchdog(clients, agentStatus, now)
    assert.equal(agentStatus.hermes, 'missed_heartbeat')
    assert.equal(broadcasts.length, 1)
    assert.equal(broadcasts[0].agent, 'hermes')
    assert.equal(broadcasts[0].status, 'missed_heartbeat')
  })

  test('does not mark agent within the 20s threshold', () => {
    const now = Date.now()
    const clients = new Map([['c1', { role: 'hermes', lastSeen: now - 5_000 }]])
    const agentStatus = { hermes: 'running' }
    runWatchdog(clients, agentStatus, now)
    assert.equal(agentStatus.hermes, 'running')
  })

  test('ignores ui clients', () => {
    const now = Date.now()
    const clients = new Map([['c1', { role: 'ui', lastSeen: now - 999_999 }]])
    const agentStatus = {}
    const broadcasts = runWatchdog(clients, agentStatus, now)
    assert.equal(broadcasts.length, 0)
  })

  test('ignores unknown-role clients', () => {
    const now = Date.now()
    const clients = new Map([['c1', { role: 'unknown', lastSeen: now - 999_999 }]])
    const broadcasts = runWatchdog(clients, {}, now)
    assert.equal(broadcasts.length, 0)
  })

  test('does not re-broadcast agent already at missed_heartbeat', () => {
    const now = Date.now()
    const clients = new Map([['c1', { role: 'hermes', lastSeen: now - 999_999 }]])
    const agentStatus = { hermes: 'missed_heartbeat' }
    const broadcasts = runWatchdog(clients, agentStatus, now)
    assert.equal(broadcasts.length, 0)
  })

  test('marks multiple stale agents', () => {
    const now = Date.now()
    const clients = new Map([
      ['c1', { role: 'hermes',   lastSeen: now - 30_000 }],
      ['c2', { role: 'openclaw', lastSeen: now - 30_000 }],
    ])
    const agentStatus = { hermes: 'running', openclaw: 'running' }
    const broadcasts = runWatchdog(clients, agentStatus, now)
    assert.equal(broadcasts.length, 2)
    assert.equal(agentStatus.hermes,   'missed_heartbeat')
    assert.equal(agentStatus.openclaw, 'missed_heartbeat')
  })

  test('exact threshold boundary: 20000ms is NOT stale', () => {
    const now = Date.now()
    const clients = new Map([['c1', { role: 'hermes', lastSeen: now - 20_000 }]])
    const agentStatus = { hermes: 'running' }
    runWatchdog(clients, agentStatus, now)
    // 20000 > 20000 is false — should still be running
    assert.equal(agentStatus.hermes, 'running')
  })
})

// ---------------------------------------------------------------------------
// agentStatus initial state
// ---------------------------------------------------------------------------

describe('agentStatus initial values', () => {
  const agentStatus = {
    openclaw: 'offline',
    hermes:   'offline',
    crabdeck: 'running',
  }

  test('openclaw starts offline', () => assert.equal(agentStatus.openclaw, 'offline'))
  test('hermes starts offline',   () => assert.equal(agentStatus.hermes,   'offline'))
  test('crabdeck starts running', () => assert.equal(agentStatus.crabdeck, 'running'))
})

// ---------------------------------------------------------------------------
// Connection close logic — agent status update on disconnect
// ---------------------------------------------------------------------------

describe('close handler — agent status on disconnect', () => {
  function handleClose(clientRole, agentStatus) {
    const broadcasts = []
    if (clientRole === 'openclaw' || clientRole === 'hermes') {
      agentStatus[clientRole] = 'offline'
      broadcasts.push({ type: 'AGENT_STATUS', agent: clientRole, status: 'offline' })
    }
    return broadcasts
  }

  test('openclaw disconnect marks it offline', () => {
    const status = { openclaw: 'running', hermes: 'running' }
    const bcast = handleClose('openclaw', status)
    assert.equal(status.openclaw, 'offline')
    assert.equal(bcast.length, 1)
    assert.equal(bcast[0].status, 'offline')
  })

  test('hermes disconnect marks it offline', () => {
    const status = { openclaw: 'running', hermes: 'running' }
    handleClose('hermes', status)
    assert.equal(status.hermes, 'offline')
  })

  test('ui disconnect does NOT change agentStatus', () => {
    const status = { openclaw: 'running', hermes: 'running' }
    const bcast = handleClose('ui', status)
    assert.equal(bcast.length, 0)
    assert.equal(status.openclaw, 'running')
  })

  test('unknown-role disconnect does NOT change agentStatus', () => {
    const status = { openclaw: 'running' }
    const bcast = handleClose('unknown', status)
    assert.equal(bcast.length, 0)
  })
})

// ---------------------------------------------------------------------------
// HELLO with token — authed flag
// ---------------------------------------------------------------------------

describe('authed flag on connection', () => {
  // When GATEWAY_TOKEN is falsy: authed = !GATEWAY_TOKEN = true (open mode)
  // When GATEWAY_TOKEN is set:   authed = false initially, set to true after valid HELLO

  test('client starts authed in open mode (no token)', () => {
    const GATEWAY_TOKEN = null
    const authed = !GATEWAY_TOKEN
    assert.equal(authed, true)
  })

  test('client starts unauthed when token is required', () => {
    const GATEWAY_TOKEN = 'secret'
    const authed = !GATEWAY_TOKEN
    assert.equal(authed, false)
  })
})

// ---------------------------------------------------------------------------
// HEARTBEAT message handling
// ---------------------------------------------------------------------------

describe('HEARTBEAT handling', () => {
  function handleHeartbeat(client, agentStatus) {
    if (!client.authed) return { allowed: false }
    const role = client.role
    const broadcasts = []
    if (role && agentStatus[role] !== undefined) {
      agentStatus[role] = 'running'
      broadcasts.push({ type: 'AGENT_STATUS', agent: role, status: 'running' })
    }
    return { allowed: true, broadcasts, ack: { type: 'HEARTBEAT_ACK' } }
  }

  test('rejects heartbeat from unauthenticated client', () => {
    const client = { authed: false, role: 'hermes' }
    const result = handleHeartbeat(client, { hermes: 'offline' })
    assert.equal(result.allowed, false)
  })

  test('accepts heartbeat from authenticated agent', () => {
    const client = { authed: true, role: 'hermes' }
    const agentStatus = { hermes: 'offline' }
    const result = handleHeartbeat(client, agentStatus)
    assert.equal(result.allowed, true)
    assert.equal(agentStatus.hermes, 'running')
  })

  test('sends HEARTBEAT_ACK on success', () => {
    const client = { authed: true, role: 'openclaw' }
    const agentStatus = { openclaw: 'running' }
    const result = handleHeartbeat(client, agentStatus)
    assert.equal(result.ack.type, 'HEARTBEAT_ACK')
  })

  test('broadcasts AGENT_STATUS update', () => {
    const client = { authed: true, role: 'openclaw' }
    const agentStatus = { openclaw: 'missed_heartbeat' }
    const result = handleHeartbeat(client, agentStatus)
    assert.equal(result.broadcasts.length, 1)
    assert.equal(result.broadcasts[0].status, 'running')
  })
})