import { useState, useEffect, useRef, useCallback } from 'react'
import Telemetry from './Telemetry.jsx'

// ─── Constants ────────────────────────────────────────────────────────────────
// These now actually read ui/.env.local (written by the installer) instead of
// being hardcoded — v2.1 wrote the file but nothing in the app ever read it.
const GW_URL        = import.meta.env.VITE_GATEWAY_WS      || 'ws://localhost:8765'
const OLLAMA_URL     = import.meta.env.VITE_OLLAMA_ENDPOINT || 'http://localhost:11434'
const API_URL        = '/api'   // proxied → orchestrator, see vite.config.js
const GATEWAY_TOKEN  = import.meta.env.VITE_GATEWAY_TOKEN   || null

const AGENTS = [
  { id: 'openclaw',  label: 'OpenClaw',  color: '#ff6b35', icon: '🦅', desc: 'Sovereign Node / System Agent' },
  { id: 'hermes',    label: 'Hermes',    color: '#a78bfa', icon: '⚡', desc: 'LLM Messenger / Ollama Bridge'  },
  { id: 'swarm',     label: 'Swarm',     color: '#22d3ee', icon: '🕸️', desc: 'Mesh Coordinator / RAG + Peers' },
  { id: 'crabdeck',  label: 'CrabDeck',  color: '#00c8ff', icon: '🦀', desc: 'Gateway Controller'             },
]

const now = () => new Date().toLocaleTimeString()

