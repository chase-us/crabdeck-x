const VAULT_URL = (process.env.VAULT_URL || 'http://localhost:7070').replace(/\/$/, '')
const VAULT_TOKEN = process.env.VAULT_TOKEN || process.env.GATEWAY_TOKEN || null

function ingestHeartbeat({ agent, ts, slot, source }) {
  if (typeof agent !== 'string' || !agent.trim()) {
    return Promise.resolve(null)
  }
  const headers = { 'content-type': 'application/json', accept: 'application/json' }
  if (VAULT_TOKEN) headers['x-vault-token'] = VAULT_TOKEN
  const ac = new AbortController()
  const timer = setTimeout(() => ac.abort(), 1500)
  return fetch(`${VAULT_URL}/v1/heartbeat`, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      agent: agent.trim(),
      ts: typeof ts === 'number' ? ts : Date.now() / 1000,
      slot: typeof slot === 'number' ? slot : undefined,
      source: typeof source === 'string' && source.trim() ? source.trim() : 'gateway',
    }),
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

module.exports = { ingestHeartbeat, VAULT_URL }
