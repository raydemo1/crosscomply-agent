"""Reciprocal Rank Fusion (RRF) for hybrid retrieval.

Fuse keyword and vector results using deterministic RRF scoring.
RRF formula: ``rrf_score = sum(1 / (k + rank))`` over all retrievers where
the chunk appears. Metadata boosts are applied as a post-RRF multiplier.

The fused results are stored in ``RetrievalTrace.hybrid_results`` with
component scores preserved for traceability.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping

from law_agent.data.schemas import Chunk
from law_agent.review.retrieval.text import tokenize
from law_agent.review.schemas import RetrievalHit

DEFAULT_RRF_K = 60
DEFAULT_SOURCE_SUPPORT_DECAY = 0.35
DEFAULT_CONTEXT_SUPPORT_DECAY = 0.45
DEFAULT_NEAR_DUPLICATE_SIMILARITY = 0.82
SOURCE_QUERY_TYPE_WEIGHTS = {
    "industry_condition": 1.05,
    "region_condition": 1.0,
    "material_fact": 1.05,
    "missing_information": 0.7,
}


def rrf_fuse(
    keyword_hits: list[RetrievalHit],
    vector_hits: list[RetrievalHit],
    *,
    top_k: int = 10,
    k: int = DEFAULT_RRF_K,
) -> list[RetrievalHit]:
    """Fuse keyword and vector results using RRF.

    Each retriever contributes ``1 / (k + rank)`` per hit. A chunk
    appearing in both retrievers gets the sum. Results are sorted by
    fused score and re-ranked. The ``retriever`` field is set to
    ``"hybrid"`` and the ``score`` field holds the RRF score.

    When the same chunk appears in both lists, the hit with more
    metadata (first encountered) is used as the base, and the score
    reflects both contributions.

    Ranks are derived from each input list's score ordering: each list
    is sorted by ``score`` descending (``chunk_id`` as a deterministic
    tiebreaker) before ranks are extracted, so rank 0 is the
    highest-scored hit. This ensures metadata boosts applied upstream
    by ``apply_boosts_to_hits`` (which update ``score`` but not the
    stale ``rank`` field) are reflected in the fused ordering.
    """

    # Sort each component list by boosted score descending so the rank
    # used by RRF reflects the boosted ordering. chunk_id breaks ties
    # deterministically. Without this, a boosted hit keeps its stale
    # ``rank`` field and the boost is ignored by RRF.
    keyword_hits = sorted(keyword_hits, key=lambda h: (-h.score, h.chunk_id))
    vector_hits = sorted(vector_hits, key=lambda h: (-h.score, h.chunk_id))

    # Build rank maps per retriever. Rank is the position in the
    # score-sorted list (not the hit's stored ``rank`` field), so that
    # boosts applied to ``score`` actually move hits up or down.
    keyword_ranks: dict[str, int] = {}
    for rank, hit in enumerate(keyword_hits):
        keyword_ranks[hit.chunk_id] = rank

    vector_ranks: dict[str, int] = {}
    for rank, hit in enumerate(vector_hits):
        vector_ranks[hit.chunk_id] = rank

    # All chunk IDs that appear in either list
    all_chunk_ids = set(keyword_ranks) | set(vector_ranks)

    # Build a lookup for the "best" hit per chunk_id (prefer keyword as base
    # since it has more field-level detail)
    hit_by_chunk: dict[str, RetrievalHit] = {}
    for hit in keyword_hits:
        hit_by_chunk[hit.chunk_id] = hit
    for hit in vector_hits:
        if hit.chunk_id not in hit_by_chunk:
            hit_by_chunk[hit.chunk_id] = hit

    # Compute RRF scores
    scored: list[tuple[float, str]] = []
    for chunk_id in all_chunk_ids:
        rrf_score = 0.0
        if chunk_id in keyword_ranks:
            rrf_score += 1.0 / (k + keyword_ranks[chunk_id])
        if chunk_id in vector_ranks:
            rrf_score += 1.0 / (k + vector_ranks[chunk_id])
        scored.append((rrf_score, chunk_id))

    scored.sort(key=lambda pair: (-pair[0], pair[1]))

    fused: list[RetrievalHit] = []
    for rank, (rrf_score, chunk_id) in enumerate(scored[:top_k]):
        base = hit_by_chunk[chunk_id]
        fused.append(
            base.model_copy(
                update={
                    "score": round(rrf_score, 6),
                    "rank": rank,
                    "retriever": "hybrid",
                }
            )
        )
    return fused


def rrf_fuse_many(
    ranked_lists: list[list[RetrievalHit]],
    *,
    top_k: int = 50,
    k: int = DEFAULT_RRF_K,
) -> list[RetrievalHit]:
    """Fuse any number of ranked candidate lists into unique chunk candidates."""

    scores: dict[str, float] = defaultdict(float)
    hit_by_chunk: dict[str, RetrievalHit] = {}
    for hits in ranked_lists:
        ordered = sorted(hits, key=lambda hit: (-hit.score, hit.chunk_id))
        seen: set[str] = set()
        for rank, hit in enumerate(ordered):
            if hit.chunk_id in seen:
                continue
            seen.add(hit.chunk_id)
            scores[hit.chunk_id] += 1.0 / (k + rank)
            hit_by_chunk.setdefault(hit.chunk_id, hit)

    ordered_scores = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [
        hit_by_chunk[chunk_id].model_copy(
            update={
                "score": round(score, 6),
                "rank": rank,
                "retriever": "hybrid",
            }
        )
        for rank, (chunk_id, score) in enumerate(ordered_scores[:top_k])
    ]


def source_aware_fuse(
    hits: list[RetrievalHit],
    *,
    top_k: int = 10,
    chunks_by_id: Mapping[str, Chunk] | None = None,
    support_decay: float = DEFAULT_SOURCE_SUPPORT_DECAY,
    context_support_decay: float = DEFAULT_CONTEXT_SUPPORT_DECAY,
    near_duplicate_similarity: float = DEFAULT_NEAR_DUPLICATE_SIMILARITY,
) -> list[RetrievalHit]:
    """Collapse chunk-ranked candidates into source-diverse evidence.

    RRF is intentionally chunk-level: it rewards chunks that appear in both
    retrieval routes. Legal evidence, however, is often judged at source level
    in evaluation and in the user's mental model. This pass ranks sources by
    their strongest chunk plus a small amount of supporting evidence from
    nearby or semantically distinct chunks in the same source, then returns one
    representative chunk per source.

    The support rules are deliberately conservative:
    - adjacent/same-article chunks provide contextual support;
    - near-duplicate chunks above the similarity threshold do not over-boost;
    - unrelated chunks can still add a small signal that the whole source is
      broadly relevant.
    """

    if top_k <= 0 or not hits:
        return []

    groups: dict[str, list[RetrievalHit]] = defaultdict(list)
    for hit in hits:
        groups[hit.source_id].append(hit)

    scored_sources: list[tuple[float, str, RetrievalHit]] = []
    for source_id, source_hits in groups.items():
        ordered = sorted(source_hits, key=lambda h: (-h.score, h.rank, h.chunk_id))
        representative = ordered[0]
        source_score = representative.score
        support_count = 0

        for support_hit in ordered[1:]:
            similarity = _text_similarity(representative.text, support_hit.text)
            if similarity >= near_duplicate_similarity:
                continue

            decay = (
                context_support_decay
                if _is_context_related(representative, support_hit, chunks_by_id)
                else support_decay
            )
            source_score += support_hit.score * decay
            support_count += 1
            if support_count >= 2:
                break

        source_score *= SOURCE_QUERY_TYPE_WEIGHTS.get(
            representative.matched_query_type or "", 1.0
        )
        scored_sources.append((source_score, source_id, representative))

    scored_sources.sort(key=lambda item: (-item[0], item[1]))

    fused: list[RetrievalHit] = []
    for rank, (score, _source_id, hit) in enumerate(scored_sources[:top_k]):
        fused.append(
            hit.model_copy(
                update={
                    "score": round(score, 6),
                    "rank": rank,
                    "retriever": "hybrid",
                }
            )
        )
    return fused


def _is_context_related(
    first: RetrievalHit,
    second: RetrievalHit,
    chunks_by_id: Mapping[str, Chunk] | None,
) -> bool:
    if first.source_id != second.source_id:
        return False
    if chunks_by_id is None:
        return False

    first_chunk = chunks_by_id.get(first.chunk_id)
    second_chunk = chunks_by_id.get(second.chunk_id)
    if first_chunk is None or second_chunk is None:
        return False

    if first_chunk.article_no and first_chunk.article_no == second_chunk.article_no:
        return True
    if first_chunk.next_chunk_id == second.chunk_id:
        return True
    if first_chunk.prev_chunk_id == second.chunk_id:
        return True
    if second_chunk.next_chunk_id == first.chunk_id:
        return True
    if second_chunk.prev_chunk_id == first.chunk_id:
        return True

    return abs(first_chunk.chunk_index - second_chunk.chunk_index) <= 1


def _text_similarity(first: str, second: str) -> float:
    first_tokens = set(tokenize(first))
    second_tokens = set(tokenize(second))
    if not first_tokens or not second_tokens:
        return 0.0
    intersection = len(first_tokens & second_tokens)
    union = len(first_tokens | second_tokens)
    if union == 0:
        return 0.0
    return intersection / union
