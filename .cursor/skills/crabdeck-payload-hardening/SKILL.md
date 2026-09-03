---
name: crabdeck-payload-hardening
description: Rigid dict/type validation for CrabDeck tool requests, heartbeats, and vault bodies. Use when adding WS message types, HTTP JSON, or agent payload parsers.
---

# Payload hardening

Ingress is hostile. Clients send lists, blanks, and non-dicts. Normalize or reject before any model or DB call.

## Instructions

1. Never assume `msg` or `msg["payload"]` is a `dict`.
2. `TOOL_REQUEST`: use `_tool_request_payload` in `agents/hermes_agent.py`. Non-dicts become `{tool: "unknown", raw: ...}`. Blank/non-string `tool` → `"unknown"`.
3. `json.dumps` on a payload can raise `TypeError` — fall back to a stringified envelope.
4. Vault Pydantic models: `metadata` and session `context` are `dict`. Lists 422.
5. Agent ids: strip + lowercase + allow-list. Return `False` / HTTP 400 rather than forwarding junk.
6. Heartbeat `ts` must be a number ≥ 0. Slot must be `int` when provided.
7. Do not enable `ENABLE_SHELL_EXEC` to "make a task work."

## TOOL_REQUEST contract

```python
def _tool_request_payload(msg):
    raw = msg.get("payload", {}) if isinstance(msg, dict) else {}
    if not isinstance(raw, dict):
        return {"tool": "unknown", "raw": raw}
    tool = raw.get("tool", "unknown")
    if not isinstance(tool, str) or not tool.strip():
        tool = "unknown"
    return {**raw, "tool": tool}
```

## Examples

| Input payload | Normalized tool |
| ------------- | --------------- |
| `{"tool": "search", "q": "x"}` | `search` |
| `"not-a-dict"` | `unknown` + `raw` |
| `{"tool": "  "}` | `unknown` |
| missing msg | `unknown` |

## Gateway notes

- Parse JSON; drop frames that fail parse.
- `HELLO` role is assigned once. Re-HELLO cannot swap identity.
- Origin check only when an `Origin` header is present (browsers). Agent processes omit it.
- Express JSON body limit is 32kb on HTTP `/health` `/metrics`.

## Troubleshooting

- Tests: `agents/test_event_loop_offload.py` `ToolPayloadTests` and `dispatch_tool_request` with `payload: "not-a-dict"`.
- Vault: `test_app.py` `test_rejects_non_dict_metadata`, `test_rejects_unknown_agent`.
