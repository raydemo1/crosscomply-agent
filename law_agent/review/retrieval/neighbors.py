"""Neighbor chunk expansion for hybrid retrieval.

Issue 6: For top-ranked hybrid hits, pull ``prev_chunk_id`` and
``next_chunk_id`` as supplemental evidence. Neighbors are not primary
ranked hits — they provide surrounding context (e.g., the rest of a
multi-paragraph article, related definitions, or a table's header).

Neighbors are added to ``RetrievalTrace.neighbor_chunks`` with
``retriever="hybrid"`` and a special rank convention: ``-1`` for prev,
``-2`` for next, so they can be distinguished from primary results.
"""

from __future__ import annotations

from law_agent.data.schemas import Chunk
from law_agent.review.schemas import RetrievalHit

# Rank convention for neighbor chunks (negative to distinguish from primary)
PREV_RANK = -1
NEXT_RANK = -2


def expand_neighbors(
    top_hits: list[RetrievalHit],
    chunks_by_id: dict[str, Chunk],
    *,
    max_neighbors: int = 10,
) -> list[RetrievalHit]:
    """Pull prev/next chunks for the top hybrid hits.

    Returns a deduplicated list of neighbor hits. If a neighbor chunk
    is already in the top hits, it is skipped (no need to duplicate).
    If a neighbor was already added from another hit's expansion, it
    is not duplicated.
    """

    top_chunk_ids = {hit.chunk_id for hit in top_hits}
    seen: set[str] = set()
    neighbors: list[RetrievalHit] = []

    for hit in top_hits:
        if len(neighbors) >= max_neighbors:
            break

        chunk = chunks_by_id.get(hit.chunk_id)
        if chunk is None:
            continue

        # Expand prev
        if chunk.prev_chunk_id and chunk.prev_chunk_id in chunks_by_id:
            if chunk.prev_chunk_id not in top_chunk_ids and chunk.prev_chunk_id not in seen:
                seen.add(chunk.prev_chunk_id)
                prev_chunk = chunks_by_id[chunk.prev_chunk_id]
                neighbors.append(_make_neighbor_hit(prev_chunk, PREV_RANK, hit.matched_query_type))
                if len(neighbors) >= max_neighbors:
                    break

        # Expand next
        if chunk.next_chunk_id and chunk.next_chunk_id in chunks_by_id:
            if chunk.next_chunk_id not in top_chunk_ids and chunk.next_chunk_id not in seen:
                seen.add(chunk.next_chunk_id)
                next_chunk = chunks_by_id[chunk.next_chunk_id]
                neighbors.append(_make_neighbor_hit(next_chunk, NEXT_RANK, hit.matched_query_type))
                if len(neighbors) >= max_neighbors:
                    break

    return neighbors


def _make_neighbor_hit(
    chunk: Chunk,
    rank: int,
    matched_query_type: str | None = None,
) -> RetrievalHit:
    """Create a RetrievalHit for a neighbor chunk."""

    return RetrievalHit(
        chunk_id=chunk.chunk_id,
        doc_id=chunk.doc_id,
        source_id=chunk.source_id,
        title=chunk.title,
        text=chunk.text,
        score=0.0,  # Neighbors don't have a retrieval score
        rank=rank,
        retriever="hybrid",
        citation_role=chunk.citation_role,
        can_cite_clause=chunk.can_cite_clause,
        source_url=chunk.source_url,
        matched_query_type=matched_query_type,
        article_no=chunk.article_no,
        citation_label=chunk.citation_label,
        heading_path=chunk.heading_path,
        doc_type=chunk.doc_type,
        authority=chunk.authority,
        law_status=chunk.law_status,
        publish_date=chunk.publish_date,
        effective_date=chunk.effective_date,
        issuing_body=chunk.issuing_body,
    )
