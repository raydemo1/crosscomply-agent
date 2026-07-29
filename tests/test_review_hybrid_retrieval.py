"""Tests for hybrid retrieval orchestration, boosts, RRF, and neighbors."""

from pathlib import Path

import pytest

from law_agent.data.io import write_jsonl
from law_agent.data.schemas import Chunk
from law_agent.review.retrieval.boosts import (
    CONDITIONAL_INDUSTRY_MISMATCH_WEIGHT,
    CONDITIONAL_LOCAL_MISMATCH_WEIGHT,
    CROSS_BORDER_PRIMARY_LEGAL_BASIS_BOOST,
    INTERPRETATION_AUXILIARY_BOOST,
    MISSING_INFORMATION_QUERY_WEIGHT,
    PRIMARY_LEGAL_BASIS_BOOST,
    apply_boosts_to_hits,
    compute_boost_for_hit,
    compute_boosts_summary,
)
from law_agent.review.retrieval.fusion import rrf_fuse
from law_agent.review.retrieval.neighbors import expand_neighbors
from law_agent.review.schemas import ReviewFacts, ReviewResult, RetrievalHit, RetrievalQuery

from tests.test_review_retrieval_keyword import FIXTURE_CHUNKS, _make_chunk


@pytest.fixture(autouse=True)
def stub_llm_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "law_agent.review.service.extract_facts_with_deepseek",
        lambda material, question=None: ReviewFacts(
            cross_border_transfer=True if "出境" in material or "新加坡" in material else None,
            region="上海" if "上海" in material else None,
            data_types=["手机号"] if "手机号" in material else [],
            missing_information=[],
        ),
    )
    monkeypatch.setattr(
        "law_agent.review.service.plan_queries_with_deepseek",
        lambda question, facts, material=None: [
            RetrievalQuery(query_id="q_test", query_type="legal_issue", text=question)
        ],
    )
    monkeypatch.setattr("law_agent.review.service.needs_llm_self_check", lambda **kwargs: False)
    monkeypatch.setattr(
        "law_agent.review.service.build_review_result_with_deepseek",
        lambda **kwargs: ReviewResult(
            review_result_id=kwargs["review_result_id"],
            review_case_id=kwargs["review_case_id"],
            trace_id=kwargs["trace_id"],
            risk_level="medium",
            conclusion="测试审查结论。",
            review_facts=kwargs["facts"],
        ),
    )


# ---------------------------------------------------------------------------
# Metadata boost tests
# ---------------------------------------------------------------------------

def test_primary_legal_basis_gets_boost() -> None:
    hit = RetrievalHit(
        chunk_id="c1", doc_id="d1", source_id="s1", title="t", text="x",
        score=1.0, rank=0, retriever="keyword",
        citation_role="primary_legal_basis", can_cite_clause=True, source_url="u",
    )
    chunk = _make_chunk(chunk_id="c1")
    facts = ReviewFacts()

    boost = compute_boost_for_hit(hit, chunk, facts)
    assert boost == pytest.approx(PRIMARY_LEGAL_BASIS_BOOST)


def test_cross_border_primary_legal_basis_gets_extra_soft_boost() -> None:
    hit = RetrievalHit(
        chunk_id="c1", doc_id="d1", source_id="s1", title="t", text="x",
        score=1.0, rank=0, retriever="keyword",
        citation_role="primary_legal_basis", can_cite_clause=True, source_url="u",
    )
    chunk = _make_chunk(chunk_id="c1").model_copy(
        update={"topic_tags": ["数据出境", "数据合规"]}
    )
    facts = ReviewFacts(cross_border_transfer=True)

    boost = compute_boost_for_hit(hit, chunk, facts)
    assert boost == pytest.approx(
        PRIMARY_LEGAL_BASIS_BOOST * CROSS_BORDER_PRIMARY_LEGAL_BASIS_BOOST
    )


def test_interpretation_auxiliary_gets_demoted() -> None:
    hit = RetrievalHit(
        chunk_id="c1", doc_id="d1", source_id="s1", title="t", text="x",
        score=1.0, rank=0, retriever="keyword",
        citation_role="interpretation_auxiliary", can_cite_clause=False, source_url="u",
    )
    chunk = _make_chunk(chunk_id="c1", citation_role="interpretation_auxiliary")
    facts = ReviewFacts()

    boost = compute_boost_for_hit(hit, chunk, facts)
    assert boost == pytest.approx(INTERPRETATION_AUXILIARY_BOOST)


def test_conditional_local_basis_boosted_when_region_matches() -> None:
    hit = RetrievalHit(
        chunk_id="c1", doc_id="d1", source_id="s1", title="t", text="x",
        score=1.0, rank=0, retriever="keyword",
        citation_role="conditional_local_basis", can_cite_clause=False, source_url="u",
    )
    chunk = _make_chunk(
        chunk_id="c1",
        citation_role="conditional_local_basis",
        applicable_region="CN-SH",
    )
    facts = ReviewFacts(region="上海")

    boost = compute_boost_for_hit(hit, chunk, facts)
    assert boost > 1.0


