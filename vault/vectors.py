"""Shell Cracked vector memory — SQLite cosine index + optional ChromaDB."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from pathlib import Path
from typing import Any, Protocol

EMBED_DIM = 384


def embed_text(text: str) -> list[float]:
    """Offline blake2 embedding (L2-normalized, 384-d). Air-gapped default."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not text.strip():
        raise ValueError("text must be non-empty")
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=64).digest()
    vec: list[float] = []
    while len(vec) < EMBED_DIM:
        for byte in digest:
            vec.append((byte / 127.5) - 1.0)
            if len(vec) == EMBED_DIM:
                break
        digest = hashlib.blake2b(digest, digest_size=64).digest()
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError("embedding length mismatch")
    return sum(x * y for x, y in zip(a, b, strict=True))


class VectorMemory(Protocol):
    def add(self, doc_id: str, text: str, metadata: dict[str, Any]) -> None: ...
    def query(self, text: str, n: int = 5, agent: str | None = None) -> list[dict[str, Any]]: ...
    def count(self) -> int: ...


class SqliteVectorMemory:
    """Always-on vector backend. Same contract as ChromaMemory."""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vectors (
                    id        TEXT PRIMARY KEY,
                    text      TEXT NOT NULL,
                    metadata  TEXT NOT NULL,
                    embedding TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def add(self, doc_id: str, text: str, metadata: dict[str, Any] | None = None) -> None:
        if not isinstance(doc_id, str) or not doc_id.strip():
            raise ValueError("doc_id must be a non-empty string")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        meta = metadata if metadata is not None else {}
        if not isinstance(meta, dict):
            raise TypeError("metadata must be a dict")
        blob = json.dumps(embed_text(text))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO vectors (id, text, metadata, embedding) VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    text=excluded.text, metadata=excluded.metadata, embedding=excluded.embedding
                """,
                (doc_id.strip(), text, json.dumps(meta, default=str), blob),
            )
            self._conn.commit()

    def query(self, text: str, n: int = 5, agent: str | None = None) -> list[dict[str, Any]]:
        if not isinstance(n, int) or n < 1 or n > 50:
            raise ValueError("n must be an int in 1..50")
        target = embed_text(text)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text, metadata, embedding FROM vectors"
            ).fetchall()
        scored: list[dict[str, Any]] = []
        for row in rows:
            meta = json.loads(row["metadata"])
            if agent is not None and str(meta.get("agent", "")).lower() != agent.lower():
                continue
            vec = json.loads(row["embedding"])
            scored.append({
                "id": row["id"],
                "text": row["text"],
                "metadata": meta,
                "score": cosine(target, vec),
            })
        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:n]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM vectors").fetchone()
        return int(row["n"])


class ChromaMemory:
    """ChromaDB persistent collection. Requires `chromadb`."""

    def __init__(self, persist_dir: str | Path) -> None:
        import chromadb
        from chromadb.api.types import EmbeddingFunction, Embeddings

        class BlakeEmbedding(EmbeddingFunction):
            def __call__(self, input: list[str]) -> Embeddings:
                return [embed_text(t) for t in input]

        path = Path(persist_dir)
        path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(path))
        self._col = self._client.get_or_create_collection(
            name="shell_cracked",
            embedding_function=BlakeEmbedding(),
            metadata={"hnsw:space": "cosine"},
        )

    def add(self, doc_id: str, text: str, metadata: dict[str, Any] | None = None) -> None:
        if not isinstance(doc_id, str) or not doc_id.strip():
            raise ValueError("doc_id must be a non-empty string")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        meta = metadata if metadata is not None else {}
        if not isinstance(meta, dict):
            raise TypeError("metadata must be a dict")
        safe_meta = {str(k): (v if isinstance(v, (str, int, float, bool)) else str(v)) for k, v in meta.items()}
        self._col.upsert(ids=[doc_id.strip()], documents=[text], metadatas=[safe_meta])

    def query(self, text: str, n: int = 5, agent: str | None = None) -> list[dict[str, Any]]:
        if not isinstance(n, int) or n < 1 or n > 50:
            raise ValueError("n must be an int in 1..50")
        if self._col.count() == 0:
            return []
        where_filter = {"agent": {"$eq": agent.lower()}} if agent else None
        query_kwargs: dict[str, Any] = {
            "query_texts": [text],
            "n_results": min(n, self._col.count()),
        }
        if where_filter:
            query_kwargs["where"] = where_filter
        result = self._col.query(**query_kwargs)
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        out: list[dict[str, Any]] = []
        for i, doc_id in enumerate(ids):
            dist = dists[i] if i < len(dists) else 1.0
            out.append({
                "id": doc_id,
                "text": docs[i] if i < len(docs) else "",
                "metadata": metas[i] if i < len(metas) else {},
                "score": 1.0 - float(dist),
            })
        return out

    def count(self) -> int:
        return int(self._col.count())


def open_vector_memory(path: str | Path, backend: str = "sqlite") -> VectorMemory:
    if backend not in {"sqlite", "chroma"}:
        raise ValueError("backend must be 'sqlite' or 'chroma'")
    if backend == "chroma":
        return ChromaMemory(path)
    return SqliteVectorMemory(path)
