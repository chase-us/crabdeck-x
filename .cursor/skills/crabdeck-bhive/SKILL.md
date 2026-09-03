---
name: crabdeck-bhive
description: bHive minute-slot heartbeat protocol and 20s watchdog. Use when changing HEARTBEAT payloads, slot math, gateway ACK/metrics, or vault heartbeat ingest.
---

# bHive heartbeat protocol

Two independent safety nets:

1. **Watchdog (20s)** — last WebSocket activity older than 20s → `missed_heartbeat`.
2. **Minute slot** — `floor(unix_seconds / 60)`. Skipping a full slot (`now_slot - last_slot > 1`) → `slot_lag`.

## Instructions

1. Every `HEARTBEAT` frame must include `type`, `agent`, `ts` (unix **seconds**), and `bhive_slot`.
2. Compute the slot from `ts`, not from a second clock, when validating skew.
3. Vault rejects a slot more than **one minute** off `minute_slot(ts)`.
4. Gateway Node `minuteSlot` takes **milliseconds** (`Date.now()`). Python `minute_slot` takes **seconds**. Do not mix units.
5. Allowed agents only: `hermes`, `openclaw`, `orchestrator`, `crabdeck`, `vault`.
6. Vault ingest from the gateway is fail-open (`void ingestHeartbeat(...)`, 1.5s abort). A down vault must not drop the WS ACK.

## Wire format

```json
{
  "type": "HEARTBEAT",
  "agent": "hermes",
  "ts": 1788401915.0,
  "bhive_slot": 29806698
}
```

Gateway replies:

```json
{ "type": "HEARTBEAT_ACK", "ts": 1788401915000, "bhive_slot": 29806698 }
```

`HEARTBEAT_ACK.ts` is JS `Date.now()` (ms). Agent `ts` is seconds. Document both; do not compare them as the same unit.

## Canonical implementations

| Language | Module | Notes |
| -------- | ------ | ----- |
| Python protocol | `vault/bhive.py` | `minute_slot`, `evaluate_agent`, `validate_agent` |
| Python client | `agents/vault_client.py` | `heartbeat_payload`, `emit_heartbeat` |
| Node protocol | `gateway/bhive.js` | `minuteSlot(tsMs)`, `missedWatchdog` |
| Node ingest | `gateway/vault_client.js` | POST `/v1/heartbeat` |

Status priority in `evaluate_agent`: watchdog miss wins over slot lag.

## Examples

**Build a payload (agents)**

```python
from vault_client import heartbeat_payload
payload = heartbeat_payload("hermes", time.time())
await ws.send(json.dumps(payload))
```

**Gateway watchdog tick**

```javascript
if (bhive.missedWatchdog(c.lastSeen, now) && agentStatus[c.role] === 'running') {
  agentStatus[c.role] = 'missed_heartbeat'
}
```

## Performance notes

- Slot id is an integer, not a timestamp string.
- Do not use `Date.now()` inside a Convex query if you later add Convex — that is a different stack. This gateway is Node; `Date.now()` is correct here.
- Tests: `vault/test_vault.py` (slot/watchdog), `gateway/test_bhive.js` (ms math).

## Troubleshooting

| Symptom | Cause |
| ------- | ----- |
| Vault 400 `bhive_slot is more than one minute off` | Client sent wall-clock slot with a stale `ts`, or mixed ms/seconds |
| Agent `slot_lag` but watchdog green | Missed at least one full minute slot; process alive but skipped a beat |
| Metrics `watchdog_miss: true` | No inbound WS frames for > 20s |
