---
name: crabdeck-swarm-mesh
description: Swarm mesh — multi-agent rounds over the gateway bus with Shell Cracked RAG seeding and peer-to-peer MESH frames. Use when adding a mesh peer, changing round/quorum rules, swarm frames, RAG seeding, or the Swarm UI tab.
---

# CrabDeck swarm mesh

Every connected agent role (`hermes`, `openclaw`, `orchestrator`) is a peer. A `SWARM_TASK` fans out as `SWARM_ROUND`s seeded with vault memory; peers see each other's contributions each round; Hermes synthesizes; the result becomes new memory. Contract: `artifacts/SWARM_MESH_PROTOCOL.md`.

## Instructions

1. **Round/quorum logic lives in `gateway/swarm.js` and is pure.** No sockets, no timers. `server.js` owns I/O and `setTimeout`. Add rules there and cover them in `gateway/test_swarm.js`.
2. **Peer logic lives in `agents/swarm.py`.** Hermes and OpenClaw call `dispatch_swarm_round` / `dispatch_mesh`; Hermes also `dispatch_swarm_synthesize`. Do not fork prompt-building into each agent.
3. **Everything blocking goes through `run_blocking`** (Ollama, vault GET/POST). The 20s watchdog still applies during a round.
4. **Vault calls are fail-open.** `queryMemory` / `retrieve_memory` return `[]`, `ingestMemory` / `upsertSession` return `null`. A down `:7070` must never stall a round or a `HEARTBEAT_ACK`.
5. **OpenClaw never executes in a swarm.** Its swarm/mesh path is the plain `ollama_generate`; `<CMD>` parsing exists only in `handle_task`. Keep it that way.
6. **New mesh role?** Add it to `SWARM_ROLES` (`gateway/swarm.js`), `ALLOWED_AGENTS` (`vault/bhive.py`, `agents/vault_client.py`), `ROLE_BRIEFS` (`agents/swarm.py`), the HELLO role map in `server.js`, and `PEER_STYLE` in `ui/src/Swarm.jsx`.
7. **Payloads are untrusted.** Use `normalizeTask` / `normalizeContribution` / `normalizeMesh` (JS) and `normalize_round` / `normalize_mesh` (Python). Non-dicts are wrapped or dropped; text is clamped (6000 chars).
8. **UI state is a pure reducer** — `reduceSwarm(state, msg)` in `ui/src/Swarm.jsx`; `CrabDeck.jsx` feeds it every frame in `SWARM_TYPES`.

## Frames at a glance

| Frame | Direction | Auth |
| ----- | --------- | ---- |
| `SWARM_TASK` | ui → gateway | any authed client |
| `SWARM_ROUND` | gateway → peers, ui | — |
| `SWARM_CONTRIBUTION` | peer → gateway → ui | agent role, participant |
| `SWARM_PEER` | gateway → other peers | — |
| `SWARM_SYNTHESIZE` / `SWARM_SYNTHESIS` | gateway ↔ hermes | hermes |
| `SWARM_RESULT` | gateway → ui, peers | — |
| `MESH` | peer → gateway → peer (+ `MESH_TRACE` to ui) | agent role |
| `MESH_PEERS` | gateway → peers, ui | — |

## Examples

**Answer a round from a new peer (Python)**

```python
elif mtype == "SWARM_ROUND":
    await dispatch_swarm_round(
        ws, msg, agent="scout", generate=ollama_generate,
        retrieve=retrieve_memory, default_model=DEFAULT_MODEL,
    )
```

**Ask a peer directly**

```python
await ws.send(json.dumps({
    "type": "MESH", "to": "orchestrator",
    "payload": {"intent": "ask", "text": "is openclaw healthy?", "session_id": session_id},
}))
```

**Drive a swarm from a script (ws client as `crabdeck-ui`)**

```json
{ "type": "SWARM_TASK", "payload": { "goal": "harden the gateway", "rounds": 2 } }
```

## Performance notes

- One vault query per swarm at the gateway plus one per LLM peer per round. Hash-embedding query is a full table scan — fine for operator history.
- Rounds close on quorum or `SWARM_ROUND_TIMEOUT_MS` (45s default). Ollama calls up to 120s will be cut off by the timeout; the peer's late contribution is logged as `stale_round`.
- Sessions are in-memory, pruned to 50 (finished first). The vault session `swarm:<id>` is the durable copy.

## Troubleshooting

| Symptom | Fix |
| ------- | --- |
| `NO_SWARM_PEERS` error | No agent has completed HELLO. Check `GATEWAY_TOKEN` matches. |
| Round closes with `silent: [...]` | Peer slower than `SWARM_ROUND_TIMEOUT_MS`, or Ollama down (peer still replies with the error text). |
| `synthesized_by: "gateway"` | Hermes not on roster or timed out; result is the transcript digest. |
| RAG panel shows 0 hits | Vault empty for that goal, or `:7070` down (fail-open). Run one swarm; its result seeds the next. |
| Contribution ignored `stale_round` | Peer answered after the round advanced. Raise the timeout or use a faster model. |
| OpenClaw ran a command during a swarm | It cannot — verify the handler uses `ollama_generate`, not `handle_task`. |