def test_conditional_local_basis_boosted_when_ftz_region_matches_parent() -> None:
    hit = RetrievalHit(
        chunk_id="c1", doc_id="d1", source_id="s1", title="t", text="x",
        score=1.0, rank=0, retriever="keyword",
        citation_role="conditional_local_basis", can_cite_clause=False, source_url="u",
    )
    chunk = _make_chunk(
        chunk_id="c1",
        citation_role="conditional_local_basis",
        applicable_region="CN-CQ",
    )
    facts = ReviewFacts(region="CN-CQ-FTZ")

    boost = compute_boost_for_hit(hit, chunk, facts)
    assert boost > 1.0


def test_conditional_local_basis_downweighted_when_region_mismatch() -> None:
    hit = RetrievalHit(
        chunk_id="c1", doc_id="d1", source_id="s1", title="t", text="x",
        score=1.0, rank=0, retriever="keyword",
        citation_role="conditional_local_basis", can_cite_clause=False, source_url="u",
    )
    chunk = _make_chunk(
        chunk_id="c1",
        citation_role="conditional_local_basis",
        applicable_region="CN-BJ",
    )
    facts = ReviewFacts(region="上海")

    boost = compute_boost_for_hit(hit, chunk, facts)
    assert boost == pytest.approx(CONDITIONAL_LOCAL_MISMATCH_WEIGHT)


def test_conditional_industry_basis_boosted_when_industry_matches() -> None:
    hit = RetrievalHit(
        chunk_id="c1", doc_id="d1", source_id="s1", title="t", text="x",
        score=1.0, rank=0, retriever="keyword",
        citation_role="conditional_industry_basis", can_cite_clause=True, source_url="u",
    )
    chunk = _make_chunk(
        chunk_id="c1",
        citation_role="conditional_industry_basis",
    ).model_copy(update={"applicable_subjects": ["汽车数据处理者"], "topic_tags": ["汽车行业"]})
    facts = ReviewFacts(industry="汽车")

    boost = compute_boost_for_hit(hit, chunk, facts)
    assert boost > 1.0


def test_conditional_industry_basis_uses_industry_aliases() -> None:
    hit = RetrievalHit(
        chunk_id="c1", doc_id="d1", source_id="s1", title="t", text="x",
        score=1.0, rank=0, retriever="keyword",
        citation_role="conditional_industry_basis", can_cite_clause=True, source_url="u",
    )
    chunk = _make_chunk(
        chunk_id="c1",
        citation_role="conditional_industry_basis",
    ).model_copy(update={"applicable_subjects": ["智能网联汽车企业"], "topic_tags": []})
    facts = ReviewFacts(industry="车联网")

    boost = compute_boost_for_hit(hit, chunk, facts)
    assert boost > 1.0


def test_conditional_industry_basis_downweighted_when_industry_mismatch() -> None:
    hit = RetrievalHit(
        chunk_id="c1", doc_id="d1", source_id="s1", title="t", text="x",
        score=1.0, rank=0, retriever="keyword",
        citation_role="conditional_industry_basis", can_cite_clause=True, source_url="u",
    )
    chunk = _make_chunk(
        chunk_id="c1",
        citation_role="conditional_industry_basis",
    ).model_copy(update={"applicable_subjects": ["金融机构"], "topic_tags": ["金融"]})
    facts = ReviewFacts(industry="车联网")

    boost = compute_boost_for_hit(hit, chunk, facts)
    assert boost == pytest.approx(CONDITIONAL_INDUSTRY_MISMATCH_WEIGHT)


def test_boosts_summary_records_active_rules() -> None:
    facts = ReviewFacts(region="上海", industry="汽车")
    summary = compute_boosts_summary(facts, ["legal_issue", "missing_information"])

    assert "primary_legal_basis" in summary
    assert "interpretation_auxiliary" in summary
    assert "conditional_local_basis:CN-SH" in summary
    assert "conditional_industry_basis:汽车" in summary
    assert "implementation_reference:missing_information" in summary


def test_apply_boosts_to_hits_multiplies_scores() -> None:
    hit = RetrievalHit(
        chunk_id="c1", doc_id="d1", source_id="s1", title="t", text="x",
        score=2.0, rank=0, retriever="keyword",
        citation_role="primary_legal_basis", can_cite_clause=True, source_url="u",
    )
    chunk = _make_chunk(chunk_id="c1")
    facts = ReviewFacts()

    boosted = apply_boosts_to_hits([hit], {"c1": chunk}, facts)
    assert boosted[0].score == pytest.approx(2.0 * PRIMARY_LEGAL_BASIS_BOOST, rel=1e-4)