// ─── Component ────────────────────────────────────────────────────────────────
export default function CrabDeck() {
  const [gwStatus, setGwStatus]             = useState('disconnected')
  const [ollamaOnline, setOllamaOnline]     = useState(false)
  const [agentHealth, setAgentHealth]       = useState({})
  const [logs, setLogs]                     = useState([])
  const [activeTab, setActiveTab]           = useState('hermes')

  // Hermes
  const [hermesInput, setHermesInput]       = useState('')
  const [hermesReply, setHermesReply]       = useState('')
  const [hermesLoading, setHermesLoading]   = useState(false)
  const [ollamaModels, setOllamaModels]     = useState([])
  const [selectedModel, setSelectedModel]   = useState('llama3')

  // OpenClaw
  const [clawInput, setClawInput]       = useState('')
  const [clawResults, setClawResults]       = useState([])

  // Swarm mesh
  const [swarmInput, setSwarmInput]         = useState('')
  const [swarmLoading, setSwarmLoading]     = useState(false)
  const [swarmResult, setSwarmResult]       = useState('')
  const [swarmMesh, setSwarmMesh]           = useState(null)

  const wsRef  = useRef(null)
  const logRef = useRef(null)

  const log = useCallback((msg, color = '#94a3b8') => {
    setLogs(prev => [...prev.slice(-200), { t: now(), msg, color }])
  }, [])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [logs])

  // ── Gateway ──────────────────────────────────────────────────────────────────
  const connectGateway = useCallback(() => {
    setGwStatus('connecting')
    log(`Connecting to CrabDeck Gateway… ${GW_URL}`, '#fbbf24')
    const ws = new WebSocket(GW_URL)
    wsRef.current = ws

    ws.onopen = () => {
      setGwStatus('connecting') // wait for ACK before declaring "connected"
      const hello = { type: 'HELLO', client: 'crabdeck-ui', version: '2.2' }
      if (GATEWAY_TOKEN) hello.token = GATEWAY_TOKEN
      ws.send(JSON.stringify(hello))
    }

    ws.onmessage = ({ data }) => {
      try {
        const msg = JSON.parse(data)

        if (msg.type === 'ACK') {
          setGwStatus('connected')
          log(`✅ Gateway connected & authenticated  ${GW_URL}`, '#4ade80')
          return
        }
        if (msg.type === 'ERROR') {
          setGwStatus('error')
          log(`⚠ Gateway rejected handshake: ${msg.message}`, '#f87171')
          return
        }

        log(`← [${msg.type}] ${JSON.stringify(msg.payload ?? '').slice(0, 120)}`, '#7dd3fc')

        if (msg.type === 'AGENT_STATUS') {
          setAgentHealth(prev => ({ ...prev, [msg.agent]: msg.status }))
        }
        if (msg.type === 'HERMES_RESPONSE') {
          setHermesReply(String(msg.payload ?? ''))
          setHermesLoading(false)
        }
        if (msg.type === 'TASK_RESULT') {
          const result = msg.payload?.result ?? JSON.stringify(msg.payload)
          setClawResults(prev => [
            { t: now(), task: msg.payload?.task ?? '?', result },
            ...prev.slice(0, 19)
          ])
        }
        if (msg.type === 'SWARM_RESULT') {
          const synthesis = msg.payload?.synthesis ?? JSON.stringify(msg.payload)
          setSwarmResult(synthesis)
          setSwarmLoading(false)
        }
        if (msg.type === 'SWARM_MESH_STATUS') {
          setSwarmMesh(msg)
        }
        if (msg.type === 'SWARM_ACK') {
          log(`🕸️ Swarm ack: ${msg.status ?? 'ok'}`, '#22d3ee')
        }
      } catch {
        log(`← ${String(data).slice(0, 160)}`, '#7dd3fc')
      }
    }

    ws.onerror = () => {
      setGwStatus('error')
      log('⚠ Gateway error — is it running?  (cd gateway && node server.js)', '#f87171')
    }

    ws.onclose = () => {
      setGwStatus('disconnected')
      log('Gateway disconnected. Retrying in 5 s…', '#f87171')
      setTimeout(connectGateway, 5000)
    }
  }, [log])

  // ── Orchestrator poll ─────────────────────────────────────────────────────────
  const pollOrchestrator = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/agents`)
      if (!res.ok) return
      const list = await res.json()
      const map = {}
      list.forEach(a => { map[a.id] = a.status })
      setAgentHealth(prev => ({ ...prev, ...map }))
    } catch { /* orchestrator offline */ }
  }, [])

  // ── Ollama ────────────────────────────────────────────────────────────────────
  const checkOllama = useCallback(async () => {
    try {
      const res = await fetch(`${OLLAMA_URL}/api/tags`)
      if (res.ok) {
        setOllamaOnline(true)
        const data = await res.json()
        const models = (data.models ?? []).map(m => m.name)
        setOllamaModels(models)
        setSelectedModel(prev => {
          if (models.includes(prev)) return prev
          // Prefer the custom branded model (ollama/Modelfile) if it's been built.
          if (models.includes('crabdeck')) return 'crabdeck'
          return models[0] ?? prev
        })
      } else { setOllamaOnline(false) }
    } catch { setOllamaOnline(false) }
  }, [])

  // ── Boot ──────────────────────────────────────────────────────────────────────
  useEffect(() => {
    log('🦀 CrabDeck v2.2 — Hermes + OpenClaw Edition', '#00c8ff')
    log('Booting agent mesh…', '#94a3b8')
    connectGateway()
    checkOllama()
    const t = setInterval(() => { pollOrchestrator(); checkOllama() }, 8000)
    return () => { clearInterval(t); wsRef.current?.close() }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Hermes via Gateway (routes through Hermes agent → Ollama) ─────────────────
  const sendToHermes = () => {
    if (!hermesInput.trim() || hermesLoading) return
    const prompt = hermesInput.trim()
    setHermesInput('')
    setHermesLoading(true)
    setHermesReply('')
    log(`→ [Hermes/${selectedModel}] ${prompt}`, '#a78bfa')

    if (wsRef.current?.readyState === WebSocket.OPEN) {
      // Route through gateway → Hermes agent → Ollama
      wsRef.current.send(JSON.stringify({
        type: 'TO_HERMES',
        payload: { prompt, model: selectedModel }
      }))
    } else {
      // Fallback: direct Ollama call (only works if Ollama allows browser CORS)
      fetch(`${OLLAMA_URL}/api/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: selectedModel, prompt, stream: false }),
      })
        .then(r => r.json())
        .then(data => {
          const reply = data.response ?? '(no response)'
          setHermesReply(reply)
          log(`← [Hermes direct] ${reply.slice(0, 100)}`, '#c4b5fd')
        })
        .catch(e => { setHermesReply('⚠ Error: ' + e.message) })
        .finally(() => setHermesLoading(false))
    }
  }

  // ── OpenClaw via Gateway ──────────────────────────────────────────────────────
  const sendToOpenClaw = () => {
    if (!clawInput.trim()) return
    const task = clawInput.trim()
    setClawInput('')
    log(`→ [OpenClaw] ${task}`, '#ff6b35')

    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'TO_OPENCLAW',
        payload: { task, model: selectedModel }
      }))
    } else {
      log('⚠ Gateway not connected — cannot reach OpenClaw', '#f87171')
    }
  }

  // ── Swarm mesh via Gateway ──────────────────────────────────────────────────
  const sendSwarmGoal = () => {
    if (!swarmInput.trim() || swarmLoading) return
    const goal = swarmInput.trim()
    setSwarmInput('')
    setSwarmLoading(true)
    setSwarmResult('')
    log(`→ [Swarm] ${goal}`, '#22d3ee')

    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'SWARM_GOAL',
        session_id: `swarm-${Date.now()}`,
        payload: { goal, model: selectedModel },
      }))
    } else {
      log('⚠ Gateway not connected — cannot start swarm mesh', '#f87171')
      setSwarmLoading(false)
    }
  }

  const fetchMeshStatus = () => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'SWARM_MESH_STATUS' }))
    }
  }

  // ── Styles ────────────────────────────────────────────────────────────────────
  const gwColor  = { connected: '#4ade80', connecting: '#fbbf24', error: '#f87171', disconnected: '#64748b' }
  const tabStyle = (t) => ({
    padding: '6px 14px', borderRadius: '6px 6px 0 0', border: 'none', cursor: 'pointer',
    fontSize: 11, fontFamily: 'monospace', letterSpacing: 1,
    background: activeTab === t ? '#0a1628' : 'transparent',
    color:      activeTab === t ? '#00c8ff' : '#475569',
    borderBottom: activeTab === t ? '2px solid #00c8ff' : '2px solid transparent',
  })

  return (
    <div style={{ background:'#040710', color:'#e2e8f0', fontFamily:'monospace',
                  height:'100vh', display:'flex', flexDirection:'column', overflow:'hidden' }}>

      {/* ── Header ── */}
      <div style={{ background:'#0a1628', borderBottom:'1px solid #1e3a5f', padding:'10px 20px',
                    display:'flex', alignItems:'center', gap:16 }}>
        <span style={{ fontSize:22 }}>🦀</span>
        <span style={{ color:'#00c8ff', fontWeight:'bold', fontSize:16, letterSpacing:2 }}>CRABDECK</span>
        <span style={{ color:'#475569', fontSize:11 }}>v2.2 · Hermes + OpenClaw Edition</span>
        <div style={{ marginLeft:'auto', display:'flex', gap:20, fontSize:11 }}>
          <span>
            <span style={{ display:'inline-block', width:8, height:8, borderRadius:'50%',
              background: gwColor[gwStatus], marginRight:5 }} />
            Gateway {gwStatus}
          </span>
          <span>
            <span style={{ display:'inline-block', width:8, height:8, borderRadius:'50%',
              background: ollamaOnline ? '#4ade80' : '#64748b', marginRight:5 }} />
            Ollama {ollamaOnline ? 'online' : 'offline'}
          </span>
        </div>
      </div>

      <div style={{ display:'flex', flex:1, overflow:'hidden' }}>

        {/* ── Left: Agent Panel ── */}
        <div style={{ width:220, background:'#06111f', borderRight:'1px solid #1e3a5f',
                      padding:12, display:'flex', flexDirection:'column', gap:8, flexShrink:0 }}>
          <div style={{ color:'#475569', fontSize:10, letterSpacing:2, marginBottom:4 }}>TEAM CRABDECK</div>
          {AGENTS.map(a => {
            const health = agentHealth[a.id]
            const ok = health === 'running'
                    || (a.id === 'crabdeck' && gwStatus === 'connected')
                    || (a.id === 'hermes'   && ollamaOnline)
            return (
              <div key={a.id} style={{ background:'#0a1628', border:`1px solid ${ok ? a.color + '44' : '#1e3a5f'}`,
                                       borderRadius:8, padding:'10px 12px', cursor:'pointer' }}
                   onClick={() => setActiveTab(a.id === 'crabdeck' ? 'telemetry' : a.id)}>
                <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom:4 }}>
                  <span style={{ fontSize:18 }}>{a.icon}</span>
                  <span style={{ color: ok ? a.color : '#64748b', fontWeight:'bold', fontSize:13 }}>{a.label}</span>
                  <span style={{ marginLeft:'auto', width:7, height:7, borderRadius:'50%',
                                 background: ok ? '#4ade80' : '#ef4444', display:'inline-block' }} />
                </div>
                <div style={{ color:'#475569', fontSize:10 }}>{a.desc}</div>
                {health && <div style={{ color:'#334155', fontSize:9, marginTop:3 }}>status: {health}</div>}
              </div>
            )
          })}

          {ollamaOnline && ollamaModels.length > 0 && (
            <div style={{ marginTop:8 }}>
              <div style={{ color:'#475569', fontSize:10, letterSpacing:2, marginBottom:6 }}>HERMES MODEL</div>
              <select value={selectedModel} onChange={e => setSelectedModel(e.target.value)}
                style={{ width:'100%', background:'#0a1628', color:'#a78bfa', border:'1px solid #312e81',
                         borderRadius:6, padding:'5px 8px', fontSize:11 }}>
                {ollamaModels.map(m => <option key={m} value={m}>{m}</option>)}
              </select>
            </div>
          )}
        </div>

        {/* ── Right: Tabs ── */}
        <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>

          {/* Tab bar */}
          <div style={{ display:'flex', gap:4, padding:'8px 16px 0',
                        borderBottom:'1px solid #1e3a5f', background:'#06111f' }}>
            <button style={tabStyle('hermes')}  onClick={() => setActiveTab('hermes')}>⚡ HERMES</button>
            <button style={tabStyle('openclaw')} onClick={() => setActiveTab('openclaw')}>🦅 OPENCLAW</button>
            <button style={tabStyle('swarm')} onClick={() => { setActiveTab('swarm'); fetchMeshStatus() }}>🕸️ SWARM</button>
            <button style={tabStyle('telemetry')} onClick={() => setActiveTab('telemetry')}>📡 TELEMETRY</button>
            <button style={tabStyle('system')}  onClick={() => setActiveTab('system')}>📜 SYSTEM LOG</button>
          </div>

          {/* ── Hermes Tab ── */}
          {activeTab === 'hermes' && (
            <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
              <div style={{ flex:1, overflowY:'auto', padding:'14px 16px' }}>
                {hermesReply ? (
                  <div style={{ background:'#0f172a', border:'1px solid #312e81', borderRadius:8,
                                padding:'12px 14px', fontSize:12, color:'#c4b5fd', lineHeight:1.6, whiteSpace:'pre-wrap' }}>
                    <span style={{ color:'#7c3aed', fontSize:10, display:'block', marginBottom:6 }}>⚡ HERMES RESPONSE</span>
                    {hermesReply}
                  </div>
                ) : (
                  <div style={{ color:'#334155', fontSize:12, marginTop:40, textAlign:'center' }}>
                    {ollamaOnline
                      ? `Hermes is ready — ${ollamaModels.length} model(s) available. Type a prompt below.`
                      : '⚠ Ollama offline. Run: ollama serve'}
                  </div>
                )}
                {hermesLoading && (
                  <div style={{ color:'#a78bfa', fontSize:12, marginTop:16 }}>
                    ⚡ Hermes is thinking via {selectedModel}…
                  </div>
                )}
              </div>
              <div style={{ display:'flex', gap:8, padding:'10px 16px',
                            borderTop:'1px solid #1e3a5f', background:'#06111f' }}>
                <input value={hermesInput} onChange={e => setHermesInput(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && sendToHermes()}
                  placeholder={ollamaOnline ? `Ask Hermes (${selectedModel})…` : 'Ollama offline'}
                  style={{ flex:1, background:'#0a1628', border:'1px solid #1e3a5f', borderRadius:6,
                           color:'#e2e8f0', padding:'8px 12px', fontSize:12, outline:'none' }} />
                <button onClick={sendToHermes} disabled={hermesLoading || !ollamaOnline}
                  style={{ background: ollamaOnline ? '#7c3aed' : '#1e293b', color:'#e2e8f0',
                           border:'none', borderRadius:6, padding:'8px 18px', cursor:'pointer', fontSize:12 }}>
                  {hermesLoading ? '…' : '⚡ Send'}
                </button>
              </div>
            </div>
          )}

          {/* ── OpenClaw Tab ── */}
          {activeTab === 'openclaw' && (
            <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
              <div style={{ flex:1, overflowY:'auto', padding:'14px 16px', display:'flex', flexDirection:'column', gap:10 }}>
                {clawResults.length === 0 && (
                  <div style={{ color:'#334155', fontSize:12, marginTop:40, textAlign:'center' }}>
                    {agentHealth['openclaw'] === 'running'
                      ? 'OpenClaw is online. Send a task below.'
                      : '⚠ OpenClaw offline. Run: python agents/openclaw_agent.py'}
                  </div>
                )}
                {clawResults.map((r, i) => (
                  <div key={i} style={{ background:'#0a1628', border:'1px solid #ff6b3533', borderRadius:8, padding:12 }}>
                    <div style={{ color:'#ff6b35', fontSize:10, marginBottom:6 }}>
                      🦅 [{r.t}] TASK: {r.task}
                    </div>
                    <div style={{ color:'#e2e8f0', fontSize:11, whiteSpace:'pre-wrap', lineHeight:1.5 }}>{r.result}</div>
                  </div>
                ))}
              </div>
              <div style={{ display:'flex', gap:8, padding:'10px 16px',
                            borderTop:'1px solid #1e3a5f', background:'#06111f' }}>
                <input value={clawInput} onChange={e => setClawInput(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && sendToOpenClaw()}
                  placeholder="Send task to OpenClaw… (e.g. list running processes)"
                  style={{ flex:1, background:'#0a1628', border:'1px solid #1e3a5f', borderRadius:6,
                           color:'#e2e8f0', padding:'8px 12px', fontSize:12, outline:'none' }} />
                <button onClick={sendToOpenClaw}
                  style={{ background:'#c2410c', color:'#fff', border:'none', borderRadius:6,
                           padding:'8px 18px', cursor:'pointer', fontSize:12 }}>
                  🦅 Send
                </button>
              </div>
            </div>
          )}

          {/* ── Swarm Mesh Tab ── */}
          {activeTab === 'swarm' && (
            <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
              <div style={{ padding:'10px 16px', borderBottom:'1px solid #1e3a5f', background:'#06111f',
                            display:'flex', gap:12, alignItems:'center', flexWrap:'wrap' }}>
                <span style={{ color:'#22d3ee', fontSize:11, letterSpacing:1 }}>MESH STATUS</span>
                {swarmMesh?.online && Object.entries(swarmMesh.online).map(([id, st]) => (
                  <span key={id} style={{ fontSize:10, color: st === 'running' ? '#4ade80' : '#64748b' }}>
                    {id}: {st}
                  </span>
                ))}
                <button onClick={fetchMeshStatus}
                  style={{ marginLeft:'auto', background:'#0f172a', color:'#22d3ee', border:'1px solid #1e3a5f',
                           borderRadius:6, padding:'4px 10px', cursor:'pointer', fontSize:10 }}>
                  Refresh mesh
                </button>
              </div>
              <div style={{ flex:1, overflowY:'auto', padding:'14px 16px' }}>
                {swarmResult ? (
                  <div style={{ background:'#0f172a', border:'1px solid #0891b2', borderRadius:8,
                                padding:'12px 14px', fontSize:12, color:'#a5f3fc', lineHeight:1.6, whiteSpace:'pre-wrap' }}>
                    <span style={{ color:'#22d3ee', fontSize:10, display:'block', marginBottom:6 }}>🕸️ SWARM SYNTHESIS (RAG + peers)</span>
                    {swarmResult}
                  </div>
                ) : (
                  <div style={{ color:'#334155', fontSize:12, marginTop:40, textAlign:'center' }}>
                    {agentHealth['swarm'] === 'running'
                      ? 'Swarm coordinator online. Send a collaborative goal — Hermes + OpenClaw will work together with shared RAG context.'
                      : '⚠ Swarm offline. Run: python agents/swarm_agent.py'}
                  </div>
                )}
                {swarmLoading && (
                  <div style={{ color:'#22d3ee', fontSize:12, marginTop:16 }}>
                    🕸️ Coordinating mesh — retrieving RAG context, delegating to peers…
                  </div>
                )}
              </div>
              <div style={{ display:'flex', gap:8, padding:'10px 16px',
                            borderTop:'1px solid #1e3a5f', background:'#06111f' }}>
                <input value={swarmInput} onChange={e => setSwarmInput(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && sendSwarmGoal()}
                  placeholder="Collaborative swarm goal (RAG + Hermes + OpenClaw)…"
                  style={{ flex:1, background:'#0a1628', border:'1px solid #1e3a5f', borderRadius:6,
                           color:'#e2e8f0', padding:'8px 12px', fontSize:12, outline:'none' }} />
                <button onClick={sendSwarmGoal} disabled={swarmLoading}
                  style={{ background: swarmLoading ? '#1e293b' : '#0891b2', color:'#fff', border:'none',
                           borderRadius:6, padding:'8px 18px', cursor:'pointer', fontSize:12 }}>
                  {swarmLoading ? '…' : '🕸️ Run Swarm'}
                </button>
              </div>
            </div>
          )}

          {activeTab === 'telemetry' && (
            <div style={{ flex:1, overflow:'hidden', minHeight:0 }}>
              <Telemetry />
            </div>
          )}

          {/* ── System Log Tab ── */}
          {activeTab === 'system' && (
            <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
              <div ref={logRef} style={{ flex:1, overflowY:'auto', padding:'10px 16px', fontSize:11 }}>
                {logs.map((l, i) => (
                  <div key={i} style={{ color: l.color, marginBottom:2 }}>
                    <span style={{ color:'#334155' }}>[{l.t}]</span> {l.msg}
                  </div>
                ))}
              </div>
              <div style={{ display:'flex', gap:8, padding:'10px 16px',
                            borderTop:'1px solid #1e3a5f', background:'#06111f' }}>
                <button onClick={() => wsRef.current?.send(JSON.stringify({ type:'PING' }))}
                  style={{ background:'#0f172a', color:'#00c8ff', border:'1px solid #1e3a5f',
                           borderRadius:6, padding:'8px 14px', cursor:'pointer', fontSize:11 }}>
                  Ping GW
                </button>
                <button onClick={() => setLogs([])}
                  style={{ background:'#0f172a', color:'#475569', border:'1px solid #1e3a5f',
                           borderRadius:6, padding:'8px 14px', cursor:'pointer', fontSize:11 }}>
                  Clear Log
                </button>
                <span style={{ marginLeft:'auto', color:'#334155', fontSize:10, alignSelf:'center' }}>
                  {logs.length} events
                </span>
              </div>
            </div>
          )}

        </div>
      </div>
    </div>
  )
}
