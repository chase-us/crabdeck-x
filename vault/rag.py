"""Retrieval-augmented generation pipeline for the CrabDeck swarm.

Retrieve → dedupe → diversify (MMR) → budget → cite → augment.

Every stage is a pure function so the swarm's grounding behaviour is unit
testable without a live vault, an LLM, or a network.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from embeddings import Embedder, EmbeddingError, HashingEmbedder, cosine, tokenize

CHUNK_SIZE = 700
CHUNK_OVERLAP = 120
MAX_CHUNKS = 64
CONTEXT_BUDGET = 4000
MMR_LAMBDA = 0.7
NEAR_DUPLICATE = 0.92

_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def chunk_text(
    text: str,
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
    max_chunks: int = MAX_CHUNKS,
) -> list[str]:
    """Split text into retrievable passages on natural boundaries.

    Whole-document vectors average away the one paragraph that answers the
    question, so ingest indexes passages instead. Splits prefer paragraph
    breaks, then sentence breaks, and only cut mid-sentence for text that
    offers neither.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not isinstance(size, int) or size < 80:
        raise ValueError("size must be an int >= 80")
    if not isinstance(overlap, int) or overlap < 0 or overlap >= size:
        raise ValueError("overlap must be an int in 0..size-1")
    if not isinstance(max_chunks, int) or max_chunks < 1:
        raise ValueError("max_chunks must be a positive int")

    body = text.strip()
    if not body:
        return []
    if len(body) <= size:
        return [body]

    units: list[str] = []
    for paragraph in _PARAGRAPH_RE.split(body):
        para = paragraph.strip()
        if not para:
            continue
        if len(para) <= size:
            units.append(para)
            continue
        for sentence in _SENTENCE_RE.split(para):
            sent = sentence.strip()
            if not sent:
                continue
            if len(sent) <= size:
                units.append(sent)
            else:
                for i in range(0, len(sent), size):
                    units.append(sent[i:i + size])

    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if len(candidate) <= size:
            current = candidate
            continue
        if current:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n\n{unit}".strip() if tail else unit
        else:
            current = unit
        if len(chunks) >= max_chunks:
            return chunks[:max_chunks]
    if current:
        chunks.append(current)
    return chunks[:max_chunks]


def token_overlap(a: str, b: str) -> float:
    """Jaccard overlap of token sets. 0.0 when either side has no tokens."""
    left, right = set(tokenize(a)), set(tokenize(b))
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def dedupe_hits(hits: Iterable[dict[str, Any]], threshold: float = NEAR_DUPLICATE) -> list[dict[str, Any]]:
    """Drop near-identical passages, keeping the highest scoring copy.

    Overlapping chunks and repeated agent results otherwise spend the whole
    context budget restating one fact.
    """
    if not isinstance(threshold, (int, float)) or not 0.0 < threshold <= 1.0:
        raise ValueError("threshold must be in (0, 1]")
    kept: list[dict[str, Any]] = []
    for hit in sorted(_valid_hits(hits), key=lambda h: -float(h.get("score", 0.0))):
        text = str(hit.get("text", ""))
        if any(token_overlap(text, str(k.get("text", ""))) >= threshold for k in kept):
            continue
        kept.append(hit)
    return kept