def test_missing_information_query_hits_are_downweighted() -> None:
    hit = RetrievalHit(
        chunk_id="c1", doc_id="d1", source_id="s1", title="t", text="x",
        score=2.0, rank=0, retriever="keyword",
        citation_role="primary_legal_basis", can_cite_clause=True, source_url="u",
        matched_query_type="missing_information",
    )
    chunk = _make_chunk(chunk_id="c1")
    facts = ReviewFacts()

    boosted = apply_boosts_to_hits([hit], {"c1": chunk}, facts)

    assert boosted[0].score == pytest.approx(
        2.0 * PRIMARY_LEGAL_BASIS_BOOST * MISSING_INFORMATION_QUERY_WEIGHT,
        rel=1e-4,
    )


# ---------------------------------------------------------------------------
# RRF fusion tests
# ---------------------------------------------------------------------------

def _make_hit(
    chunk_id: str, rank: int, score: float = 1.0, retriever: str = "keyword"
) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id, doc_id="d", source_id="s", title="t", text="x",
        score=score, rank=rank, retriever=retriever,
        citation_role="primary_legal_basis", can_cite_clause=True, source_url="u",
    )


def test_rrf_fuse_combines_keyword_and_vector_ranks() -> None:
    keyword_hits = [_make_hit("a", rank=0), _make_hit("b", rank=1)]
    vector_hits = [_make_hit("a", rank=0), _make_hit("c", rank=1)]

    fused = rrf_fuse(keyword_hits, vector_hits, top_k=10)

    assert len(fused) == 3
    # Chunk "a" appears in both, should rank first
    assert fused[0].chunk_id == "a"
    assert fused[0].retriever == "hybrid"
    assert fused[0].rank == 0


def test_rrf_fuse_score_formula() -> None:
    """Verify RRF score = sum(1 / (k + rank)) for a chunk in both lists.

    Ranks are derived from each list's score ordering (see the fusion fix),
    so to place ``a`` at vector rank 1 we put a higher-scored hit ``b``
    ahead of it in the vector list. ``a`` then contributes keyword rank 0
    plus vector rank 1.
    """

    keyword_hits = [_make_hit("a", rank=0, score=1.0)]  # a at keyword rank 0
    vector_hits = [
        _make_hit("b", rank=0, score=1.0),  # higher score -> vector rank 0
        _make_hit("a", rank=1, score=0.5),  # lower score  -> vector rank 1
    ]

    fused = rrf_fuse(keyword_hits, vector_hits, top_k=2, k=60)

    a_hit = next(h for h in fused if h.chunk_id == "a")
    expected = 1.0 / (60 + 0) + 1.0 / (60 + 1)
    assert a_hit.score == pytest.approx(expected, rel=1e-5)


def test_rrf_fuse_respects_top_k() -> None:
    keyword_hits = [_make_hit(f"k{i}", rank=i) for i in range(5)]
    vector_hits = [_make_hit(f"v{i}", rank=i) for i in range(5)]

    fused = rrf_fuse(keyword_hits, vector_hits, top_k=3)

    assert len(fused) == 3


def test_rrf_fuse_preserves_metadata_from_base_hit() -> None:
    keyword_hits = [
        RetrievalHit(
            chunk_id="a", doc_id="d1", source_id="s1", title="法规标题",
            text="法规正文", score=1.0, rank=0, retriever="keyword",
            citation_role="primary_legal_basis", can_cite_clause=True,
            source_url="http://example.com", matched_query_type="legal_issue",
        )
    ]
    vector_hits: list[RetrievalHit] = []

    fused = rrf_fuse(keyword_hits, vector_hits, top_k=1)

    assert fused[0].title == "法规标题"
    assert fused[0].citation_role == "primary_legal_basis"
    assert fused[0].can_cite_clause is True
    assert fused[0].source_url == "http://example.com"
    assert fused[0].matched_query_type == "legal_issue"


def test_source_evidence_packets_keep_same_source_supporting_chunks() -> None:
    from law_agent.review.service import (
        build_source_evidence_packets,
        flatten_source_evidence_packets,
    )

    representative = _make_hit("a1", rank=0, score=1.0).model_copy(
        update={"source_id": "source_a", "title": "来源 A"}
    )
    supporting = [
        _make_hit("a2", rank=1, score=0.9).model_copy(update={"source_id": "source_a"}),
        _make_hit("a3", rank=2, score=0.8).model_copy(update={"source_id": "source_a"}),
        _make_hit("b1", rank=3, score=0.7).model_copy(update={"source_id": "source_b"}),
    ]
    neighbor = _make_hit("a0", rank=-1, score=0.0).model_copy(update={"source_id": "source_a"})

    packets = build_source_evidence_packets(
        representative_hits=[representative],
        candidate_hits=[representative, *supporting],
        neighbor_hits=[neighbor],
    )

    assert len(packets) == 1
    assert packets[0].representative_chunk.chunk_id == "a1"
    assert [hit.chunk_id for hit in packets[0].supporting_chunks] == ["a2", "a3"]
    assert [hit.chunk_id for hit in packets[0].neighbor_chunks] == ["a0"]
    assert [hit.chunk_id for hit in flatten_source_evidence_packets(packets)] == [
        "a1",
        "a2",
        "a3",
        "a0",
    ]


