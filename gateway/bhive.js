module.exports = {
  SLOT_SECONDS: 60,
  WATCHDOG_MS: 20_000,

  minuteSlot(tsMs = Date.now()) {
    if (typeof tsMs !== 'number' || !Number.isFinite(tsMs) || tsMs < 0) {
      throw new Error('tsMs must be a non-negative number')
    }
    return Math.floor(tsMs / 1000 / this.SLOT_SECONDS)
  },

  missedWatchdog(lastSeenMs, nowMs = Date.now()) {
    if (typeof lastSeenMs !== 'number' || typeof nowMs !== 'number') {
      throw new Error('timestamps must be numbers')
    }
    return nowMs - lastSeenMs > this.WATCHDOG_MS
  },
}
