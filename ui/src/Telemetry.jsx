import { useCallback, useEffect, useState } from 'react'

const VAULT = '/vault'
const GW = '/gw'

function statusTone(status) {
  if (status === 'running' || status === 'ok') return 'bg-emerald-400'
  if (status === 'slot_lag') return 'bg-amber-400'
  if (status === 'missed_heartbeat' || status === 'error') return 'bg-rose-500'
  return 'bg-slate-500'
}

function asList(value) {
  if (Array.isArray(value)) return value
  if (value && Array.isArray(value.agents)) return value.agents
  if (value && Array.isArray(value.clients)) return value.clients
  return []
}

export default function Telemetry() {
  const [vault, setVault] = useState(null)
  const [bhive, setBhive] = useState(null)
  const [gateway, setGateway] = useState(null)
  const [metrics, setMetrics] = useState(null)
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState([])
  const [error, setError] = useState('')
  const [searching, setSearching] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [v, b, g, m] = await Promise.allSettled([
        fetch(`${VAULT}/health`).then((r) => (r.ok ? r.json() : null)),
        fetch(`${VAULT}/v1/bhive`).then((r) => (r.ok ? r.json() : null)),
        fetch(`${GW}/health`).then((r) => (r.ok ? r.json() : null)),
        fetch(`${GW}/metrics`).then((r) => (r.ok ? r.json() : null)),
      ])
      setVault(v.status === 'fulfilled' ? v.value : null)
      setBhive(b.status === 'fulfilled' ? b.value : null)
      setGateway(g.status === 'fulfilled' ? g.value : null)
      setMetrics(m.status === 'fulfilled' ? m.value : null)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'telemetry poll failed')
    }
  }, [])

  useEffect(() => {
    refresh()
    const timer = setInterval(refresh, 5000)
    return () => clearInterval(timer)
  }, [refresh])

  const searchMemory = async () => {
    const q = query.trim()
    if (!q || searching) return
    setSearching(true)
    try {
      const res = await fetch(`${VAULT}/v1/memory/query?q=${encodeURIComponent(q)}&n=5`)
      if (!res.ok) {
        setHits([])
        setError(`memory query failed (${res.status})`)
        return
      }
      const body = await res.json()
      setHits(Array.isArray(body.hits) ? body.hits : [])
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'memory query failed')
    } finally {
      setSearching(false)
    }
  }

  const agents = asList(bhive?.agents)
  const clients = asList(metrics?.clients)
  const slot = bhive?.slot ?? gateway?.bhive_slot ?? metrics?.slot ?? '—'

  return (
    <div className="flex h-full flex-col overflow-hidden bg-slate-950 text-slate-200">
      <div className="grid gap-3 p-4 sm:grid-cols-3">
        <div className="rounded-lg border border-cyan-900/60 bg-slate-900/80 p-3">
          <div className="text-[10px] tracking-[0.2em] text-slate-500">BHIVE SLOT</div>
          <div className="mt-1 font-mono text-xl text-cyan-400">{slot}</div>
          <div className="mt-1 text-[11px] text-slate-500">watchdog 20s · minute slots</div>
        </div>
        <div className="rounded-lg border border-cyan-900/60 bg-slate-900/80 p-3">
          <div className="text-[10px] tracking-[0.2em] text-slate-500">SHELL CRACKED</div>
          <div className="mt-1 flex items-center gap-2">
            <span className={`inline-block h-2 w-2 rounded-full ${statusTone(vault?.status)}`} />
            <span className="font-mono text-sm">{vault?.service ?? 'offline'}</span>
          </div>
          <div className="mt-1 text-[11px] text-slate-500">
            vectors {vault?.vector_count ?? 0} · {vault?.vector_backend ?? 'sqlite'}
          </div>
        </div>
        <div className="rounded-lg border border-cyan-900/60 bg-slate-900/80 p-3">
          <div className="text-[10px] tracking-[0.2em] text-slate-500">GATEWAY</div>
          <div className="mt-1 flex items-center gap-2">
            <span className={`inline-block h-2 w-2 rounded-full ${statusTone(gateway?.status)}`} />
            <span className="font-mono text-sm">{gateway ? `${gateway.clients ?? 0} clients` : 'offline'}</span>
          </div>
          <div className="mt-1 text-[11px] text-slate-500">
            uptime {gateway?.uptime != null ? `${Math.floor(gateway.uptime)}s` : '—'}
            {gateway?.swarm ? ` · swarm ${gateway.swarm.active} active / ${gateway.swarm.total} total · ${gateway.swarm.peers?.length ?? 0} peers` : ''}
          </div>
        </div>
      </div>

      {error && (
        <div className="mx-4 mb-2 rounded border border-rose-900 bg-rose-950/40 px-3 py-2 text-xs text-rose-300">
          {error}
        </div>
      )}

      <div className="grid flex-1 gap-3 overflow-hidden px-4 pb-4 lg:grid-cols-2">
        <div className="flex flex-col overflow-hidden rounded-lg border border-slate-800 bg-slate-900/60">
          <div className="border-b border-slate-800 px-3 py-2 text-[10px] tracking-[0.2em] text-slate-500">
            AGENT WATCH
          </div>
          <div className="flex-1 overflow-y-auto p-3">
            {agents.length === 0 && (
              <div className="text-xs text-slate-600">No vault heartbeats yet. Start Hermes / OpenClaw.</div>
            )}
            {agents.map((agent) => (
              <div key={agent.id || agent.agent} className="mb-2 rounded border border-slate-800 bg-slate-950/80 p-2">
                <div className="flex items-center gap-2">
                  <span className={`inline-block h-2 w-2 rounded-full ${statusTone(agent.status)}`} />
                  <span className="font-mono text-sm text-cyan-300">{agent.id || agent.agent}</span>
                  <span className="ml-auto font-mono text-[10px] text-slate-500">{agent.status}</span>
                </div>
                <div className="mt-1 font-mono text-[10px] text-slate-600">
                  slot {agent.last_slot} → {agent.now_slot}
                  {agent.watchdog_miss ? ' · watchdog miss' : ''}
                  {agent.slot_miss ? ' · slot lag' : ''}
                </div>
              </div>
            ))}
            {clients.length > 0 && (
              <div className="mt-3 text-[10px] tracking-[0.2em] text-slate-500">GATEWAY SOCKETS</div>
            )}
            {clients.map((client) => (
              <div key={client.id} className="mt-1 font-mono text-[11px] text-slate-400">
                {client.role} {client.watchdog_miss ? '· missed' : '· live'}
              </div>
            ))}
          </div>
        </div>

        <div className="flex flex-col overflow-hidden rounded-lg border border-slate-800 bg-slate-900/60">
          <div className="border-b border-slate-800 px-3 py-2 text-[10px] tracking-[0.2em] text-slate-500">
            VECTOR MEMORY
          </div>
          <div className="flex gap-2 border-b border-slate-800 p-3">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && searchMemory()}
              placeholder="Query Shell Cracked…"
              className="flex-1 rounded border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-slate-200 outline-none focus:border-cyan-600"
            />
            <button
              type="button"
              onClick={searchMemory}
              disabled={searching}
              className="rounded bg-cyan-700 px-3 py-2 text-xs text-white disabled:bg-slate-700"
            >
              {searching ? '…' : 'Search'}
            </button>
          </div>
          <div className="flex-1 overflow-y-auto p-3">
            {hits.length === 0 && (
              <div className="text-xs text-slate-600">No hits. Prompt Hermes or dispatch an OpenClaw task to seed memory.</div>
            )}
            {hits.map((hit) => (
              <div key={hit.id} className="mb-2 rounded border border-violet-900/40 bg-slate-950/80 p-2">
                <div className="font-mono text-[10px] text-violet-400">
                  {hit.metadata?.agent ?? 'agent'} · score {Number(hit.score ?? 0).toFixed(3)}
                </div>
                <div className="mt-1 whitespace-pre-wrap text-xs text-slate-300">{hit.text}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
