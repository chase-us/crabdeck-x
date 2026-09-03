'use strict'

/**
 * Origin bind config for the CrabDeck gateway.
 *
 * Cloudflare 521 means the edge could not TCP-handshake the origin.
 * Defaulting HOST to 0.0.0.0 (not 127.0.0.1) is required so a reverse
 * proxy or Cloudflare can reach the process from outside the box.
 */

function parsePort(raw, fallback) {
  if (raw === undefined || raw === null || raw === '') {
    return fallback
  }
  const port = Number(raw)
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    throw new Error(`Invalid port: ${raw}`)
  }
  return port
}

function parseOriginPorts(raw) {
  if (raw === undefined || raw === null || String(raw).trim() === '') {
    return []
  }
  const ports = []
  for (const part of String(raw).split(',')) {
    const trimmed = part.trim()
    if (!trimmed) continue
    const port = Number(trimmed)
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error(`Invalid ORIGIN_PORT entry: ${trimmed}`)
    }
    if (!ports.includes(port)) ports.push(port)
  }
  return ports
}

function gatewayBindConfig(env = process.env) {
  if (env === null || typeof env !== 'object') {
    throw new Error('env must be an object')
  }

  const port = parsePort(env.PORT, 8765)
  const host = (env.HOST && String(env.HOST).trim()) || '0.0.0.0'
  const originPorts = parseOriginPorts(env.ORIGIN_PORT).filter((p) => p !== port)

  return {
    port,
    host,
    originPorts,
    listeners: [port, ...originPorts].map((p) => `${host}:${p}`),
  }
}

module.exports = { gatewayBindConfig, parsePort, parseOriginPorts }
