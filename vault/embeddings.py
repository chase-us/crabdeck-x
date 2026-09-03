"""Text encoders for Shell Cracked retrieval.

Three embedding spaces, each self-identifying so vectors from different
encoders are never scored against each other:

| id                     | semantic? | deps    | use |
| ---------------------- | --------- | ------- | --- |
| `hash-v1`              | lexical   | none    | default — offline retrieval |
| `ollama:<model>`       | yes       | Ollama  | best recall, needs :11434 |
| `blake2-v1`            | no        | none    | legacy v2.2 rows only |

`blake2-v1` digests the *whole* string, so "swarm mesh routing" and "how does
mesh routing work" land in unrelated directions — cosine over it is noise.
It stays only so pre-existing `vault/data/vectors-*` rows keep parsing; the
vector store re-embeds them into the active space on read.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from typing import Protocol

EMBED_DIM = 384
BIGRAM_WEIGHT = 0.55
MAX_TOKENS = 4000

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+.#-]*")

# Trimmed to genuine function words. Retrieval terms an operator would
# actually search for ("status", "error", "shell") are deliberately kept.
STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "did", "do",
    "does", "for", "from", "had", "has", "have", "he", "her", "him", "his", "i",
    "if", "in", "into", "is", "it", "its", "me", "my", "of", "on", "or", "our",
    "she", "so", "than", "that", "the", "their", "them", "then", "there",
    "these", "they", "this", "to", "was", "we", "were", "what", "when", "which",
    "who", "will", "with", "would", "you", "your",
})


class EmbeddingError(RuntimeError):
    """Raised when an embedding backend cannot encode text."""


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, function words dropped."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    tokens = [t for t in _TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]
    return tokens[:MAX_TOKENS]


def _bucket(token: str) -> tuple[int, float]:
    """Map a token to a dimension and a sign.

    The sign is the hashing trick's collision defence: two unrelated tokens
    sharing a bucket cancel out on average instead of always reinforcing.
    """
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % EMBED_DIM, 1.0 if (value >> 63) & 1 else -1.0


def hashing_embed(text: str) -> list[float]:
    """Signed hashing vectorizer over unigrams + adjacent bigrams.

    Cosine between two of these is weighted term overlap, so paraphrases with
    shared vocabulary actually rank above unrelated text — the property RAG
    retrieval needs and `blake2-v1` never had.
    """
    tokens = tokenize(text)
    if not tokens:
        raise ValueError("text must contain at least one indexable token")

    counts: Counter[tuple[str, float]] = Counter()
    for token in tokens:
        counts[(token, 1.0)] += 1
    for left, right in zip(tokens, tokens[1:]):
        counts[(f"{left}_{right}", BIGRAM_WEIGHT)] += 1

    vec = [0.0] * EMBED_DIM
    for (token, weight), tf in counts.items():
        index, sign = _bucket(token)
        # Sublinear tf: a term repeated 20x should not swamp the vector.
        vec[index] += sign * weight * (1.0 + math.log(tf))

    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        raise ValueError("text embedded to the zero vector")
    return [x / norm for x in vec]


def blake2_embed(text: str) -> list[float]:
    """Legacy v2.2 whole-string digest. Not semantic — read the module docstring."""
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


class Embedder(Protocol):
    id: str
    dim: int

    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    id = "hash-v1"
    dim = EMBED_DIM

    def embed(self, text: str) -> list[float]:
        return hashing_embed(text)


class Blake2Embedder:
    id = "blake2-v1"
    dim = EMBED_DIM

    def embed(self, text: str) -> list[float]:
        return blake2_embed(text)


class OllamaEmbedder:
    """True semantic vectors from a local Ollama embedding model.

    Raises `EmbeddingError` rather than silently degrading: a fallback vector
    written into this space would be scored against real ones and rank
    arbitrarily. `open_embedder` wraps this so callers get a clean fallback
    *space*, not a corrupted one.
    """

    def __init__(
        self,
        model: str = "nomic-embed-text",
        url: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        self.model = model.strip()
        self.id = f"ollama:{self.model}"
        self.dim = 0  # learned from the first successful response
        self._url = (url or os.environ.get("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self._timeout = float(timeout)

    def _post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        req = urllib.request.Request(
            f"{self._url}{path}",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
        if not isinstance(parsed, dict):
            raise EmbeddingError("ollama returned a non-object response")
        return parsed

    def embed(self, text: str) -> list[float]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        try:
            # /api/embed is current; /api/embeddings is the pre-0.3 shape.
            body = self._post("/api/embed", {"model": self.model, "input": text})
            raw = body.get("embeddings")
            vector = raw[0] if isinstance(raw, list) and raw else None
            if vector is None:
                body = self._post("/api/embeddings", {"model": self.model, "prompt": text})
                vector = body.get("embedding")
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise EmbeddingError(f"ollama embed failed: {exc}") from exc

        if not isinstance(vector, list) or not vector:
            raise EmbeddingError("ollama returned no embedding")
        try:
            floats = [float(x) for x in vector]
        except (TypeError, ValueError) as exc:
            raise EmbeddingError("ollama embedding was not numeric") from exc

        norm = math.sqrt(sum(x * x for x in floats))
        if norm == 0.0:
            raise EmbeddingError("ollama returned the zero vector")
        self.dim = len(floats)
        return [x / norm for x in floats]


class FallbackEmbedder:
    """Try `primary`, fall back to `secondary`, and report which space was used.

    Each vector is tagged with the id of the encoder that produced it, so a
    transient Ollama outage parks those rows in `hash-v1` instead of poisoning
    the semantic space. The store re-embeds mismatched rows on read.
    """

    def __init__(self, primary: Embedder, secondary: Embedder) -> None:
        self._primary = primary
        self._secondary = secondary
        self.id = primary.id
        self.dim = primary.dim
        self.degraded = False

    def embed(self, text: str) -> list[float]:
        try:
            vector = self._primary.embed(text)
        except EmbeddingError:
            self.degraded = True
            self.id = self._secondary.id
            self.dim = self._secondary.dim
            return self._secondary.embed(text)
        self.degraded = False
        self.id = self._primary.id
        self.dim = len(vector)
        return vector


def open_embedder(name: str = "hash", **kwargs: object) -> Embedder:
    """Resolve an embedder by name: `hash`, `blake2`, or `ollama`."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    key = name.strip().lower()
    if key in {"hash", "hashing", "hash-v1"}:
        return HashingEmbedder()
    if key in {"blake2", "blake2-v1", "legacy"}:
        return Blake2Embedder()
    if key == "ollama":
        model = kwargs.get("model") or os.environ.get("VAULT_EMBED_MODEL", "nomic-embed-text")
        url = kwargs.get("url")
        return FallbackEmbedder(
            OllamaEmbedder(model=str(model), url=str(url) if url else None),
            HashingEmbedder(),
        )
    raise ValueError("name must be 'hash', 'blake2', or 'ollama'")
