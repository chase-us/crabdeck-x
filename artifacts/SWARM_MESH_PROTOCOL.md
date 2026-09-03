# Swarm mesh protocol

Multi-agent collaboration over the existing gateway bus, grounded by Shell Cracked retrieval (RAG).

Before this, the gateway was hub-and-spoke: UI → one agent → UI. Agents never saw each other and memory was write-only. The mesh turns every connected agent role into a peer: a goal fans out to all of them, each round's contributions are fed to every other peer, and the swarm's result is written back into the vault so the next swarm starts from what the last one learned.

## Roles

| Role | Mesh behaviour |
| ---- | -------------- |
| `hermes` | Contributes every round; **synthesizes** the final answer |
| `openclaw` | Contributes every round (advisory — no `<CMD>` parsing, never executes during a swarm) |
| `orchestrator` | Contributes a deterministic health digest each round (no LLM) |
| `ui` | Starts swarms, watches every frame |

Roster = connected + authenticated clients whose role is in `SWARM_ROLES` (`gateway/swarm.js`). A swarm only includes peers present at start.

## Session lifecycle

```
UI ──SWARM_TASK──▶ gateway
                    │ GET /v1/memory/query?q=<goal>   (RAG seed, fail-open, 1.5s)
                    │ create session, participants = roster
                    ├─SWARM_ROUND r1──▶ hermes, openclaw, orchestrator   (+ UI)
                    │
      peer ──SWARM_CONTRIBUTION──▶ gateway ──SWARM_PEER──▶ other peers
                    │                        └─SWARM_CONTRIBUTION──▶ UI
                    │ when all peers spoke, or ROUND_TIMEOUT elapsed:
                    ├─SWARM_ROUND r2 (contributions of r1 attached)──▶ peers
                    │ …
                    │ after the last round:
                    ├─SWARM_SYNTHESIZE──▶ hermes         (UI gets SWARM_SYNTHESIZING)
      hermes ──SWARM_SYNTHESIS──▶ gateway
                    ├─SWARM_RESULT──▶ UI + peers
                    ├─POST /v1/session  swarm:<id>   (full transcript)
                    └─POST /v1/memory   kind=swarm_result agent=crabdeck
```

State machine: `running → synthesizing → done`, or `failed` when no peer answers round 1. Silent peers are recorded per round (`silent: [...]`); a departed peer that was the last holdout closes the round immediately.

If Hermes is not on the roster (or times out), the gateway finalizes with a transcript digest (`synthesized_by: "gateway"`).

## Frames

### `SWARM_TASK` (UI → gateway)

```json
{ "type": "SWARM_TASK", "payload": { "goal": "plan the v2.3 release", "rounds": 2, "model": "llama3" } }
```

`goal` required, ≤ 4000 chars. `rounds` 1..4 (default 2). `model` optional.

### `SWARM_ROUND` (gateway → peers, UI)

```json
{
  "type": "SWARM_ROUND",
  "payload": {
    "session_id": "…", "goal": "…", "model": "llama3",
    "round": 2, "max_rounds": 2,
    "peers": ["hermes", "openclaw", "orchestrator"],
    "context": [{ "id": "hermes:…", "text": "…", "agent": "hermes", "kind": "prompt_result", "score": 0.85 }],
    "contributions": { "hermes": "…", "openclaw": "…", "orchestrator": "…" }
  }
}
```

`context` is the RAG seed (≤ 6 hits, ≤ 600 chars each, gated by `SWARM_RAG_MIN_SCORE`). `contributions` is the previous round (empty in round 1).

### `SWARM_CONTRIBUTION` (peer → gateway; gateway → UI)

```json
{ "type": "SWARM_CONTRIBUTION", "agent": "openclaw", "payload": { "session_id": "…", "round": 2, "text": "…" } }
```

Text ≤ 6000 chars. Rejected (silently logged) when: not a participant, stale round, duplicate for the round, session not running. `round: null` means "current round".

### `SWARM_PEER` (gateway → other peers)

`{ session_id, round, from, text }` — real-time awareness; the same text arrives again in the next `SWARM_ROUND.contributions`.

### `SWARM_SYNTHESIZE` / `SWARM_SYNTHESIS` (gateway ↔ hermes)

Synthesize payload = round payload plus `transcript: [{ round, agent, text }]`. Reply: `{ session_id, text }`.

### `SWARM_RESULT` (gateway → UI, peers)

```json
{ "session_id", "goal", "status": "done|failed", "peers", "rounds", "result", "synthesized_by",
  "transcript": [{ "round", "agent", "text" }], "context", "error", "started_at", "finished_at" }
```

### `MESH` (peer ↔ peer, addressed)

```json
{ "type": "MESH", "to": "openclaw", "payload": { "intent": "ask", "text": "is shell exec enabled?", "session_id": "…" } }
```

Gateway rewrites to `{ type: "MESH", from: <sender role>, to, payload }`, delivers to `to`, and mirrors a `MESH_TRACE` to the UI. Peers answer `ask` with a generated `tell`; `tell` is stored as `mesh_note` memory. Replies are always `tell`, so two peers cannot loop.

### `MESH_PEERS` (gateway → peers, UI)

`{ peers: [...], bhive_slot }` on every agent join/leave.

## RAG

Two layers, both fail-open:

1. **Gateway seed** — `GET /v1/memory/query?q=<goal>&n=SWARM_RAG_HITS` at session start; attached to every round.
2. **Peer retrieval** — each LLM peer calls `vault_client.retrieve_memory(goal, 5)` off-loop and merges (dedupe by id, rank by score) before prompting. `agents/swarm.py::merge_context`.

Results write back: `swarm_result` (agent `crabdeck`) and `mesh_note`. Default embeddings are blake2 hash similarity, so `SWARM_RAG_MIN_SCORE` defaults to `0`; raise it (e.g. `0.82`) only with a semantic backend.

## Gateway env

| Var | Default | Meaning |
| --- | ------- | ------- |
| `SWARM_RAG_HITS` | 5 | vault hits retrieved per swarm |
| `SWARM_RAG_MIN_SCORE` | 0 | drop hits below this cosine score |
| `SWARM_ROUND_TIMEOUT_MS` | 45000 | close a round without silent peers |

## HTTP

- `GET /health` → includes `swarm: { active, total, peers }`
- `GET /swarm` → session summaries
- `GET /swarm/:id` → full result payload

## Security

- Every swarm frame requires an authenticated client. `SWARM_CONTRIBUTION`/`MESH` require an agent role; `SWARM_SYNTHESIS` requires `hermes`.
- OpenClaw's swarm path is `ollama_generate` (plain completion). `<CMD>` blocks are never parsed in a swarm round or mesh reply, regardless of `ENABLE_SHELL_EXEC`.
- All blocking work (Ollama, vault) runs via `run_blocking`; heartbeats keep the 20s watchdog green during long rounds.

## Tests

- `gateway/test_swarm.js` — roster, normalizers, full 2-round session, timeouts, failure, fallback synthesis, prune
- `agents/test_swarm.py` — payload hardening, RAG prompt shape, off-loop dispatch, mesh ask/tell
- `orchestrator/test_swarm_peer.py` — digest content, handlers, gateway liveness
