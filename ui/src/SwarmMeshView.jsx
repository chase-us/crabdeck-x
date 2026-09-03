import { useState, useEffect, useCallback } from 'react'

const VAULT = '/vault'
const GW = '/gw'
const API = '/api'

export default function SwarmMeshView({ wsRef, log }) {
  const [mesh, setMesh] = useState(null)
  const [swarmGoal, setSwarmGoal] = useState('')
  const [ragQuery, setRagQuery] = useState('')
  const [ragResults, setRagResults] = useState(null)
  const [activeTasks, setActiveTasks] = useState([])
  const [peerMsg, setPeerMsg] = useState({ target: 'openclaw', action: 'COLLAB_QUERY', message: '' })
  const [collabLogs, setCollabLogs] = useState([])
  const [loading, setLoading] = useState(false)
  const [ragLoading, setRagLoading] = useState(false)

  // Fetch current mesh topology from gateway & orchestrator
  const refreshMesh = useCallback(async () => {
    try {
      const res = await fetch(`${GW}/mesh`)
      if (res.ok) {
        const data = await res.json()
        setMesh(data)
      } else {
        // Fallback to orchestrator /mesh
        const orchRes = await fetch(`${API}/mesh`)
        if (orchRes.ok) {
          const orchData = await orchRes.json()
          setMesh(orchData)
        }
      }
    } catch {
      // Mesh fetch fail-open
    }
  }, [])

  useEffect(() => {
    refreshMesh()
    const timer = setInterval(refreshMesh, 4000)
    return () => clearInterval(timer)
  }, [refreshMesh])

  // Trigger coordinated multi-agent swarm task
  const launchSwarmTask = async () => {
    if (!swarmGoal.trim() || loading) return
    const goal = swarmGoal.trim()
    setSwarmGoal('')
    setLoading(true)

    const newTaskId = `task-${Date.now().toString(36)}`
    const taskEntry = {
      id: newTaskId,
      goal,
      status: 'in_progress',
      initiator: 'crabdeck-ui',
      createdAt: Date.now(),
      results: {},
    }
    setActiveTasks(prev => [taskEntry, ...prev])

    // Broadcast over WebSocket mesh
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'SWARM_COORDINATE',
        taskId: newTaskId,
        goal,
        payload: { goal },
      }))
      log?.(`🌐 [Swarm Mesh] Dispatched multi-agent task: "${goal}"`, '#38bdf8')
    } else {
      // Fallback via Orchestrator REST
      try {
        await fetch(`${API}/mesh/tasks`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ goal, initiator: 'ui' }),
        })
      } catch (err) {
        log?.(`⚠ Swarm dispatch failed: ${err.message}`, '#f87171')
      }
    }
    setLoading(false)
  }

  // Execute Cross-Agent Swarm RAG Retrieval
  const runSwarmRag = async () => {
    if (!ragQuery.trim() || ragLoading) return
    const q = ragQuery.trim()
    setRagLoading(true)
    setRagResults(null)
    try {
      const res = await fetch(`${VAULT}/v1/rag/retrieve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query: q,
          n: 5,
          synthesize: true,
        }),
      })
      if (res.ok) {
        const body = await res.json()
        setRagResults(body)
        log?.(`🔍 [Swarm RAG] Retrieved ${body.hits_count} citations across ${body.agents_represented?.join(', ')}`, '#a78bfa')
      } else {
        log?.(`⚠ Swarm RAG retrieval failed (${res.status})`, '#f87171')
      }
    } catch (err) {
      log?.(`⚠ Swarm RAG error: ${err.message}`, '#f87171')
    } finally {
      setRagLoading(false)
    }
  }

  // Send Direct P2P Mesh Message between agents
  const sendP2pMessage = () => {
    if (!peerMsg.message.trim()) return
    const payload = {
      type: 'SWARM_MESSAGE',
      target: peerMsg.target,
      from: 'crabdeck-ui',
      action: peerMsg.action,
      payload: { question: peerMsg.message.trim(), task: peerMsg.message.trim() },
    }
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(payload))
      setCollabLogs(prev => [
        { t: new Date().toLocaleTimeString(), text: `P2P [UI → ${peerMsg.target}] ${peerMsg.action}: ${peerMsg.message}` },
        ...prev.slice(0, 30)
      ])
      log?.(`🔗 [Mesh P2P] UI → ${peerMsg.target}: ${peerMsg.message}`, '#f59e0b')
      setPeerMsg(prev => ({ ...prev, message: '' }))
    } else {
      log?.('⚠ Gateway WebSocket not connected for P2P mesh message', '#f87171')
    }
  }

  const nodes = mesh?.nodes || []

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden', background: '#030712', color: '#f1f5f9' }}>
      
      {/* Mesh Status Banner */}
      <div style={{ padding: '12px 18px', background: '#0b1329', borderBottom: '1px solid #1e293b', display: 'flex', alignItems: 'center', gap: 16 }}>
        <div>
          <span style={{ fontSize: 18, marginRight: 8 }}>🕸️</span>
          <span style={{ fontWeight: 'bold', color: '#38bdf8', fontSize: 14, letterSpacing: 1 }}>SWARM MESH & COLLABORATIVE RAG</span>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 14, fontSize: 11 }}>
          <span style={{ background: '#1e293b', padding: '3px 8px', borderRadius: 4, color: '#94a3b8' }}>
            Mesh Nodes: <strong style={{ color: '#38bdf8' }}>{mesh?.mesh_size ?? nodes.length}</strong>
          </span>
          <span style={{ background: '#1e293b', padding: '3px 8px', borderRadius: 4, color: '#94a3b8' }}>
            Active Peers: <strong style={{ color: '#4ade80' }}>{mesh?.active_nodes ?? nodes.filter(n => n.status === 'running').length}</strong>
          </span>
          <span style={{ background: '#1e293b', padding: '3px 8px', borderRadius: 4, color: '#94a3b8' }}>
            Protocol: <strong style={{ color: '#c084fc' }}>bHive-Slot + RAG-Sync</strong>
          </span>
        </div>
      </div>

      <div style={{ flex: 1, display: 'grid', gridTemplateColumns: '1fr 1fr', overflow: 'hidden', gap: 12, padding: 14 }}>
        
        {/* Left Column: Swarm Topology & Multi-Agent Coordination */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12, overflowY: 'auto' }}>
          
          {/* Active Mesh Nodes Card */}
          <div style={{ background: '#0a1224', border: '1px solid #1e3a5f', borderRadius: 8, padding: 12 }}>
            <div style={{ fontSize: 11, fontWeight: 'bold', color: '#94a3b8', letterSpacing: 1, marginBottom: 8 }}>
              ACTIVE SWARM PEERS (P2P MESH)
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 8 }}>
              {nodes.length > 0 ? nodes.map((node, i) => (
                <div key={i} style={{ background: '#0f172a', border: '1px solid #334155', borderRadius: 6, padding: '8px 10px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 'bold', fontSize: 12 }}>
                    <span style={{ width: 8, height: 8, borderRadius: '50%', background: node.status === 'running' ? '#4ade80' : '#ef4444' }} />
                    <span style={{ color: '#e2e8f0', textTransform: 'capitalize' }}>{node.agent || node.role}</span>
                  </div>
                  <div style={{ fontSize: 10, color: '#64748b', marginTop: 4 }}>
                    {(node.capabilities || []).slice(0, 2).join(' · ') || 'Mesh Node'}
                  </div>
                </div>
              )) : (
                <div style={{ color: '#64748b', fontSize: 11, fontStyle: 'italic' }}>
                  Connecting peers: Hermes (LLM/RAG), OpenClaw (System), Gateway (Hub), Orchestrator
                </div>
              )}
            </div>
          </div>

          {/* Coordinated Swarm Task Dispatch */}
          <div style={{ background: '#0a1224', border: '1px solid #1e3a5f', borderRadius: 8, padding: 12 }}>
            <div style={{ fontSize: 11, fontWeight: 'bold', color: '#38bdf8', letterSpacing: 1, marginBottom: 6 }}>
              COORDINATED MULTI-AGENT SWARM TASK
            </div>
            <div style={{ fontSize: 11, color: '#64748b', marginBottom: 8 }}>
              Broadcasts a collective mission to all agents in the mesh. Hermes reasons and synthesizes via RAG, while OpenClaw inspects system state.
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <input
                value={swarmGoal}
                onChange={e => setSwarmGoal(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && launchSwarmTask()}
                placeholder="Enter swarm mission (e.g. Audit system health, recall past incidents, and propose fix)..."
                style={{ flex: 1, background: '#0f172a', border: '1px solid #1e3a5f', borderRadius: 6, color: '#f1f5f9', padding: '8px 12px', fontSize: 11 }}
              />
              <button
                onClick={launchSwarmTask}
                disabled={loading}
                style={{ background: '#0284c7', color: '#fff', border: 'none', borderRadius: 6, padding: '8px 16px', cursor: 'pointer', fontSize: 11, fontWeight: 'bold' }}
              >
                {loading ? 'Dispatched...' : '🚀 Launch Swarm'}
              </button>
            </div>
          </div>

          {/* Direct Agent-to-Agent P2P Router */}
          <div style={{ background: '#0a1224', border: '1px solid #1e3a5f', borderRadius: 8, padding: 12 }}>
            <div style={{ fontSize: 11, fontWeight: 'bold', color: '#f59e0b', letterSpacing: 1, marginBottom: 6 }}>
              AGENT-TO-AGENT P2P COLLABORATION
            </div>
            <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
              <select
                value={peerMsg.target}
                onChange={e => setPeerMsg({ ...peerMsg, target: e.target.value })}
                style={{ background: '#0f172a', border: '1px solid #334155', borderRadius: 6, color: '#f59e0b', padding: '6px 8px', fontSize: 11 }}
              >
                <option value="openclaw">Target: OpenClaw</option>
                <option value="hermes">Target: Hermes</option>
                <option value="orchestrator">Target: Orchestrator</option>
              </select>
              <select
                value={peerMsg.action}
                onChange={e => setPeerMsg({ ...peerMsg, action: e.target.value })}
                style={{ background: '#0f172a', border: '1px solid #334155', borderRadius: 6, color: '#94a3b8', padding: '6px 8px', fontSize: 11 }}
              >
                <option value="COLLAB_QUERY">Action: COLLAB_QUERY</option>
                <option value="EXEC_TASK">Action: EXEC_TASK</option>
                <option value="RAG_VERIFY">Action: RAG_VERIFY</option>
              </select>
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <input
                value={peerMsg.message}
                onChange={e => setPeerMsg({ ...peerMsg, message: e.target.value })}
                onKeyDown={e => e.key === 'Enter' && sendP2pMessage()}
                placeholder="Message or query for peer agent..."
                style={{ flex: 1, background: '#0f172a', border: '1px solid #1e3a5f', borderRadius: 6, color: '#f1f5f9', padding: '8px 12px', fontSize: 11 }}
              />
              <button
                onClick={sendP2pMessage}
                style={{ background: '#d97706', color: '#fff', border: 'none', borderRadius: 6, padding: '8px 14px', cursor: 'pointer', fontSize: 11, fontWeight: 'bold' }}
              >
                Send P2P
              </button>
            </div>
          </div>

          {/* Swarm Tasks List */}
          {activeTasks.length > 0 && (
            <div style={{ background: '#0a1224', border: '1px solid #1e3a5f', borderRadius: 8, padding: 12 }}>
              <div style={{ fontSize: 11, fontWeight: 'bold', color: '#94a3b8', letterSpacing: 1, marginBottom: 6 }}>
                ACTIVE / RECENT SWARM MISSIONS
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {activeTasks.map((t, i) => (
                  <div key={i} style={{ background: '#0f172a', border: '1px solid #1e293b', borderRadius: 6, padding: '8px 10px', fontSize: 11 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', color: '#38bdf8' }}>
                      <strong>{t.goal}</strong>
                      <span style={{ color: t.status === 'completed' ? '#4ade80' : '#fbbf24' }}>[{t.status}]</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Right Column: Swarm RAG Tech Retrieval & Cross-Agent Citations */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12, overflowY: 'auto' }}>
          
          <div style={{ background: '#0a1224', border: '1px solid #1e3a5f', borderRadius: 8, padding: 12, flex: 1, display: 'flex', flexDirection: 'column' }}>
            <div style={{ fontSize: 11, fontWeight: 'bold', color: '#a78bfa', letterSpacing: 1, marginBottom: 4 }}>
              SWARM RAG RETRIEVAL & VECTOR CONTEXT
            </div>
            <div style={{ fontSize: 11, color: '#64748b', marginBottom: 8 }}>
              Query the Shell Cracked vector vault with cosine distance across all agent memories (Hermes prompts, OpenClaw task execution, and system events).
            </div>
            
            <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
              <input
                value={ragQuery}
                onChange={e => setRagQuery(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && runSwarmRag()}
                placeholder="Search across all agent memories (e.g. latency, health, errors)..."
                style={{ flex: 1, background: '#0f172a', border: '1px solid #312e81', borderRadius: 6, color: '#f1f5f9', padding: '8px 12px', fontSize: 11 }}
              />
              <button
                onClick={runSwarmRag}
                disabled={ragLoading}
                style={{ background: '#7c3aed', color: '#fff', border: 'none', borderRadius: 6, padding: '8px 16px', cursor: 'pointer', fontSize: 11, fontWeight: 'bold' }}
              >
                {ragLoading ? 'Retrieving...' : '🔍 RAG Search'}
              </button>
            </div>

            {/* RAG Results Display */}
            <div style={{ flex: 1, overflowY: 'auto', background: '#030712', borderRadius: 6, border: '1px solid #1e293b', padding: 10 }}>
              {ragResults ? (
                <div>
                  <div style={{ display: 'flex', gap: 10, marginBottom: 8, fontSize: 11 }}>
                    <span style={{ color: '#4ade80' }}>✓ Hits: {ragResults.hits_count}</span>
                    <span style={{ color: '#c084fc' }}>Agents: {ragResults.agents_represented?.join(', ') || 'none'}</span>
                  </div>

                  {ragResults.synthesis && (
                    <div style={{ background: '#1e1b4b', border: '1px solid #4338ca', borderRadius: 6, padding: '8px 10px', fontSize: 11, color: '#e0e7ff', marginBottom: 10, whiteSpace: 'pre-wrap' }}>
                      <strong style={{ color: '#818cf8', display: 'block', marginBottom: 4 }}>Deterministic Multi-Source RAG Synthesis:</strong>
                      {ragResults.synthesis}
                    </div>
                  )}

                  <div style={{ fontSize: 10, fontWeight: 'bold', color: '#94a3b8', marginBottom: 6 }}>GROUNDED CITATIONS:</div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                    {ragResults.citations?.map((c, idx) => (
                      <div key={idx} style={{ background: '#0f172a', border: '1px solid #1e293b', borderRadius: 6, padding: '6px 8px', fontSize: 11 }}>
                        <div style={{ display: 'flex', gap: 8, color: '#a78bfa', fontWeight: 'bold' }}>
                          <span>{c.source_id}</span>
                          <span style={{ textTransform: 'capitalize' }}>Agent: {c.agent}</span>
                          <span style={{ color: '#64748b' }}>Kind: {c.kind}</span>
                          <span style={{ marginLeft: 'auto', color: '#38bdf8' }}>Score: {c.score}</span>
                        </div>
                        <div style={{ color: '#cbd5e1', marginTop: 3, fontSize: 10, whiteSpace: 'pre-wrap' }}>
                          {c.excerpt}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <div style={{ color: '#475569', fontSize: 11, textAlign: 'center', marginTop: 30 }}>
                  Enter a query above to perform RAG retrieval and synthesis across all swarm memories.
                </div>
              )}
            </div>

          </div>

          {/* P2P Collab Event Stream */}
          {collabLogs.length > 0 && (
            <div style={{ background: '#0a1224', border: '1px solid #1e3a5f', borderRadius: 8, padding: 10, maxHeight: 150, overflowY: 'auto' }}>
              <div style={{ fontSize: 10, fontWeight: 'bold', color: '#f59e0b', marginBottom: 4 }}>P2P COLLABORATION STREAM:</div>
              {collabLogs.map((logItem, i) => (
                <div key={i} style={{ fontSize: 10, color: '#94a3b8', marginBottom: 2 }}>
                  <span style={{ color: '#475569' }}>[{logItem.t}]</span> {logItem.text}
                </div>
              ))}
            </div>
          )}

        </div>

      </div>

    </div>
  )
}
