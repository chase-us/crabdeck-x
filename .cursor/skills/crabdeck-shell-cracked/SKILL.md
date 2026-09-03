---
name: crabdeck-shell-cracked
description: Shell Cracked memory vault — SQLite WAL state plus vector history (SQLite or Chroma). Use when adding vault routes, changing embeddings, sessions, or VAULT_* env.
---

# Shell Cracked vault

FastAPI service on **:7070**. Persistent agent state and vector memory so a process restart does not wipe swarm context.

## Instructions

1. Keep documents relational and flat (`agents`, `heartbeats`, `events`, `sessions`, `vectors`). No nested unbounded arrays in SQLite rows.
2. Default vector backend is **sqlite** (blake2 384-d L2 embeddings). Chroma is opt-in (`VAULT_VECTOR_BACKEND=chroma` + `pip install chromadb`).
3. Hash embeddings are **not** semantic. Tests that expect ranking must query the **exact stored text**, not a paraphrase.
4. Public routes that mutate require `VAULT_TOKEN` when set (`X-Vault-Token` or `Bearer`).
5. Validate agent ids with `validate_agent`. Do not invent swarm members.
   Memory `kind`s in use: `prompt_result` (hermes), `task_result` (openclaw), `swarm_result` (gateway writes as `crabdeck`), `mesh_note` (peer-to-peer tells). Swarm transcripts persist as sessions `swarm:<id>`.
6. Tests inject `SqliteVault` + `SqliteVectorMemory` into `create_app(store=..., vectors=..., vault_token=...)`. Do not point tests at `vault/data/`.
7. `vault/data/` is gitignored.

## HTTP surface

| Method | Path | Auth | Notes |
| ------ | ---- | ---- | ----- |
| GET | `/health` | no | `service=shell-cracked`, `bhive_slot`, `vector_count` |
| GET | `/v1/bhive` | no | current slot + evaluated agents |
| POST | `/v1/heartbeat` | token if set | `{agent, ts?, slot?, source}` |
| POST | `/v1/memory` | token if set | `{agent, kind, text≤8000, metadata dict}` |
| GET | `/v1/memory/query?q=&n=` | no | `n` in 1..50 |
| GET | `/v1/agents` | no | `{agents:[...]}` |
| GET | `/v1/heartbeats` | no | `{heartbeats:[...]}` |
| GET | `/v1/events` | no | `{events:[...]}` |
| POST | `/v1/session` | token if set | `{session_id, context dict}` |
| GET | `/v1/session/{id}` | no | 404 if missing |

## Examples

**Ingest from an agent (off the loop)**

```python
await run_blocking(
    emit_memory,
    "hermes",
    "prompt_result",
    f"{prompt[:1200]}\n---\n{reply[:4000]}",
    {"model": model},
)
```

`emit_memory` / `emit_heartbeat` return `False` on network/type failure. Dispatch must still succeed.

**Factory for tests**

```python
app = create_app(store=SqliteVault(tmp / "t.db"), vectors=SqliteVectorMemory(tmp / "v.db"), vault_token=None)
```

## Performance notes

- SQLite connections use WAL + a mutex (`check_same_thread=False`).
- Vector query is a full scan of the sqlite embedding table — fine for bounded operator history, not a million-doc corpus. Move to Chroma when that limit is real.
- Module import still constructs the default `vault/data` store for `uvicorn app:app`. Tests should not rely on that instance.

## Troubleshooting

| Symptom | Fix |
| ------- | --- |
| 401 on POST | Send `X-Vault-Token` matching `VAULT_TOKEN` |
| 400 unknown agent | Id not in `ALLOWED_AGENTS` |
| 422 on memory | `metadata` must be a JSON object |
| Vector test ranks the wrong id | Query the exact document string, not a synonym |
| UI telemetry vault offline | Vite `/vault` proxy → `:7070`; start `uvicorn app:app --port 7070` |
