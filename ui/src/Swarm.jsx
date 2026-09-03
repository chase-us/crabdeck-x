import { useState } from 'react'

const PEER_STYLE = {
  hermes:       { color: '#a78bfa', icon: '⚡', label: 'Hermes' },
  openclaw:     { color: '#ff6b35', icon: '🦅', label: 'OpenClaw' },
  orchestrator: { color: '#00c8ff', icon: '🧭', label: 'Orchestrator' },
  gateway:      { color: '#64748b', icon: '🦀', label: 'Gateway' },
}
const peerStyle = (role) => PEER_STYLE[role] ?? { color: '#94a3b8', icon: '•', label: role }

const STATUS_COLOR = {
  running: '#fbbf24', synthesizing: '#a78bfa', done: '#4ade80', failed: '#f87171',
}

const panel = {
  background: '#0a1628', border: '1px solid #1e3a5f', borderRadius: 8, padding: 12,
}
const label = { color: '#475569', fontSize: 10, letterSpacing: 2, marginBottom: 6 }

export function initialSwarmState() {
  return { peers: [], session: null, history: [], traces: [] }
}

/** Fold one gateway frame into swarm state. Pure; used by CrabDeck.jsx's ws.onmessage. */
export function reduceSwarm(state, msg) {
  const p = msg.payload ?? {}
  switch (msg.type) {
    case 'MESH_PEERS':
      return { ...state, peers: Array.isArray(p.peers) ? p.peers : [] }
    case 'SWARM_STARTED':
      return {
        ...state,
        session: {
          id: p.session_id, goal: p.goal, status: p.status ?? 'running',
          round: p.round ?? 1, maxRounds: p.max_rounds ?? 1,
          peers: p.peers ?? [], context: p.context ?? [], model: p.model ?? null,
          rounds: { 1: {} }, result: null, synthesizedBy: null, error: null,
          startedAt: p.created_at ?? Date.now(),
        },
      }
    case 'SWARM_ROUND':
      if (!state.session || state.session.id !== p.session_id) return state
      return {
        ...state,
        session: {
          ...state.session,
          round: p.round, maxRounds: p.max_rounds ?? state.session.maxRounds,
          rounds: { ...state.session.rounds, [p.round]: state.session.rounds[p.round] ?? {} },
        },
      }
    case 'SWARM_CONTRIBUTION': {
      if (!state.session || state.session.id !== p.session_id) return state
      const rounds = { ...state.session.rounds }
      rounds[p.round] = { ...(rounds[p.round] ?? {}), [p.from]: p.text }
      return { ...state, session: { ...state.session, rounds } }
    }
    case 'SWARM_SYNTHESIZING':
      if (!state.session || state.session.id !== p.session_id) return state
      return { ...state, session: { ...state.session, status: 'synthesizing', synthesizedBy: p.synthesizer } }
    case 'SWARM_RESULT': {
      if (!state.session || state.session.id !== p.session_id) return state
      const done = {
        ...state.session, status: p.status, result: p.result, error: p.error,
        synthesizedBy: p.synthesized_by, finishedAt: p.finished_at,
      }
      return {
        ...state,
        session: done,
        history: [{ id: done.id, goal: done.goal, status: done.status, peers: done.peers, finishedAt: done.finishedAt },
                  ...state.history.filter((h) => h.id !== done.id)].slice(0, 20),
      }
    }
    case 'MESH_TRACE':
      return { ...state, traces: [{ t: Date.now(), ...p }, ...state.traces].slice(0, 30) }
    default:
      return state
  }
}

