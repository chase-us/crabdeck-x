'use strict'

const crypto = require('crypto')

const MAX_TASK_CHARS = 8_000
const MAX_RESULT_CHARS = 12_000

function normalizeTask(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new TypeError('SWARM_TASK payload must be an object')
  }
  const task = payload.task
  if (typeof task !== 'string' || !task.trim() || task.length > MAX_TASK_CHARS) {
    throw new RangeError(`task must be a non-empty string up to ${MAX_TASK_CHARS} characters`)
  }
  const model = typeof payload.model === 'string' && payload.model.trim()
    ? payload.model.trim()
    : undefined
  return { task: task.trim(), model }
}

function normalizeResult(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new TypeError('SWARM_RESULT payload must be an object')
  }
  if (typeof payload.taskId !== 'string' || !payload.taskId.trim()) {
    throw new TypeError('SWARM_RESULT taskId must be a non-empty string')
  }
  if (typeof payload.result !== 'string' || !payload.result.trim() || payload.result.length > MAX_RESULT_CHARS) {
    throw new RangeError(`result must be a non-empty string up to ${MAX_RESULT_CHARS} characters`)
  }
  return { taskId: payload.taskId, result: payload.result.trim() }
}

class SwarmMesh {
  constructor() {
    this.tasks = new Map()
  }

  create(payload, members) {
    const { task, model } = normalizeTask(payload)
    const participants = [...new Set(members)].filter(member => typeof member === 'string' && member)
    if (participants.length < 2) {
      throw new Error('A swarm requires at least two active agents')
    }
    const taskId = crypto.randomUUID()
    const record = {
      taskId,
      task,
      model,
      participants,
      results: new Map(),
      createdAt: Date.now(),
    }
    this.tasks.set(taskId, record)
    return record
  }

  submit(agent, payload) {
    const { taskId, result } = normalizeResult(payload)
    const record = this.tasks.get(taskId)
    if (!record) throw new Error('Unknown swarm task')
    if (!record.participants.includes(agent)) throw new Error('Agent is not assigned to this swarm task')
    if (record.results.has(agent)) throw new Error('Agent already submitted a result for this swarm task')
    record.results.set(agent, result)
    const complete = record.results.size === record.participants.length
    return {
      taskId,
      agent,
      result,
      model: record.model,
      complete,
      pending: record.participants.filter(member => !record.results.has(member)),
      results: Object.fromEntries(record.results),
    }
  }

  finish(taskId) {
    const record = this.tasks.get(taskId)
    if (!record) return
    this.tasks.delete(taskId)
  }
}

module.exports = { SwarmMesh, normalizeResult, normalizeTask }
