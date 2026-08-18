"""Tests for RRF fusion score-aware ranking (P1 bug fix).

Bug: ``apply_boosts_to_hits()`` multiplies hit scores by boost factors, but
``rrf_fuse()`` derived ranks from the ``rank`` field stored on each hit
rather than from the boosted score ordering. A boosted hit that should rank
higher was therefore ignored by RRF, because RRF only looked at the stale
rank position instead of the boosted score.

Fix: ``rrf_fuse()`` sorts each input list by score descending before
extracting ranks, so the rank used by RRF reflects the boosted ordering.
"""

from __future__ import annotations

from law_agent.review.retrieval.fusion import rrf_fuse, rrf_fuse_many, source_aware_fuse
from law_agent.review.schemas import RetrievalHit


def _make_hit(
    chunk_id: str,
    score: float,
    rank: int,
    retriever: str = "keyword",
) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id,
        doc_id="d",
        source_id="s",
        title="t",
        text="x",
        score=score,
        rank=rank,
        retriever=retriever,
        citation_role="primary_legal_basis",
        can_cite_clause=True,
        source_url="u",
    )


def _rank_of(fused: list[RetrievalHit], chunk_id: str) -> int:
    for hit in fused:
        if hit.chunk_id == chunk_id:
            return hit.rank
    raise AssertionError(f"chunk {chunk_id!r} not found in fused results")


def test_boosted_hit_ranks_higher_after_fusion() -> None:
    """A hit whose score is boosted above a peer should overtake it after RRF.

    Setup:
      keyword (pre-boost): chunk_a(0.9, rank0), chunk_b(0.5, rank1), chunk_c(0.3, rank2)
      vector:              chunk_b(0.8, rank0), chunk_a(0.4, rank1), chunk_d(0.3, rank2)

    Both ``chunk_a`` and ``chunk_b`` appear in both retrievers. Pre-boost they
    tie on RRF (a: kw0+vec1, b: kw1+vec0) and ``chunk_a`` wins the chunk_id
    tiebreak, so ``chunk_a`` ranks above ``chunk_b``.

    Boost: bump ``chunk_b``'s keyword score to 0.95 (above ``chunk_a``'s 0.9)
    while keeping the stale ``rank`` field at 1 -- this mirrors what
    ``apply_boosts_to_hits`` does (it updates ``score`` but not ``rank``).

    After the fix, ``rrf_fuse`` re-sorts by boosted score, so ``chunk_b``
    becomes rank 0 in the keyword list and rank 0 in the vector list, giving
    it the highest fused score. ``chunk_b`` must therefore rank at least as
    high as ``chunk_a``. Without the fix, ``chunk_b`` is stuck at rank 1
    regardless of its score and stays below ``chunk_a``.
    """

    # --- pre-boost baseline: chunk_a ranks above chunk_b (tie -> chunk_id) ---
    baseline_keyword = [
        _make_hit("chunk_a", score=0.9, rank=0),
        _make_hit("chunk_b", score=0.5, rank=1),
        _make_hit("chunk_c", score=0.3, rank=2),
    ]
    vector_hits = [
        _make_hit("chunk_b", score=0.8, rank=0, retriever="pgvector"),
        _make_hit("chunk_a", score=0.4, rank=1, retriever="pgvector"),
        _make_hit("chunk_d", score=0.3, rank=2, retriever="pgvector"),
    ]

    baseline_fused = rrf_fuse(baseline_keyword, vector_hits, top_k=10)
    assert _rank_of(baseline_fused, "chunk_a") < _rank_of(baseline_fused, "chunk_b")

    # --- boost: chunk_b's keyword score jumps above chunk_a's, rank is stale ---
    boosted_keyword = [
        _make_hit("chunk_a", score=0.9, rank=0),
        _make_hit("chunk_b", score=0.95, rank=1),  # boosted above a, rank unchanged
        _make_hit("chunk_c", score=0.3, rank=2),
    ]

    boosted_fused = rrf_fuse(boosted_keyword, vector_hits, top_k=10)

    # With the fix, chunk_b overtakes chunk_a.
    # Without the fix, chunk_b is still below chunk_a (boost ignored).
    assert _rank_of(boosted_fused, "chunk_b") <= _rank_of(boosted_fused, "chunk_a")


def test_source_aware_fuse_returns_source_diverse_representatives() -> None:
    hits = [
        _make_hit("a1", score=0.030, rank=0),
        _make_hit("a2", score=0.029, rank=1),
        _make_hit("b1", score=0.020, rank=2),
    ]
    hits[0] = hits[0].model_copy(
        update={"source_id": "source_a", "text": "个人信息出境标准合同办法"}
    )
    hits[1] = hits[1].model_copy(
        update={"source_id": "source_a", "text": "备案材料包括标准合同和个人信息保护影响评估报告"}
    )
    hits[2] = hits[2].model_copy(update={"source_id": "source_b", "text": "其他法规"})

    fused = source_aware_fuse(hits, top_k=2)

    assert [hit.source_id for hit in fused] == ["source_a", "source_b"]
    assert fused[0].chunk_id == "a1"
    assert fused[0].score > hits[0].score


def test_source_aware_fuse_does_not_overboost_near_duplicate_chunks() -> None:
    duplicate_hits = [
        _make_hit("a1", score=0.030, rank=0),
        _make_hit("a2", score=0.029, rank=1),
    ]
    duplicate_hits[0] = duplicate_hits[0].model_copy(
        update={"source_id": "source_a", "text": "个人信息出境标准合同备案材料"}
    )
    duplicate_hits[1] = duplicate_hits[1].model_copy(
        update={"source_id": "source_a", "text": "个人信息出境标准合同备案材料"}
    )

    fused = source_aware_fuse(duplicate_hits, top_k=1)

    assert fused[0].score == duplicate_hits[0].score


def test_source_aware_fuse_prefers_specific_query_types_over_missing_information() -> None:
    missing_hit = _make_hit("missing", score=0.030, rank=0).model_copy(
        update={
            "source_id": "generic_source",
            "matched_query_type": "missing_information",
        }
    )
    industry_hit = _make_hit("industry", score=0.020, rank=1).model_copy(
        update={
            "source_id": "industry_source",
            "matched_query_type": "industry_condition",
        }
    )

    fused = source_aware_fuse([missing_hit, industry_hit], top_k=2)

    assert fused[0].source_id == "industry_source"


def test_rrf_fuse_many_includes_issue_specific_candidates() -> None:
    global_candidates = [
        _make_hit(f"global_{index}", score=100 - index, rank=index) for index in range(50)
    ]
    issue_candidate = _make_hit("issue_specific", score=1.0, rank=0)

    fused = rrf_fuse_many(
        [global_candidates, [issue_candidate]],
        top_k=50,
    )

    assert len({hit.chunk_id for hit in fused}) == 50
    assert "issue_specific" in {hit.chunk_id for hit in fused}
