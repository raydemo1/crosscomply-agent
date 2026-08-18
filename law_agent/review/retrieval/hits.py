"""Helpers for combining adapter retrieval results."""

from __future__ import annotations

from law_agent.review.schemas import RetrievalHit


def merge_hits_by_chunk_id(
    hits_per_query: list[list[RetrievalHit]],
    *,
    top_k: int = 10,
) -> list[RetrievalHit]:
    """Deduplicate hits by chunk ID, retain the best score, and re-rank."""

    best: dict[str, RetrievalHit] = {}
    for hits in hits_per_query:
        for hit in hits:
            existing = best.get(hit.chunk_id)
            if existing is None or hit.score > existing.score:
                best[hit.chunk_id] = hit

    merged = sorted(best.values(), key=lambda hit: (-hit.score, hit.chunk_id))
    return [hit.model_copy(update={"rank": rank}) for rank, hit in enumerate(merged[:top_k])]
