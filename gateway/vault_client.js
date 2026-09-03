const VAULT_URL = (process.env.VAULT_URL || 'http://localhost:7070').replace(/\/$/, '')
const VAULT_TOKEN = process.env.VAULT_TOKEN || process.env.GATEWAY_TOKEN || null
const VAULT_TIMEOUT_MS = 1500

// Every helper here is fail-open: a down or slow vault resolves to null and
// must never delay a HEARTBEAT_ACK or a swarm round.
function vaultFetch(path, { method = 'GET', body } = {}) {
  const headers = { accept: 'application/json' }
  if (body !== undefined) headers['content-type'] = 'application/json'
  if (VAULT_TOKEN) headers['x-vault-token'] = VAULT_TOKEN
  const ac = new AbortController()
  const timer = setTimeout(() => ac.abort(), VAULT_TIMEOUT_MS)
  return fetch(`${VAULT_URL}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: ac.signal,
  })
    .then(async (res) => {
      if (!res.ok) return null
      try {
        return await res.json()
      } catch {
        return null
      }
    })
    .catch(() => null)
    .finally(() => clearTimeout(timer))
}

function ingestHeartbeat({ agent, ts, slot, source }) {
  if (typeof agent !== 'string' || !agent.trim()) {
    return Promise.resolve(null)
  }
  return vaultFetch('/v1/heartbeat', {
    method: 'POST',
    body: {
      agent: agent.trim(),
      ts: typeof ts === 'number' ? ts : Date.now() / 1000,
      slot: typeof slot === 'number' ? slot : undefined,
      source: typeof source === 'string' && source.trim() ? source.trim() : 'gateway',
    },
  })
}

/** RAG retrieval: top-n vault memories for a query. Resolves [] on any failure. */
function queryMemory(q, n = 5) {
  if (typeof q !== 'string' || !q.trim()) return Promise.resolve([])
  const limit = Number.isInteger(n) && n >= 1 && n <= 50 ? n : 5
  const qs = new URLSearchParams({ q: q.trim().slice(0, 2000), n: String(limit) })
  return vaultFetch(`/v1/memory/query?${qs}`).then((body) =>
    body && Array.isArray(body.hits) ? body.hits : [],
  )
}

/** Write a new memory document. `agent` must be an allow-listed swarm member. */
function ingestMemory({ agent, kind, text, metadata }) {
  if (typeof agent !== 'string' || !agent.trim()) return Promise.resolve(null)
  if (typeof kind !== 'string' || !kind.trim() || kind.length > 64) return Promise.resolve(null)
  if (typeof text !== 'string' || !text.trim()) return Promise.resolve(null)
  return vaultFetch('/v1/memory', {
    method: 'POST',
    body: {
      agent: agent.trim(),
      kind: kind.trim(),
      text: text.slice(0, 8000),
      metadata: metadata && typeof metadata === 'object' && !Array.isArray(metadata) ? metadata : {},
    },
  })
}

/** Persist swarm session state so a gateway restart does not lose the transcript. */
function upsertSession(sessionId, context) {
  if (typeof sessionId !== 'string' || !sessionId.trim()) return Promise.resolve(null)
  if (!context || typeof context !== 'object' || Array.isArray(context)) return Promise.resolve(null)
  return vaultFetch('/v1/session', {
    method: 'POST',
    body: { session_id: sessionId.trim().slice(0, 128), context },
  })
}

module.exports = { ingestHeartbeat, queryMemory, ingestMemory, upsertSession, VAULT_URL }