def _valid_hits(hits: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only dict hits carrying non-empty text. Vault output is untrusted."""
    out: list[dict[str, Any]] = []
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        text = hit.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        out.append(hit)
    return out


def mmr_select(
    hits: Iterable[dict[str, Any]],
    k: int = 4,
    lambda_: float = MMR_LAMBDA,
    embedder: Embedder | None = None,
) -> list[dict[str, Any]]:
    """Maximal Marginal Relevance: relevance minus redundancy.

    Pure top-k retrieval returns k paraphrases of the same passage. MMR
    trades a little relevance for coverage, which matters more when several
    swarm peers are reasoning over the same context block.
    """
    if not isinstance(k, int) or k < 1:
        raise ValueError("k must be a positive int")
    if not isinstance(lambda_, (int, float)) or not 0.0 <= lambda_ <= 1.0:
        raise ValueError("lambda_ must be in 0..1")

    pool = sorted(_valid_hits(hits), key=lambda h: -float(h.get("score", 0.0)))
    if len(pool) <= 1:
        return pool[:k]

    encoder = embedder if embedder is not None else HashingEmbedder()
    vectors: dict[int, list[float]] = {}
    for i, hit in enumerate(pool):
        try:
            vectors[i] = encoder.embed(str(hit["text"]))
        except (EmbeddingError, TypeError, ValueError):
            continue

    selected: list[int] = []
    remaining = list(range(len(pool)))
    while remaining and len(selected) < k:
        best_index, best_value = remaining[0], float("-inf")
        for i in remaining:
            relevance = float(pool[i].get("score", 0.0))
            redundancy = 0.0
            if selected and i in vectors:
                sims = [
                    cosine(vectors[i], vectors[j])
                    for j in selected
                    if j in vectors and len(vectors[j]) == len(vectors[i])
                ]
                redundancy = max(sims) if sims else 0.0
            value = lambda_ * relevance - (1.0 - lambda_) * redundancy
            if value > best_value:
                best_index, best_value = i, value
        selected.append(best_index)
        remaining.remove(best_index)
    return [pool[i] for i in selected]


def build_context(
    hits: Iterable[dict[str, Any]],
    budget: int = CONTEXT_BUDGET,
) -> tuple[str, list[dict[str, Any]]]:
    """Render numbered, attributed passages that fit inside `budget` chars."""
    if not isinstance(budget, int) or budget < 200:
        raise ValueError("budget must be an int >= 200")

    blocks: list[str] = []
    citations: list[dict[str, Any]] = []
    used = 0
    for hit in _valid_hits(hits):
        meta = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
        agent = str(meta.get("agent", "unknown"))
        kind = str(meta.get("kind", "memory"))
        index = len(citations) + 1
        header = f"[{index}] {agent}/{kind}"
        text = str(hit["text"]).strip()
        room = budget - used - len(header) - 2
        if room < 120:
            break
        if len(text) > room:
            text = f"{text[:room].rstrip()}…"
        block = f"{header}\n{text}"
        blocks.append(block)
        used += len(block) + 2
        citations.append({
            "n": index,
            "id": hit.get("id"),
            "agent": agent,
            "kind": kind,
            "score": round(float(hit.get("score", 0.0)), 4),
        })
    return "\n\n".join(blocks), citations


GROUNDING_RULES = (
    "Answer only from the SWARM MEMORY passages above.\n"
    "Cite the passages you use as [n].\n"
    "If the passages do not contain the answer, say so plainly and do not guess."
)


def augment_prompt(question: str, context: str, rules: str = GROUNDING_RULES) -> str:
    """Assemble the grounded prompt. No context ⇒ say so, do not fake citations."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    if not isinstance(context, str):
        raise TypeError("context must be a string")
    if not context.strip():
        return (
            "SWARM MEMORY: (empty — nothing retrieved)\n\n"
            "Answer from your own knowledge and state that no swarm memory was available. "
            "Do not invent citations.\n\n"
            f"QUESTION: {question.strip()}"
        )
    return (
        f"SWARM MEMORY:\n{context}\n\n"
        f"{rules}\n\n"
        f"QUESTION: {question.strip()}"
    )


def assemble(
    question: str,
    hits: Iterable[dict[str, Any]],
    k: int = 4,
    budget: int = CONTEXT_BUDGET,
    embedder: Embedder | None = None,
) -> dict[str, Any]:
    """Full retrieve-side pipeline over raw vault hits."""
    ranked = mmr_select(dedupe_hits(hits), k=k, embedder=embedder)
    context, citations = build_context(ranked, budget=budget)
    return {
        "context": context,
        "citations": citations,
        "hits": ranked,
        "prompt": augment_prompt(question, context),
        "grounded": bool(citations),
    }
