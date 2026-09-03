# bHive protocol

Minute-slot scheduling plus a 20 second WebSocket watchdog.

## Constants

| Name | Value | Where |
| ---- | ----- | ----- |
| `SLOT_SECONDS` | 60 | `vault/bhive.py`, `gateway/bhive.js` |
| `WATCHDOG_SECONDS` | 20 | Python evaluate + Node `WATCHDOG_MS = 20_000` |
| Agent heartbeat period | 10s | Hermes, OpenClaw |
| Orchestrator heartbeat period | 10s | `BHIVE_EVERY` |
| Gateway watchdog poll | 10s | `setInterval` in `server.js` |

## Slot

```
slot = floor(unix_seconds / 60)
```

Node helper takes **milliseconds**: `Math.floor(tsMs / 1000 / 60)`.

## Agent HEARTBEAT

```json
{
  "type": "HEARTBEAT",
  "agent": "hermes",
  "ts": 1788401915.0,
  "bhive_slot": 29806698
}
```

`agent` must be one of: `hermes`, `openclaw`, `orchestrator`, `crabdeck`, `vault`.

## Evaluation

```
if (now - last_seen) > 20s        → missed_heartbeat
else if (now_slot - last_slot) > 1 → slot_lag
else                               → running
```

Watchdog miss wins.

## Vault ingest

`POST /v1/heartbeat` `{ agent, ts?, slot?, source }`.

If `slot` is omitted, the vault computes it from `ts` (or now). If provided, `|slot - minute_slot(ts)| > 1` is 400.

Gateway also forwards HEARTBEAT frames to the vault with `source=gateway` after sending `HEARTBEAT_ACK`. Fail-open.

## Tests

- `vault/test_vault.py` — slot edges, watchdog at 20.0001s, skew reject
- `gateway/test_bhive.js` — ms slot math, 20_001 ms miss, blank agent ingest