export default function Swarm({ swarm, connected, send, defaultModel }) {
  const [goal, setGoal] = useState('')
  const [rounds, setRounds] = useState(2)
  const s = swarm.session
  const busy = s && (s.status === 'running' || s.status === 'synthesizing')
  const canLaunch = connected && swarm.peers.length > 0 && goal.trim() && !busy

  const launch = () => {
    if (!canLaunch) return
    send({ type: 'SWARM_TASK', payload: { goal: goal.trim(), rounds, model: defaultModel } })
    setGoal('')
  }

  const roundNumbers = s ? Object.keys(s.rounds).map(Number).sort((a, b) => a - b) : []

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <div style={{ flex: 1, overflowY: 'auto', padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 12 }}>

        {/* Mesh roster */}
        <div style={panel}>
          <div style={label}>MESH PEERS</div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {['hermes', 'openclaw', 'orchestrator'].map((role) => {
              const on = swarm.peers.includes(role)
              const st = peerStyle(role)
              return (
                <span key={role} style={{
                  display: 'inline-flex', alignItems: 'center', gap: 6, padding: '4px 10px', borderRadius: 999,
                  border: `1px solid ${on ? st.color + '66' : '#1e3a5f'}`, color: on ? st.color : '#475569', fontSize: 11,
                }}>
                  <span style={{ width: 6, height: 6, borderRadius: '50%', background: on ? '#4ade80' : '#334155', display: 'inline-block' }} />
                  {st.icon} {st.label}
                </span>
              )
            })}
            <span style={{ marginLeft: 'auto', color: '#334155', fontSize: 10, alignSelf: 'center' }}>
              {swarm.peers.length}/3 in mesh · rounds fan out to every peer · Hermes synthesizes
            </span>
          </div>
        </div>

        {/* Session */}
        {!s && (
          <div style={{ color: '#334155', fontSize: 12, marginTop: 24, textAlign: 'center' }}>
            {swarm.peers.length === 0
              ? '⚠ No agent peers on the mesh. Start Hermes / OpenClaw / Orchestrator.'
              : 'Give the swarm a goal. The gateway retrieves matching Shell Cracked memory, every peer answers, then peers critique each other before Hermes synthesizes.'}
          </div>
        )}

        {s && (
          <>
            <div style={panel}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: STATUS_COLOR[s.status] ?? '#64748b', display: 'inline-block' }} />
                <span style={{ color: '#e2e8f0', fontSize: 13, fontWeight: 'bold' }}>{s.goal}</span>
                <span style={{ marginLeft: 'auto', color: '#475569', fontSize: 10 }}>
                  {s.status.toUpperCase()} · round {s.round}/{s.maxRounds} · {s.id?.slice(0, 8)}
                </span>
              </div>
              <div style={{ color: '#475569', fontSize: 10 }}>
                peers: {s.peers.join(', ')}{s.model ? ` · model ${s.model}` : ''}
              </div>
            </div>

            {/* RAG context */}
            <div style={panel}>
              <div style={label}>RETRIEVED MEMORY (RAG SEED) · {s.context.length} hit{s.context.length === 1 ? '' : 's'}</div>
              {s.context.length === 0 && <div style={{ color: '#334155', fontSize: 11 }}>Vault had nothing matching this goal yet. The result will be stored so the next swarm starts warmer.</div>}
              {s.context.map((hit, i) => (
                <div key={hit.id || i} style={{ borderLeft: '2px solid #312e81', paddingLeft: 8, marginBottom: 6 }}>
                  <div style={{ color: '#7c3aed', fontSize: 10 }}>[{i + 1}] {hit.agent}/{hit.kind} · score {Number(hit.score ?? 0).toFixed(3)}</div>
                  <div style={{ color: '#94a3b8', fontSize: 11, whiteSpace: 'pre-wrap' }}>{hit.text}</div>
                </div>
              ))}
            </div>

            {/* Rounds */}
            {roundNumbers.map((r) => (
              <div key={r} style={panel}>
                <div style={label}>ROUND {r}{r === 1 ? ' · OPENING POSITIONS' : r < s.maxRounds ? ' · CRITIQUE & CONVERGE' : ' · FINAL POSITIONS'}</div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 8 }}>
                  {s.peers.map((role) => {
                    const st = peerStyle(role)
                    const text = s.rounds[r]?.[role]
                    return (
                      <div key={role} style={{ background: '#06111f', border: `1px solid ${st.color}33`, borderRadius: 6, padding: 10, minHeight: 60 }}>
                        <div style={{ color: st.color, fontSize: 10, marginBottom: 4 }}>{st.icon} {st.label.toUpperCase()}</div>
                        <div style={{ color: text ? '#e2e8f0' : '#334155', fontSize: 11, whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>
                          {text ?? (s.status === 'running' && s.round === r ? 'thinking…' : 'silent')}
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            ))}

            {/* Synthesis */}
            {(s.status === 'synthesizing' || s.result || s.error) && (
              <div style={{ ...panel, border: `1px solid ${s.error ? '#7f1d1d' : '#4c1d95'}` }}>
                <div style={label}>
                  {s.error ? 'SWARM FAILED' : `SYNTHESIS · ${peerStyle(s.synthesizedBy ?? 'hermes').label.toUpperCase()}`}
                </div>
                {s.status === 'synthesizing' && <div style={{ color: '#a78bfa', fontSize: 12 }}>⚡ Hermes is merging the transcript…</div>}
                {s.error && <div style={{ color: '#f87171', fontSize: 12 }}>{s.error}</div>}
                {s.result && <div style={{ color: '#c4b5fd', fontSize: 12, whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>{s.result}</div>}
                {s.status === 'done' && <div style={{ color: '#64748b', fontSize: 10, marginTop: 8 }}>Stored in Shell Cracked as swarm_result — search it in Telemetry.</div>}
              </div>
            )}
          </>
        )}

        {/* Mesh traces + history */}
        {(swarm.traces.length > 0 || swarm.history.length > 0) && (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 12 }}>
            {swarm.traces.length > 0 && (
              <div style={panel}>
                <div style={label}>PEER ↔ PEER MESH TRAFFIC</div>
                {swarm.traces.map((tr, i) => (
                  <div key={i} style={{ fontSize: 10, color: '#94a3b8', marginBottom: 3 }}>
                    <span style={{ color: peerStyle(tr.from).color }}>{tr.from}</span>
                    {' → '}
                    <span style={{ color: peerStyle(tr.to).color }}>{tr.to}</span>
                    <span style={{ color: '#475569' }}> [{tr.intent}] </span>
                    {String(tr.text ?? '').slice(0, 90)}
                  </div>
                ))}
              </div>
            )}
            {swarm.history.length > 0 && (
              <div style={panel}>
                <div style={label}>RECENT SWARMS</div>
                {swarm.history.map((h) => (
                  <div key={h.id} style={{ fontSize: 10, color: '#94a3b8', marginBottom: 3, display: 'flex', gap: 6 }}>
                    <span style={{ width: 6, height: 6, borderRadius: '50%', background: STATUS_COLOR[h.status] ?? '#64748b', display: 'inline-block', marginTop: 3 }} />
                    <span style={{ flex: 1 }}>{h.goal.slice(0, 60)}</span>
                    <span style={{ color: '#475569' }}>{h.peers.length} peers</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Launch bar */}
      <div style={{ display: 'flex', gap: 8, padding: '10px 16px', borderTop: '1px solid #1e3a5f', background: '#06111f' }}>
        <input value={goal} onChange={(e) => setGoal(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && launch()}
          placeholder={swarm.peers.length ? 'Goal for the swarm… (e.g. plan the v2.3 release)' : 'Waiting for mesh peers…'}
          style={{ flex: 1, background: '#0a1628', border: '1px solid #1e3a5f', borderRadius: 6,
                   color: '#e2e8f0', padding: '8px 12px', fontSize: 12, outline: 'none' }} />
        <select value={rounds} onChange={(e) => setRounds(Number(e.target.value))}
          style={{ background: '#0a1628', color: '#94a3b8', border: '1px solid #1e3a5f', borderRadius: 6, padding: '5px 8px', fontSize: 11 }}>
          {[1, 2, 3, 4].map((n) => <option key={n} value={n}>{n} round{n === 1 ? '' : 's'}</option>)}
        </select>
        <button onClick={launch} disabled={!canLaunch}
          style={{ background: canLaunch ? '#0e7490' : '#1e293b', color: '#e2e8f0', border: 'none', borderRadius: 6,
                   padding: '8px 18px', cursor: canLaunch ? 'pointer' : 'default', fontSize: 12 }}>
          {busy ? '…' : '🕸 Swarm'}
        </button>
      </div>
    </div>
  )
}
