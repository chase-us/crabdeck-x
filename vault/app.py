"""Shell Cracked FastAPI service — SQLite state + vector memory + bHive."""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bhive import minute_slot, validate_agent
from sqlite_store import SqliteVault
from vectors import VectorMemory, open_vector_memory


class HeartbeatIn(BaseModel):
    agent: str
    ts: float | None = None
    slot: int | None = None
    source: str = "agent"


class MemoryIn(BaseModel):
    agent: str
    kind: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=8000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    context: dict[str, Any]


class RagQueryIn(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    n: int = Field(default=5, ge=1, le=50)
    agent: str | None = None
    min_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    synthesize: bool = False


def create_app(
    store: SqliteVault | None = None,
    vectors: VectorMemory | None = None,
    vault_token: str | None = None,
    allowed_origins: list[str] | None = None,
) -> FastAPI:
    token = VAULT_TOKEN if vault_token is None else vault_token
    origins = ALLOWED_ORIGINS if allowed_origins is None else allowed_origins
    db = store if store is not None else _STORE
    vec = vectors if vectors is not None else _VECTORS

    api = FastAPI(title="Shell Cracked", version="1.0.0")
    api.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    def require_token(authorization: str | None, x_vault_token: str | None) -> None:
        if not token:
            return
        presented = None
        if x_vault_token:
            presented = x_vault_token
        elif authorization and authorization.lower().startswith("bearer "):
            presented = authorization[7:].strip()
        if presented != token:
            raise HTTPException(status_code=401, detail="Invalid vault token")

    @api.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "shell-cracked",
            "vector_backend": VECTOR_BACKEND,
            "vector_count": vec.count(),
            "bhive_slot": minute_slot(time.time()),
            "authRequired": bool(token),
        }

    @api.get("/v1/bhive")
    def bhive_status() -> dict[str, Any]:
        now = time.time()
        return {
            "slot": minute_slot(now),
            "watchdog_seconds": 20,
            "agents": db.list_agents(now),
        }

    @api.post("/v1/heartbeat")
    def post_heartbeat(
        body: HeartbeatIn,
        authorization: str | None = Header(default=None),
        x_vault_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(authorization, x_vault_token)
        try:
            validate_agent(body.agent)
            result = db.record_heartbeat(body.agent, body.ts, body.slot, body.source)
            db.log_event("heartbeat", result, agent=body.agent)
            return result
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/v1/memory")
    def post_memory(
        body: MemoryIn,
        authorization: str | None = Header(default=None),
        x_vault_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(authorization, x_vault_token)
        if not isinstance(body.metadata, dict):
            raise HTTPException(status_code=400, detail="metadata must be a dict")
        try:
            agent = validate_agent(body.agent)
            doc_id = f"{agent}:{uuid.uuid4().hex}"
            meta = {**body.metadata, "agent": agent, "kind": body.kind}
            vec.add(doc_id, body.text, meta)
            event_id = db.log_event(
                "memory",
                {"id": doc_id, "kind": body.kind, "text": body.text[:240]},
                agent=agent,
            )
            return {"id": doc_id, "event_id": event_id}
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/v1/memory/query")
    def query_memory(q: str, n: int = 5, agent: str | None = None) -> dict[str, Any]:
        if not q or not q.strip():
            raise HTTPException(status_code=400, detail="q must be non-empty")
        try:
            hits = vec.query(q.strip(), n=n, agent=agent)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"query": q.strip(), "hits": hits, "agent": agent}

    @api.post("/v1/rag/retrieve")
    def rag_retrieve(
        body: RagQueryIn,
        authorization: str | None = Header(default=None),
        x_vault_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Collaborative Swarm RAG retrieval endpoint.
        Returns ranked documents with citations, metadata, and cross-agent context.
        """
        if not body.query or not body.query.strip():
            raise HTTPException(status_code=400, detail="query must be non-empty")
        try:
            hits = vec.query(body.query.strip(), n=body.n, agent=body.agent)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        filtered_hits = [h for h in hits if h.get("score", 0.0) >= body.min_score]

        # Build structured context block and citations
        citations = []
        contexts = []
        agents_represented = set()
        for idx, hit in enumerate(filtered_hits, 1):
            h_meta = hit.get("metadata", {})
            h_agent = h_meta.get("agent", "unknown")
            agents_represented.add(h_agent)
            h_kind = h_meta.get("kind", "memory")
            h_score = round(float(hit.get("score", 0.0)), 4)
            h_text = hit.get("text", "")

            citation = {
                "source_id": f"[{idx}]",
                "doc_id": hit.get("id"),
                "agent": h_agent,
                "kind": h_kind,
                "score": h_score,
                "excerpt": h_text[:200],
            }
            citations.append(citation)
            contexts.append(f"[{idx}] ({h_agent} / {h_kind}) [relevance: {h_score}]:\n{h_text}")

        context_prompt = "\n\n".join(contexts) if contexts else "(No prior swarm memories matched this query)"

        result: dict[str, Any] = {
            "query": body.query.strip(),
            "hits_count": len(filtered_hits),
            "hits": filtered_hits,
            "citations": citations,
            "agents_represented": sorted(list(agents_represented)),
            "context_prompt": context_prompt,
        }

        if body.synthesize:
            # Deterministic multi-source RAG synthesis for air-gapped / prompt-free extraction
            summary_lines = [f"Retrieved {len(filtered_hits)} memories across {len(agents_represented)} agents:"]
            for c in citations[:3]:
                summary_lines.append(f"- {c['agent']} ({c['kind']}): {c['excerpt'][:120]}...")
            result["synthesis"] = "\n".join(summary_lines)

        return result

    @api.get("/v1/agents")
    def agents() -> dict[str, Any]:
        return {"agents": db.list_agents()}

    @api.get("/v1/heartbeats")
    def heartbeats(agent: str | None = None, limit: int = 50) -> dict[str, Any]:
        try:
            return {"heartbeats": db.recent_heartbeats(agent=agent, limit=limit)}
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/v1/events")
    def events(limit: int = 50) -> dict[str, Any]:
        try:
            return {"events": db.recent_events(limit=limit)}
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/v1/session")
    def post_session(
        body: SessionIn,
        authorization: str | None = Header(default=None),
        x_vault_token: str | None = Header(default=None),
    ) -> dict[str, str]:
        require_token(authorization, x_vault_token)
        try:
            db.upsert_session(body.session_id, body.context)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": body.session_id}

    @api.get("/v1/session/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        row = db.get_session(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="session not found")
        return row

    return api


VAULT_TOKEN = os.environ.get("VAULT_TOKEN") or None
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if o.strip()
]
DATA_DIR = Path(os.environ.get("VAULT_DATA_DIR", os.path.join(os.path.dirname(__file__), "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
VECTOR_BACKEND = os.environ.get("VAULT_VECTOR_BACKEND", "sqlite")
_STORE = SqliteVault(DATA_DIR / "shell_cracked.db")
_VECTORS = open_vector_memory(DATA_DIR / f"vectors-{VECTOR_BACKEND}", VECTOR_BACKEND)
app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "7070")), reload=False)
