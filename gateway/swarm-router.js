/** REST dispatch router for swarm mesh — JSON errors on timeout / offline. */

const express = require('express')
const crypto = require('crypto')

const DISPATCH_TIMEOUT_MS = Number(process.env.SWARM_DISPATCH_TIMEOUT_MS || 120_000)
const pending = new Map()

function createSwarmRouter({ agentStatus, sendToAgent, meshOnlineStatus }) {
  const router = express.Router()

  router.get('/status', (_req, res) => {
    const online = meshOnlineStatus()
    res.json({
      online,
      swarm_coordinator: online.swarm === 'running',
      dispatch_timeout_ms: DISPATCH_TIMEOUT_MS,
      pending_tasks: pending.size,
    })
  })

  router.post('/dispatch', (req, res) => {
    const goal = typeof req.body?.goal === 'string' ? req.body.goal.trim() : ''
    const model = typeof req.body?.model === 'string' ? req.body.model : 'llama3'
    const timeoutMs = Number(req.body?.timeout_ms) || DISPATCH_TIMEOUT_MS

    if (!goal) {
      return res.status(400).json({
        error: 'EMPTY_GOAL',
        message: 'Request body must include a non-empty goal string.',
      })
    }

    const online = meshOnlineStatus()
    if (online.swarm !== 'running') {
      return res.status(503).json({
        error: 'SWARM_OFFLINE',
        message: 'Swarm coordinator is not connected. Start agents/swarm_agent.py.',
        online,
      })
    }

    const taskId = `task-${crypto.randomUUID().slice(0, 10)}`
    const sessionId = req.body?.session_id || `swarm-${Date.now()}`

    const timer = setTimeout(() => {
      const entry = pending.get(taskId)
      if (!entry) return
      pending.delete(taskId)
      if (!entry.res.headersSent) {
        entry.res.status(504).json({
          error: 'DISPATCH_TIMEOUT',
          message: `Swarm did not return within ${timeoutMs}ms`,
          task_id: taskId,
          session_id: sessionId,
        })
      }
    }, timeoutMs)

    pending.set(taskId, { res, timer, started: Date.now() })

    const routed = sendToAgent('swarm', {
      type: 'SWARM_GOAL',
      task_id: taskId,
      session_id: sessionId,
      from: 'api',
      payload: { goal, model, session_id: sessionId },
      reply_via: 'http',
    })

    if (!routed) {
      clearTimeout(timer)
      pending.delete(taskId)
      return res.status(503).json({
        error: 'ROUTING_FAILED',
        message: 'Could not deliver goal to swarm coordinator.',
        online,
      })
    }

    if (req.body?.async === true) {
      clearTimeout(timer)
      pending.delete(taskId)
      return res.status(202).json({
        status: 'accepted',
        task_id: taskId,
        session_id: sessionId,
        hint: 'Subscribe via WebSocket for SWARM_RESULT',
      })
    }

    const entry = pending.get(taskId)
    if (entry) {
      entry.res = res
    }
  })

  function resolveDispatchResult(msg) {
    const taskId = msg.task_id
    if (!taskId || !pending.has(taskId)) return false
    const entry = pending.get(taskId)
    clearTimeout(entry.timer)
    pending.delete(taskId)
    if (entry.res.headersSent) return true
    const payload = msg.payload || {}
    if (payload.error) {
      entry.res.status(422).json({
        error: 'SWARM_FAILED',
        message: String(payload.error),
        task_id: taskId,
        payload,
      })
    } else {
      entry.res.json({
        status: 'ok',
        task_id: taskId,
        session_id: msg.session_id,
        elapsed_ms: Date.now() - entry.started,
        payload,
      })
    }
    return true
  }

  return { router, resolveDispatchResult }
}

module.exports = { createSwarmRouter, DISPATCH_TIMEOUT_MS }
