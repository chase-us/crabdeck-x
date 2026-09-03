const test = require('node:test')
const assert = require('node:assert/strict')
const http = require('node:http')
const { WebSocketServer, WebSocket } = require('ws')

test('Swarm Mesh protocol message routing & task dispatch', async () => {
  // Create an in-memory mock gateway using same routing logic
  const clients = new Map()
  const swarmTasks = new Map()
  const receivedByClaw = []
  const receivedByHermes = []

  const server = http.createServer()
  const wss = new WebSocketServer({ server })

  wss.on('connection', (ws) => {
    let role = 'unknown'
    ws.on('message', (raw) => {
      const msg = JSON.parse(raw)
      if (msg.type === 'HELLO') {
        role = msg.client
        clients.set(role, ws)
        ws.send(JSON.stringify({ type: 'ACK', role }))
      } else if (msg.type === 'SWARM_MESSAGE') {
        const target = clients.get(msg.target)
        if (target && target.readyState === WebSocket.OPEN) {
          target.send(JSON.stringify({ type: 'SWARM_MESSAGE', from: role, action: msg.action, payload: msg.payload }))
        }
      } else if (msg.type === 'SWARM_COORDINATE') {
        swarmTasks.set(msg.taskId, { goal: msg.goal, results: {} })
        for (const [, c] of clients) {
          c.send(JSON.stringify({ type: 'SWARM_TASK_DISPATCH', taskId: msg.taskId, goal: msg.goal }))
        }
      } else if (msg.type === 'SWARM_TASK_CONTRIBUTION') {
        const t = swarmTasks.get(msg.taskId)
        if (t) {
          t.results[msg.agent] = msg.contribution
          for (const [, c] of clients) {
            c.send(JSON.stringify({ type: 'SWARM_TASK_UPDATE', taskId: msg.taskId, agent: msg.agent, contribution: msg.contribution }))
          }
        }
      }
    })
  })

  await new Promise(resolve => server.listen(0, resolve))
  const port = server.address().port

  // Connect Hermes client
  const hermesWs = new WebSocket(`ws://localhost:${port}`)
  await new Promise(resolve => hermesWs.on('open', resolve))
  hermesWs.send(JSON.stringify({ type: 'HELLO', client: 'hermes' }))
  await new Promise(resolve => hermesWs.once('message', resolve))

  // Connect OpenClaw client
  const clawWs = new WebSocket(`ws://localhost:${port}`)
  await new Promise(resolve => clawWs.on('open', resolve))
  clawWs.send(JSON.stringify({ type: 'HELLO', client: 'openclaw' }))
  await new Promise(resolve => clawWs.once('message', resolve))

  hermesWs.on('message', (data) => {
    receivedByHermes.push(JSON.parse(data))
  })
  clawWs.on('message', (data) => {
    receivedByClaw.push(JSON.parse(data))
  })

  // Test 1: P2P Swarm Message (Hermes -> OpenClaw)
  hermesWs.send(JSON.stringify({
    type: 'SWARM_MESSAGE',
    target: 'openclaw',
    action: 'RAG_COLLAB_QUERY',
    payload: { question: 'What is the system disk status?' },
  }))

  await new Promise(resolve => setTimeout(resolve, 50))
  assert.equal(receivedByClaw.length, 1)
  assert.equal(receivedByClaw[0].type, 'SWARM_MESSAGE')
  assert.equal(receivedByClaw[0].from, 'hermes')
  assert.equal(receivedByClaw[0].action, 'RAG_COLLAB_QUERY')

  // Test 2: Coordinated Swarm Task Dispatch
  hermesWs.send(JSON.stringify({
    type: 'SWARM_COORDINATE',
    taskId: 'task-mesh-1',
    goal: 'Analyze system health and synthesize documentation',
  }))

  await new Promise(resolve => setTimeout(resolve, 50))
  const clawDispatch = receivedByClaw.find(m => m.type === 'SWARM_TASK_DISPATCH')
  const hermesDispatch = receivedByHermes.find(m => m.type === 'SWARM_TASK_DISPATCH')
  assert.ok(clawDispatch)
  assert.ok(hermesDispatch)
  assert.equal(clawDispatch.taskId, 'task-mesh-1')

  // Test 3: Task Contribution
  clawWs.send(JSON.stringify({
    type: 'SWARM_TASK_CONTRIBUTION',
    taskId: 'task-mesh-1',
    agent: 'openclaw',
    contribution: 'Disk and memory verified normal',
  }))

  await new Promise(resolve => setTimeout(resolve, 50))
  const hermesUpdate = receivedByHermes.find(m => m.type === 'SWARM_TASK_UPDATE')
  assert.ok(hermesUpdate)
  assert.equal(hermesUpdate.agent, 'openclaw')
  assert.equal(hermesUpdate.contribution, 'Disk and memory verified normal')

  hermesWs.close()
  clawWs.close()
  wss.close()
  server.close()
})