# ---------------------------------------------------------------------------
# Neighbor expansion tests
# ---------------------------------------------------------------------------

def test_expand_neighbors_pulls_prev_and_next() -> None:
    chunk_a = _make_chunk(chunk_id="a", chunk_index=0, next_chunk_id="b")
    chunk_b = _make_chunk(chunk_id="b", chunk_index=1, prev_chunk_id="a", next_chunk_id="c")
    chunk_c = _make_chunk(chunk_id="c", chunk_index=2, prev_chunk_id="b")

    chunks_by_id = {"a": chunk_a, "b": chunk_b, "c": chunk_c}

    top_hits = [
        RetrievalHit(
            chunk_id="b", doc_id="d", source_id="s", title="t", text="x",
            score=1.0, rank=0, retriever="hybrid",
            citation_role="primary_legal_basis", can_cite_clause=True, source_url="u",
        )
    ]

    neighbors = expand_neighbors(top_hits, chunks_by_id)

    neighbor_ids = [n.chunk_id for n in neighbors]
    assert "a" in neighbor_ids  # prev of b
    assert "c" in neighbor_ids  # next of b


def test_expand_neighbors_skips_chunks_already_in_top_hits() -> None:
    chunk_a = _make_chunk(chunk_id="a", chunk_index=0, next_chunk_id="b")
    chunk_b = _make_chunk(chunk_id="b", chunk_index=1, prev_chunk_id="a")

    chunks_by_id = {"a": chunk_a, "b": chunk_b}

    # Both a and b are in top hits, so no neighbors should be added
    top_hits = [
        RetrievalHit(
            chunk_id="a", doc_id="d", source_id="s", title="t", text="x",
            score=1.0, rank=0, retriever="hybrid",
            citation_role="primary_legal_basis", can_cite_clause=True, source_url="u",
        ),
        RetrievalHit(
            chunk_id="b", doc_id="d", source_id="s", title="t", text="x",
            score=0.8, rank=1, retriever="hybrid",
            citation_role="primary_legal_basis", can_cite_clause=True, source_url="u",
        ),
    ]

    neighbors = expand_neighbors(top_hits, chunks_by_id)
    assert len(neighbors) == 0


def test_expand_neighbors_deduplicates() -> None:
    """If two top hits share the same neighbor, it should only appear once."""

    chunk_a = _make_chunk(chunk_id="a", chunk_index=0, next_chunk_id="b")
    chunk_b = _make_chunk(chunk_id="b", chunk_index=1, prev_chunk_id="a", next_chunk_id="c")
    chunk_c = _make_chunk(chunk_id="c", chunk_index=2, prev_chunk_id="b", next_chunk_id="d")
    chunk_d = _make_chunk(chunk_id="d", chunk_index=3, prev_chunk_id="c")

    chunks_by_id = {"a": chunk_a, "b": chunk_b, "c": chunk_c, "d": chunk_d}

    # b and c are top hits; b's next is c (already in top), c's prev is b (already in top)
    # b's prev is a, c's next is d -> should get a and d
    top_hits = [
        RetrievalHit(
            chunk_id="b", doc_id="d", source_id="s", title="t", text="x",
            score=1.0, rank=0, retriever="hybrid",
            citation_role="primary_legal_basis", can_cite_clause=True, source_url="u",
        ),
        RetrievalHit(
            chunk_id="c", doc_id="d", source_id="s", title="t", text="x",
            score=0.8, rank=1, retriever="hybrid",
            citation_role="primary_legal_basis", can_cite_clause=True, source_url="u",
        ),
    ]

    neighbors = expand_neighbors(top_hits, chunks_by_id)
    neighbor_ids = [n.chunk_id for n in neighbors]
    assert "a" in neighbor_ids
    assert "d" in neighbor_ids
    # No duplicates
    assert len(neighbor_ids) == len(set(neighbor_ids))


def test_neighbor_rank_convention() -> None:
    chunk_a = _make_chunk(chunk_id="a", chunk_index=0, next_chunk_id="b")
    chunk_b = _make_chunk(chunk_id="b", chunk_index=1, prev_chunk_id="a")

    chunks_by_id = {"a": chunk_a, "b": chunk_b}

    top_hits = [
        RetrievalHit(
            chunk_id="b", doc_id="d", source_id="s", title="t", text="x",
            score=1.0, rank=0, retriever="hybrid",
            citation_role="primary_legal_basis", can_cite_clause=True, source_url="u",
        )
    ]

    neighbors = expand_neighbors(top_hits, chunks_by_id)
    # "a" is prev of "b", should have rank -1
    assert neighbors[0].rank == -1


