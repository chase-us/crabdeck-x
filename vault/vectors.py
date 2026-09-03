"""Shell Cracked vector memory — SQLite cosine index + optional ChromaDB.

Every row records the embedding *space* that produced it (`hash-v1`,
`ollama:<model>`, legacy `blake2-v1`). Cosine is only meaningful inside one
space, so `query` re-embeds any row left in a stale space before scoring.
That makes an embedder swap — including the v2.2 `blake2-v1` default — a
transparent upgrade instead of a silently broken index.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Protocol

from embeddings import (
    EMBED_DIM,
    Blake2Embedder,
    Embedder,
    EmbeddingError,
    HashingEmbedder,
    blake2_embed,
    cosine,
    hashing_embed,
    open_embedder,
)

LEGACY_SPACE = "blake2-v1"
# Cap the per-query self-heal so a large stale index degrades latency
# gradually instead of stalling one unlucky request.
MIGRATE_BATCH = 256

__all__ = [
    "EMBED_DIM",
    "ChromaMemory",
    "SqliteVectorMemory",
    "VectorMemory",
    "blake2_embed",
    "cosine",
    "embed_text",
    "hashing_embed",
    "open_vector_memory",
]


def embed_text(text: str) -> list[float]:
    """Default encoder (`hash-v1`): lexical, offline, 384-d, L2-normalized."""
    return hashing_embed(text)


class VectorMemory(Protocol):
    def add(self, doc_id: str, text: str, metadata: dict[str, Any]) -> None: ...
    def query(self, text: str, n: int = 5) -> list[dict[str, Any]]: ...
    def count(self) -> int: ...


class SqliteVectorMemory:
    """Always-on vector backend. Same contract as ChromaMemory."""

    def __init__(self, db_path: str | Path, embedder: Embedder | None = None) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._embedder = embedder if embedder is not None else HashingEmbedder()
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
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(vectors)")}
            if "space" not in columns:
                # Pre-existing rows can only have come from the v2.2 default.
                self._conn.execute(
                    f"ALTER TABLE vectors ADD COLUMN space TEXT NOT NULL DEFAULT '{LEGACY_SPACE}'"
                )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_vectors_space ON vectors(space)")
            self._conn.commit()

    @property
    def space(self) -> str:
        return self._embedder.id

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
        blob = json.dumps(self._embedder.embed(text))
        space = self._embedder.id
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO vectors (id, text, metadata, embedding, space)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    text=excluded.text, metadata=excluded.metadata,
                    embedding=excluded.embedding, space=excluded.space
                """,
                (doc_id.strip(), text, json.dumps(meta, default=str), blob, space),
            )
            self._conn.commit()

    def _reembed_stale(self, space: str) -> int:
        """Move rows from other spaces into `space`. Returns rows healed."""
        with self._lock:
            stale = self._conn.execute(
                "SELECT id, text FROM vectors WHERE space != ? LIMIT ?",
                (space, MIGRATE_BATCH),
            ).fetchall()
        if not stale:
            return 0
        updates: list[tuple[str, str, str]] = []
        for row in stale:
            try:
                updates.append((json.dumps(self._embedder.embed(row["text"])), space, row["id"]))
            except (EmbeddingError, TypeError, ValueError):
                # Unencodable text (e.g. punctuation-only) would retry forever.
                # Park it in a terminal space so it is skipped, not re-scanned.
                updates.append((json.dumps([0.0] * EMBED_DIM), f"{space}:unindexable", row["id"]))
        with self._lock:
            self._conn.executemany(
                "UPDATE vectors SET embedding = ?, space = ? WHERE id = ?", updates
            )
            self._conn.commit()
        return len(updates)

    def query(self, text: str, n: int = 5) -> list[dict[str, Any]]:
        if not isinstance(n, int) or n < 1 or n > 50:
            raise ValueError("n must be an int in 1..50")
        space = self._embedder.id
        target = self._embedder.embed(text)
        # embed() may reveal a fallback space (Ollama down) — re-read it.
        space = self._embedder.id
        self._reembed_stale(space)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text, metadata, embedding FROM vectors WHERE space = ?",
                (space,),
            ).fetchall()
        scored: list[dict[str, Any]] = []
        for row in rows:
            vec = json.loads(row["embedding"])
            if len(vec) != len(target):
                continue
            scored.append({
                "id": row["id"],
                "text": row["text"],
                "metadata": json.loads(row["metadata"]),
                "score": cosine(target, vec),
            })
        scored.sort(key=lambda item: (-item["score"], item["id"]))
        return scored[:n]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM vectors").fetchone()
        return int(row["n"])


class ChromaMemory:
    """ChromaDB persistent collection. Requires `chromadb`."""

    def __init__(self, persist_dir: str | Path, embedder: Embedder | None = None) -> None:
        import chromadb
        from chromadb.api.types import EmbeddingFunction, Embeddings

        active = embedder if embedder is not None else HashingEmbedder()

        class VaultEmbedding(EmbeddingFunction):
            def __call__(self, input: list[str]) -> Embeddings:
                return [active.embed(t) for t in input]

        path = Path(persist_dir)
        path.mkdir(parents=True, exist_ok=True)
        self._embedder = active
        self._client = chromadb.PersistentClient(path=str(path))
        # Collection name carries the space: swapping embedders starts a clean
        # index instead of mixing incompatible vectors under one name.
        safe_space = "".join(c if c.isalnum() else "_" for c in active.id)
        self._col = self._client.get_or_create_collection(
            name=f"shell_cracked_{safe_space}",
            embedding_function=VaultEmbedding(),
            metadata={"hnsw:space": "cosine"},
        )

    @property
    def space(self) -> str:
        return self._embedder.id

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

    def query(self, text: str, n: int = 5) -> list[dict[str, Any]]:
        if not isinstance(n, int) or n < 1 or n > 50:
            raise ValueError("n must be an int in 1..50")
        if self._col.count() == 0:
            return []
        result = self._col.query(query_texts=[text], n_results=min(n, self._col.count()))
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


def open_vector_memory(
    path: str | Path,
    backend: str = "sqlite",
    embedder: str | Embedder = "hash",
) -> VectorMemory:
    if backend not in {"sqlite", "chroma"}:
        raise ValueError("backend must be 'sqlite' or 'chroma'")
    encoder = embedder if not isinstance(embedder, str) else open_embedder(embedder)
    if backend == "chroma":
        return ChromaMemory(path, embedder=encoder)
    return SqliteVectorMemory(path, embedder=encoder)


def legacy_embedder() -> Embedder:
    """v2.2 `blake2-v1` encoder, for reading an un-migrated index as-is."""
    return Blake2Embedder()
