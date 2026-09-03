/** Gateway swarm mesh routing — peer delegation and broadcast. */

const SWARM_AGENTS = new Set(['hermes', 'openclaw', 'orchestrator', 'swarm', 'crabdeck'])

const SWARM_TYPES = new Set([
  'SWARM_GOAL',
  'SWARM_DELEGATE',
  'SWARM_BROADCAST',
  'SWARM_PEER_QUERY',
  'SWARM_PEER_RESPONSE',
  'SWARM_CONTEXT',
  'SWARM_RESULT',
  'SWARM_MESH_STATUS',
  'SWARM_ACK',
])

function isSwarmType(type) {
  return SWARM_TYPES.has(type)
}

function createMeshRouter(clients, agentStatus, sendTo, broadcast) {
  function sendToAgent(role, msg) {
    for (const [, c] of clients) {
      if (c.role === role && c.authed && c.ws.readyState === 1) {
        c.ws.send(JSON.stringify(msg))
        return true
      }
    }
    return false
  }

  function broadcastToMesh(msg, excludeId = null) {
    const raw = JSON.stringify(msg)
    for (const [id, c] of clients) {
      if (id === excludeId) continue
      if (!c.authed) continue
      if (!SWARM_AGENTS.has(c.role) && c.role !== 'ui') continue
      if (c.ws.readyState === 1) c.ws.send(raw)
    }
  }

  function meshOnlineStatus() {
    const online = {}
    for (const role of SWARM_AGENTS) {
      online[role] = agentStatus[role] || 'offline'
    }
    return online
  }

  function handleSwarmMessage(client, ws, msg) {
    const { type } = msg
    const from = client.role

    if (type === 'SWARM_GOAL') {
      const payload = msg.payload || {}
      console.log(`[swarm] goal from ${from}: ${JSON.stringify(payload).slice(0, 100)}`)
      const routed = sendToAgent('swarm', { ...msg, from })
      if (!routed) {
        broadcastToMesh({
          type: 'SWARM_DELEGATE',
          task_id: msg.task_id || `task-${Date.now()}`,
          session_id: msg.session_id || `swarm-${Date.now()}`,
          from,
          payload,
          fallback: true,
        })
      }
      ws.send(JSON.stringify({ type: 'SWARM_ACK', task_id: msg.task_id, status: 'accepted' }))
      return true
    }

    if (type === 'SWARM_DELEGATE') {
      const target = msg.target
      if (!target || !SWARM_AGENTS.has(target)) {
        ws.send(JSON.stringify({ type: 'ERROR', code: 'BAD_TARGET', message: `Unknown swarm target: ${target}` }))
        return true
      }
      console.log(`[swarm] ${from} → ${target} delegate`)
      const routed = sendToAgent(target, msg)
      if (!routed) {
        ws.send(JSON.stringify({
          type: 'SWARM_ACK',
          task_id: msg.task_id,
          status: 'target_offline',
          target,
        }))
      }
      return true
    }

    if (type === 'SWARM_BROADCAST') {
      console.log(`[swarm] broadcast from ${from}`)
      broadcastToMesh({ ...msg, from }, client.id)
      return true
    }

    if (type === 'SWARM_PEER_QUERY') {
      const target = msg.target
      if (target && SWARM_AGENTS.has(target)) {
        sendToAgent(target, msg)
      } else {
        broadcastToMesh(msg, client.id)
      }
      return true
    }

    if (type === 'SWARM_PEER_RESPONSE' || type === 'SWARM_CONTEXT') {
      sendToAgent('swarm', msg)
      sendTo('ui', msg)
      return true
    }

    if (type === 'SWARM_RESULT') {
      console.log(`[swarm] result task=${msg.task_id}`)
      sendTo('ui', msg)
      broadcastToMesh({ type: 'SWARM_CONTEXT', session_id: msg.session_id, payload: msg.payload }, client.id)
      return true
    }

    if (type === 'SWARM_MESH_STATUS') {
      ws.send(JSON.stringify({
        type: 'SWARM_MESH_STATUS',
        agents: [...SWARM_AGENTS],
        online: meshOnlineStatus(),
        ts: Date.now(),
      }))
      return true
    }

    return false
  }

  return { handleSwarmMessage, meshOnlineStatus, broadcastToMesh, sendToAgent }
}

module.exports = {
  SWARM_AGENTS,
  SWARM_TYPES,
  isSwarmType,
  createMeshRouter,
}