# ---------------------------------------------------------------------------
# Integration: run_hybrid_retrieval service
# ---------------------------------------------------------------------------

def _write_fixture_corpus(tmp_path: Path) -> Path:
    chunks_path = tmp_path / "chunks.jsonl"
    # Add prev/next links to fixture chunks for neighbor testing
    linked_chunks = []
    for i, chunk in enumerate(FIXTURE_CHUNKS):
        linked = chunk.model_copy(
            update={
                "prev_chunk_id": FIXTURE_CHUNKS[i - 1].chunk_id if i > 0 else None,
                "next_chunk_id": FIXTURE_CHUNKS[i + 1].chunk_id if i < len(FIXTURE_CHUNKS) - 1 else None,
            }
        )
        linked_chunks.append(linked)
    write_jsonl(chunks_path, linked_chunks)
    return chunks_path


class _RecordingSearchManyRetriever:
    def __init__(self, chunks: list[Chunk], retriever: str) -> None:
        self.chunks = chunks
        self.retriever = retriever
        self.requested_top_ks: list[int] = []

    def search_many(self, queries, *, top_k: int = 10):
        self.requested_top_ks.append(top_k)
        hits = [
            RetrievalHit(
                chunk_id=chunk.chunk_id,
                doc_id=chunk.doc_id,
                source_id=chunk.source_id,
                title=chunk.title,
                text=chunk.text,
                score=float(len(self.chunks) - index),
                rank=index,
                retriever=self.retriever,
                citation_role=chunk.citation_role,
                can_cite_clause=chunk.can_cite_clause,
                source_url=chunk.source_url,
            )
            for index, chunk in enumerate(self.chunks[:top_k])
        ]
        return [hits for _query in queries]


def test_run_hybrid_retrieval_expands_source_window_when_rerank_is_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from law_agent.config import RerankConfig
    from law_agent.review.retrieval.rerank import RerankOutcome
    from law_agent.review.service import create_review_case, run_hybrid_retrieval

    chunks = [
        _make_chunk(
            chunk_id=f"rerank_{index}",
            doc_id=f"doc_{index}",
            source_id=f"src_{index}",
            text=f"第{index}条 数据出境安全评估 申报条件。",
        )
        for index in range(40)
    ]
    chunks_path = tmp_path / "chunks.jsonl"
    write_jsonl(chunks_path, chunks)

    create_review_case(
        question="这个场景是否需要数据出境安全评估？",
        material_text="我们会将手机号发送给新加坡服务商用于推荐优化。",
        output_dir=tmp_path,
        now=lambda: "2026-07-06T00:00:00+00:00",
        id_factory=lambda prefix: f"{prefix}_test",
    )
    keyword = _RecordingSearchManyRetriever(chunks, "keyword")
    vector = _RecordingSearchManyRetriever(chunks, "pgvector")
    captured = {}

    def fake_load_rerank_config(*, mode):
        return RerankConfig(
            mode=mode,
            base_url="https://example.test/v1",
            api_key="sk-test",
            model="BAAI/bge-reranker-v2-m3",
            timeout_seconds=30,
            window=12,
            blend_weight=0.4,
        )

    def fake_rerank_hits(hits, **kwargs):
        captured["input_count"] = len(hits)
        selected = list(hits[: kwargs["top_k"]])
        return RerankOutcome(
            hits=selected,
            info={
                "mode": kwargs["mode"],
                "window": len(hits),
                "input_count": len(hits),
                "output_count": len(selected),
            },
        )

    monkeypatch.setattr(
        "law_agent.review.service.load_rerank_config",
        fake_load_rerank_config,
    )
    monkeypatch.setattr(
        "law_agent.review.service.rerank_hits",
        fake_rerank_hits,
    )

    trace = run_hybrid_retrieval(
        case_id="review_test",
        chunks_path=chunks_path,
        output_dir=tmp_path,
        top_k=5,
        rerank_mode="embedding",
        keyword_retriever=keyword,
        vector_retriever=vector,
    )

    assert captured["input_count"] == 12
    assert len(trace.hybrid_results) == 5
    assert trace.rerank["initial"]["mode"] == "embedding"


