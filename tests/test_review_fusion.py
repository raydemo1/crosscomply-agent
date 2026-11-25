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

from law_agent.config import RerankConfig
from law_agent.review.retrieval.fusion import rrf_fuse, rrf_fuse_many, source_aware_fuse
from law_agent.review.retrieval.rerank import Reranker, RerankScore, rerank_hits
from law_agent.review.schemas import RetrievalHit, RetrievalQuery, ReviewFacts


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


def test_query_source_budget_deduplicates_and_fills_from_uneven_lists() -> None:
    from law_agent.review.retrieval.fusion import interleave_sources

    hits = [_make_hit(name, score=1.0, rank=i) for i, name in enumerate(["a", "b", "c", "d"])]
    hits = [hit.model_copy(update={"source_id": hit.chunk_id}) for hit in hits]
    selected = interleave_sources([[hits[0], hits[1], hits[3]], [hits[0], hits[2]]], top_k=4)
    assert [hit.source_id for hit in selected] == ["a", "b", "c", "d"]
    assert [hit.rank for hit in selected] == [0, 1, 2, 3]
    assert interleave_sources([hits, []], top_k=2) == selected[:2]
    assert interleave_sources([hits], top_k=0) == []
    assert interleave_sources([], top_k=10) == []


class _FakeReranker(Reranker):
    def __init__(self, scores: list[RerankScore]) -> None:
        self.scores = scores
        self.seen_query = ""
        self.seen_documents: list[str] = []

    def rerank(self, *, query: str, documents, top_n: int) -> list[RerankScore]:
        self.seen_query = query
        self.seen_documents = list(documents)
        return self.scores


def _hit(chunk_id: str, rank: int, score: float = 0.1) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id,
        doc_id=f"doc_{chunk_id}",
        source_id=f"src_{chunk_id}",
        title=f"title {chunk_id}",
        text=f"第{rank}条 数据出境安全评估 申报条件。",
        score=score,
        rank=rank,
        retriever="hybrid",
        citation_role="primary_legal_basis",
        can_cite_clause=True,
        source_url="u",
        matched_query_type="legal_issue",
    )


def _config(window: int = 3) -> RerankConfig:
    return RerankConfig(
        mode="embedding",
        base_url="https://example.test/v1",
        api_key="sk-test",
        model="BAAI/bge-reranker-v2-m3",
        timeout_seconds=30,
        window=window,
        blend_weight=0.4,
    )


def test_rerank_hits_reorders_window_and_keeps_missing_candidates() -> None:
    hits = [_hit("a", 0), _hit("b", 1), _hit("c", 2)]
    fake = _FakeReranker([RerankScore(index=2, score=0.9), RerankScore(index=0, score=0.2)])

    outcome = rerank_hits(
        hits,
        question="是否需要数据出境安全评估？",
        material_text="手机号同步给境外服务商。",
        facts=ReviewFacts(cross_border_transfer=True),
        queries=[
            RetrievalQuery(
                query_id="q1",
                query_type="legal_issue",
                text="数据出境安全评估 申报条件",
            )
        ],
        top_k=3,
        mode="embedding",
        config=_config(),
        reranker=fake,
    )

    assert [hit.chunk_id for hit in outcome.hits] == ["c", "a", "b"]
    assert [hit.rank for hit in outcome.hits] == [0, 1, 2]
    assert outcome.hits[0].score == 0.96
    assert outcome.info["mode"] == "embedding"
    assert outcome.info["model"] == "BAAI/bge-reranker-v2-m3"
    assert outcome.info["blend_weight"] == 0.4
    assert "业务事实" in fake.seen_query
    assert "证据角色" in fake.seen_documents[0]


def test_rerank_hits_preserves_calibrated_cross_query_confidence_and_handles_logits() -> None:
    # Low-confidence query (rerank=0.05) must NOT inflate to 1.0 like per-query min-max did.
    low_hits = [_hit("low_a", 0, score=1.0 / 60), _hit("low_b", 1, score=1.0 / 61)]
    high_hits = [_hit("high_a", 0, score=1.0 / 60), _hit("high_b", 1, score=1.0 / 61)]
    cfg = RerankConfig(
        mode="cross_encoder", base_url="https://example.test/v1", api_key="sk-test",
        model="BAAI/bge-reranker-v2-m3", timeout_seconds=30, window=5, blend_weight=0.85,
    )
    low_out = rerank_hits(
        low_hits, question="q", material_text="", facts=ReviewFacts(), queries=[], top_k=2,
        mode="cross_encoder", config=cfg,
        reranker=_FakeReranker([RerankScore(index=0, score=0.05), RerankScore(index=1, score=0.01)]),
    )
    high_out = rerank_hits(
        high_hits, question="q", material_text="", facts=ReviewFacts(), queries=[], top_k=2,
        mode="cross_encoder", config=cfg,
        reranker=_FakeReranker([RerankScore(index=0, score=0.92), RerankScore(index=1, score=0.85)]),
    )
    assert low_out.hits[0].score < 0.2
    assert high_out.hits[1].score > low_out.hits[0].score

    # Unbounded logits outside [0, 1] are mapped cleanly via logistic sigmoid.
    logit_out = rerank_hits(
        low_hits, question="q", material_text="", facts=ReviewFacts(), queries=[], top_k=2,
        mode="cross_encoder", config=cfg,
        reranker=_FakeReranker([RerankScore(index=0, score=-2.0), RerankScore(index=1, score=3.0)]),
    )
    assert [h.chunk_id for h in logit_out.hits] == ["low_b", "low_a"]
    assert 0.8 < logit_out.hits[0].score < 1.0 and logit_out.hits[1].score < 0.3


def test_rerank_hits_off_preserves_original_order() -> None:
    hits = [_hit("a", 0), _hit("b", 1)]

    outcome = rerank_hits(
        hits,
        question="问题",
        material_text="材料",
        facts=ReviewFacts(),
        queries=[],
        top_k=2,
        mode="off",
    )

    assert outcome.hits == hits
    assert outcome.info["mode"] == "off"
