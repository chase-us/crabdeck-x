# Shell Cracked HTTP API

Base URL (local): `http://127.0.0.1:7070`  
Vite proxy: `/vault` → that origin.

Auth on mutating routes when `VAULT_TOKEN` is set:

- `X-Vault-Token: <token>`
- or `Authorization: Bearer <token>`

## GET /health

```json
{
  "status": "ok",
  "service": "shell-cracked",
  "vector_backend": "sqlite",
  "vector_count": 0,
  "bhive_slot": 29806698,
  "authRequired": false
}
```

## GET /v1/bhive

```json
{
  "slot": 29806698,
  "watchdog_seconds": 20,
  "agents": [
    {
      "id": "hermes",
      "stored_status": "running",
      "agent": "hermes",
      "last_seen": 1788401915.0,
      "last_slot": 29806698,
      "now_slot": 29806698,
      "watchdog_miss": false,
      "slot_miss": false,
      "status": "running",
      "updated_at": 1788401915.0
    }
  ]
}
```

## POST /v1/heartbeat

Body: `{ "agent": "hermes", "ts": 1788401915.0, "slot": 29806698, "source": "agent" }`

Success: `{ "agent", "ts", "slot", "status": "running" }`  
Errors: 400 unknown agent / slot skew; 401 bad token.

## POST /v1/memory

Body:

```json
{
  "agent": "hermes",
  "kind": "prompt_result",
  "text": "…",
  "metadata": { "model": "llama3" }
}
```

`kind` 1..64 chars. `text` 1..8000. `metadata` must be an object.

Success: `{ "id": "hermes:<uuid>", "event_id": 1 }`

## GET /v1/memory/query

`?q=swarm+status&n=5`  
Empty/whitespace `q` → 400.  
Response: `{ "query", "hits": [{ "id", "text", "metadata", "score" }] }`

Default embeddings are blake2 hashes (cosine on 384-d L2 vectors). Scores are similarity of hashes, not English semantics.

## Sessions

`POST /v1/session` `{ "session_id", "context": {} }`  
`GET /v1/session/{session_id}` → 404 if missing.

## Lists

- `GET /v1/agents` → `{ "agents": [...] }`
- `GET /v1/heartbeats?agent=&limit=` → `{ "heartbeats": [...] }`
- `GET /v1/events?limit=` → `{ "events": [...] }`