def test_run_hybrid_retrieval_default_rerank_window_covers_candidate_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from law_agent.review.retrieval.rerank import RerankOutcome
    from law_agent.review.service import (
        DEFAULT_CANDIDATE_TOP_K,
        create_review_case,
        run_hybrid_retrieval,
    )

    monkeypatch.delenv("RERANK_WINDOW", raising=False)

    chunks = [
        _make_chunk(
            chunk_id=f"rerank_default_{index}",
            doc_id=f"doc_{index}",
            source_id=f"src_{index}",
            text=f"第{index}条 数据出境安全评估 申报条件。",
        )
        for index in range(DEFAULT_CANDIDATE_TOP_K + 10)
    ]
    chunks_path = tmp_path / "chunks.jsonl"
    write_jsonl(chunks_path, chunks)

    create_review_case(
        question="这个场景是否需要数据出境安全评估？",
        material_text="我们会将手机号发送给新加坡服务商用于推荐优化。",
        output_dir=tmp_path,
        now=lambda: "2026-07-06T00:00:00+00:00",
        id_factory=lambda prefix: f"{prefix}_test",
    )
    keyword = _RecordingSearchManyRetriever(chunks, "keyword")
    vector = _RecordingSearchManyRetriever(chunks, "pgvector")
    captured = {}

    def fake_rerank_hits(hits, **kwargs):
        captured["input_count"] = len(hits)
        selected = list(hits[: kwargs["top_k"]])
        return RerankOutcome(
            hits=selected,
            info={
                "mode": kwargs["mode"],
                "window": len(hits),
                "input_count": len(hits),
                "output_count": len(selected),
            },
        )

    monkeypatch.setattr(
        "law_agent.review.service.rerank_hits",
        fake_rerank_hits,
    )

    run_hybrid_retrieval(
        case_id="review_test",
        chunks_path=chunks_path,
        output_dir=tmp_path,
        top_k=5,
        rerank_mode="embedding",
        keyword_retriever=keyword,
        vector_retriever=vector,
    )

    assert keyword.requested_top_ks == [DEFAULT_CANDIDATE_TOP_K]
    assert vector.requested_top_ks == [DEFAULT_CANDIDATE_TOP_K]
    assert captured["input_count"] == DEFAULT_CANDIDATE_TOP_K


def test_run_hybrid_retrieval_returns_all_components(tmp_path: Path) -> None:
    from law_agent.review.service import create_review_case, run_hybrid_retrieval

    chunks_path = _write_fixture_corpus(tmp_path)

    create_review_case(
        question="这个场景是否需要数据出境安全评估？",
        material_text="我们会将手机号和定位信息发送给新加坡服务商用于推荐优化。",
        output_dir=tmp_path,
        now=lambda: "2026-07-06T00:00:00+00:00",
        id_factory=lambda prefix: f"{prefix}_test",
    )

    trace = run_hybrid_retrieval(
        case_id="review_test",
        chunks_path=chunks_path,
        output_dir=tmp_path,
        top_k=5,
        keyword_retriever=_RecordingSearchManyRetriever(FIXTURE_CHUNKS, "elasticsearch"),
        vector_retriever=_RecordingSearchManyRetriever(FIXTURE_CHUNKS, "pgvector"),
    )

    assert len(trace.keyword_results) > 0
    assert len(trace.vector_results) > 0
    assert len(trace.hybrid_results) > 0
    assert all(h.retriever == "hybrid" for h in trace.hybrid_results)
    assert trace.metadata_boosts  # boost summary recorded


@pytest.mark.parametrize("failure_stage", ["none", "revision"])
def test_multi_agent_runs_one_critic_revision_and_records_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    from law_agent.review.schemas import (
        CaseAnalysis,
        CritiqueDecision,
        EvidenceSelfCheck,
        IssuePlan,
        ReviewIssue,
        ReviewResult,
        RetrievalQuery,
    )
    from law_agent.review.service import create_review_case, run_hybrid_retrieval
    from law_agent.review.llm import ReviewWorkflowFailed

    # Override the autouse fact stub so the Case Analyst routing condition
    # (should_run_case_analyst) is satisfied: region + cross_border give the
    # compound complexity required to invoke the Analyst path.
    monkeypatch.setattr(
        "law_agent.review.service.extract_facts_with_deepseek",
        lambda material, question=None: ReviewFacts(
            cross_border_transfer=True,
            region="上海",
            data_types=["手机号"],
            missing_information=[],
        ),
    )

    chunks_path = _write_fixture_corpus(tmp_path)
    response = create_review_case(
        question="这个场景是否需要数据出境安全评估？",
        material_text="手机号和定位信息发送给新加坡服务商。",
        output_dir=tmp_path,
        now=lambda: "2026-07-06T00:00:00+00:00",
        id_factory=lambda prefix: f"{prefix}_test",
    )
    revision_inputs: list[list[str] | None] = []

    monkeypatch.setattr(
        "law_agent.review.service.run_self_check_with_deepseek",
        lambda *args, **kwargs: EvidenceSelfCheck(status="sufficient"),
    )
    monkeypatch.setattr(
        "law_agent.review.service.validate_llm_self_check",
        lambda check, *args, **kwargs: check,
    )
    analyst_query = RetrievalQuery(
        query_id="q_analyst",
        query_type="legal_issue",
        text="数据出境安全评估 申报门槛 条文",
    )
    monkeypatch.setattr(
        "law_agent.review.service.run_case_analyst",
        lambda **kwargs: CaseAnalysis(
            issue_plan=IssuePlan(
                issues=[
                    ReviewIssue(
                        issue_id="issue_1",
                        question="是否达到申报门槛？",
                        query_ids=[analyst_query.query_id],
                        query_types=["legal_issue"],
                        research_queries=[analyst_query.text],
                        required_evidence_roles=["primary_legal_basis"],
                        priority="high",
                    )
                ]
            ),
            queries=list(kwargs["initial_queries"]) + [analyst_query],
        ),
    )

    def fake_build_result(**kwargs):
        revision_inputs.append(kwargs.get("critique_instructions"))
        return ReviewResult(
            review_result_id="result_test",
            review_case_id="review_test",
            trace_id="trace_test",
            risk_level="high",
            conclusion="需要进一步核验。",
            review_facts=response.review_case.review_facts,
        )

    def fake_revise_result(**kwargs):
        revision_inputs.append([action.reason for action in kwargs["actions"]])
        if failure_stage == "revision":
            raise ReviewWorkflowFailed(
                failed_node="compliance_revision",
                reason="revision_patch_validation_failed",
                message="revision failed",
                attempts=1,
            )
        return kwargs["result"]

    monkeypatch.setattr(
        "law_agent.review.service.build_review_result_with_deepseek",
        fake_build_result,
    )
    monkeypatch.setattr(
        "law_agent.review.service.revise_review_result_with_deepseek",
        fake_revise_result,
    )
    monkeypatch.setattr(
        "law_agent.review.service.run_evidence_critic",
        lambda **kwargs: CritiqueDecision(
            decision="revise",
            unsupported_claims=["结论过宽"],
            missing_issue_ids=[],
            revision_instructions=["收窄结论"],
            targeted_retrieval_requests=[
                {
                    "issue_id": "issue_1",
                    "query": "个人信息保护法 第三十九条 境外提供",
                    "query_type": "legal_issue",
                    "reason": "补充直接法条",
                }
            ],
            reason="证据只支持条件性判断",
        ),
    )

    trace = run_hybrid_retrieval(
        case_id="review_test",
        chunks_path=chunks_path,
        output_dir=tmp_path,
        top_k=5,
        review_mode="multi_agent",
        keyword_retriever=_RecordingSearchManyRetriever(FIXTURE_CHUNKS, "elasticsearch"),
        vector_retriever=_RecordingSearchManyRetriever(FIXTURE_CHUNKS, "pgvector"),
    )

    assert revision_inputs == [None, ["收窄结论"]]
    assert trace.critique_decision is not None
    assert trace.critique_decision.decision == "revise"
    researcher_step = next(
        step for step in trace.agent_steps if step.agent_name == "evidence_researcher"
    )
    assert researcher_step.llm_calls == 0
    reviewer_step = next(
        step for step in trace.agent_steps if step.agent_name == "compliance_reviewer"
    )
    assert reviewer_step.status == "completed"
    assert any(query.text == analyst_query.text for query in trace.queries)
    assert any(
        query.text == "个人信息保护法 第三十九条 境外提供"
        for query in trace.queries
    )
    assert [step.agent_name for step in trace.agent_steps] == [
        "case_analyst",
        "evidence_researcher",
        "compliance_reviewer",
        "evidence_critic",
        "targeted_researcher",
        "compliance_revision",
    ]
    revision_step = trace.agent_steps[-1]
    assert revision_step.status == (
        "failed" if failure_stage == "revision" else "completed"
    )


def test_run_hybrid_retrieval_uses_wide_candidate_pool_before_final_top_k(
    tmp_path: Path,
) -> None:
    from law_agent.review.service import create_review_case, run_hybrid_retrieval

    chunks = [
        _make_chunk(
            chunk_id=f"wide_{index}",
            doc_id=f"doc_{index}",
            source_id=f"src_{index}",
            text=f"第{index}条 数据出境安全评估 申报条件。",
        )
        for index in range(120)
    ]
    chunks_path = tmp_path / "chunks.jsonl"
    write_jsonl(chunks_path, chunks)

    create_review_case(
        question="这个场景是否需要数据出境安全评估？",
        material_text="我们会将手机号发送给新加坡服务商用于推荐优化。",
        output_dir=tmp_path,
        now=lambda: "2026-07-06T00:00:00+00:00",
        id_factory=lambda prefix: f"{prefix}_test",
    )
    keyword = _RecordingSearchManyRetriever(chunks, "keyword")
    vector = _RecordingSearchManyRetriever(chunks, "pgvector")

    trace = run_hybrid_retrieval(
        case_id="review_test",
        chunks_path=chunks_path,
        output_dir=tmp_path,
        top_k=5,
        keyword_retriever=keyword,
        vector_retriever=vector,
    )

    assert keyword.requested_top_ks == [50]
    assert vector.requested_top_ks == [50]
    assert len(trace.keyword_results) == 50
    assert len(trace.vector_results) == 50
    assert len(trace.hybrid_results) == 5


def test_run_hybrid_retrieval_persists_to_trace(tmp_path: Path) -> None:
    from law_agent.review.io import read_retrieval_traces
    from law_agent.review.service import create_review_case, run_hybrid_retrieval

    chunks_path = _write_fixture_corpus(tmp_path)

    create_review_case(
        question="数据出境安全评估",
        material_text="手机号发送给新加坡。",
        output_dir=tmp_path,
        now=lambda: "2026-07-06T00:00:00+00:00",
        id_factory=lambda prefix: f"{prefix}_test",
    )

    run_hybrid_retrieval(
        case_id="review_test",
        chunks_path=chunks_path,
        output_dir=tmp_path,
        keyword_retriever=_RecordingSearchManyRetriever(FIXTURE_CHUNKS, "elasticsearch"),
        vector_retriever=_RecordingSearchManyRetriever(FIXTURE_CHUNKS, "pgvector"),
    )

    traces = read_retrieval_traces(tmp_path / "retrieval_traces.jsonl")
    assert len(traces) == 1
    assert len(traces[0].hybrid_results) > 0
    assert len(traces[0].vector_results) > 0
    assert traces[0].metadata_boosts


def test_run_hybrid_retrieval_with_region_facts_boosts_local_evidence(tmp_path: Path) -> None:
    from law_agent.review.service import create_review_case, run_hybrid_retrieval

    chunks_path = _write_fixture_corpus(tmp_path)

    create_review_case(
        question="上海数据出境负面清单要求？",
        material_text="公司在上海自贸区开展业务，涉及数据出境。",
        output_dir=tmp_path,
        now=lambda: "2026-07-06T00:00:00+00:00",
        id_factory=lambda prefix: f"{prefix}_test",
    )

    trace = run_hybrid_retrieval(
        case_id="review_test",
        chunks_path=chunks_path,
        output_dir=tmp_path,
        top_k=10,
        keyword_retriever=_RecordingSearchManyRetriever(FIXTURE_CHUNKS, "elasticsearch"),
        vector_retriever=_RecordingSearchManyRetriever(FIXTURE_CHUNKS, "pgvector"),
    )

    # The Shanghai chunk should appear in results (region boost applied)
    chunk_ids = [h.chunk_id for h in trace.hybrid_results]
    assert "chunk_shanghai" in chunk_ids

    # Boost summary should record the local basis boost
    assert any("CN-SH" in key for key in trace.metadata_boosts)


def test_run_hybrid_retrieval_non_matching_evidence_remains_present(tmp_path: Path) -> None:
    """Boosts should elevate matching evidence, not filter out others."""

    from law_agent.review.service import create_review_case, run_hybrid_retrieval

    chunks_path = _write_fixture_corpus(tmp_path)

    create_review_case(
        question="这个场景是否需要数据出境安全评估？",
        material_text="我们会将手机号和定位信息发送给新加坡服务商用于推荐优化。",
        output_dir=tmp_path,
        now=lambda: "2026-07-06T00:00:00+00:00",
        id_factory=lambda prefix: f"{prefix}_test",
    )

    trace = run_hybrid_retrieval(
        case_id="review_test",
        chunks_path=chunks_path,
        output_dir=tmp_path,
        top_k=10,
        keyword_retriever=_RecordingSearchManyRetriever(FIXTURE_CHUNKS, "elasticsearch"),
        vector_retriever=_RecordingSearchManyRetriever(FIXTURE_CHUNKS, "pgvector"),
    )

    # Multiple citation roles should be present (no hard filtering)
    roles = {h.citation_role for h in trace.hybrid_results}
    assert len(roles) > 1  # not just one role filtered in


def test_run_hybrid_retrieval_expands_neighbors(tmp_path: Path) -> None:
    from law_agent.review.service import create_review_case, run_hybrid_retrieval

    chunks_path = _write_fixture_corpus(tmp_path)

    create_review_case(
        question="数据出境安全评估",
        material_text="手机号发送给新加坡。",
        output_dir=tmp_path,
        now=lambda: "2026-07-06T00:00:00+00:00",
        id_factory=lambda prefix: f"{prefix}_test",
    )

    trace = run_hybrid_retrieval(
        case_id="review_test",
        chunks_path=chunks_path,
        output_dir=tmp_path,
        top_k=5,
        keyword_retriever=_RecordingSearchManyRetriever(FIXTURE_CHUNKS, "elasticsearch"),
        vector_retriever=_RecordingSearchManyRetriever(FIXTURE_CHUNKS, "pgvector"),
    )

    # Neighbors should be present (linked chunks have prev/next IDs)
    assert len(trace.neighbor_chunks) > 0
    # Neighbor ranks should be negative (convention)
    assert all(n.rank < 0 for n in trace.neighbor_chunks)
