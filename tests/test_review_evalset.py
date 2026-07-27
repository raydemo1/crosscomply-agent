"""Tests for evaluation suite (Issue 9)."""

from law_agent.review.evalset.cases import get_default_scenarios, get_scenarios
from law_agent.review.evalset.metrics import (
    aggregate_metrics,
    count_duplicate_sources_at_k,
    compute_mrr_at_k,
    compute_recall_at_k,
    compute_source_pool_recall,
    distinct_source_hits_at_k,
    evaluate_case,
    ordered_unique_sources,
)
from law_agent.review.evalset.schemas import CaseMetricResult, EvalScenario
from law_agent.review.schemas import RetrievalHit


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _hit(
    source_id: str = "s1",
    chunk_id: str = "c1",
    citation_role: str = "primary_legal_basis",
    can_cite: bool = True,
    rank: int = 0,
) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id, doc_id="d1", source_id=source_id, title="t", text="x",
        score=1.0, rank=rank, retriever="hybrid",
        citation_role=citation_role, can_cite_clause=can_cite, source_url="u",
    )


# ---------------------------------------------------------------------------
# Golden set tests
# ---------------------------------------------------------------------------

def test_default_scenarios_includes_abstention_case() -> None:
    scenarios = get_default_scenarios()
    abstain = [s for s in scenarios if s.should_abstain]
    assert len(abstain) >= 1


def test_default_scenarios_includes_cross_border_cases() -> None:
    scenarios = get_default_scenarios()
    cross_border = [s for s in scenarios if "cross_border" in s.tags]
    assert len(cross_border) >= 2


def test_default_scenarios_includes_regional_cases() -> None:
    scenarios = get_default_scenarios()
    regional = [s for s in scenarios if "regional" in s.tags]
    assert len(regional) >= 2


def test_default_scenarios_includes_industry_cases() -> None:
    scenarios = get_default_scenarios()
    industry = [s for s in scenarios if "automotive" in s.tags or "industry" in s.tags]
    assert len(industry) >= 1


def test_eval_suites_are_distinct_and_have_unique_case_ids() -> None:
    quick = get_scenarios("quick")
    base = get_scenarios("base")
    full = get_scenarios("full")

    assert 8 <= len(quick) <= 15
    assert len(base) == 24
    assert 80 <= len(full) <= 120
    assert len({case.case_id for case in full}) == len(full)
    assert {case.case_id for case in quick}.issubset(
        {case.case_id for case in full}
    )


def test_full_suite_covers_required_domains() -> None:
    scenarios = get_scenarios("full")
    tags = {tag for scenario in scenarios for tag in scenario.tags}

    required = {
        "cross_border",
        "standard_contract",
        "certification",
        "automotive",
        "financial",
        "regional",
        "tc260",
        "qna",
        "abstention",
        "out_of_corpus",
        "conflict",
    }
    assert required <= tags


def test_source_bearing_scenarios_partition_must_have_and_optional_sources() -> None:
    for scenario in get_scenarios("full"):
        must_have = set(scenario.must_have_sources)
        optional = set(scenario.optional_supporting_sources)
        assert must_have.isdisjoint(optional)
        assert must_have | optional == set(scenario.expected_sources)


# ---------------------------------------------------------------------------
# Recall@K tests
# ---------------------------------------------------------------------------

def test_recall_at_k_all_found() -> None:
    hits = [_hit(source_id="s1"), _hit(source_id="s2", rank=1)]
    score, missing = compute_recall_at_k(hits, ["s1", "s2"], k=5)
    assert score == 1.0
    assert missing == []


def test_recall_at_k_partial() -> None:
    hits = [_hit(source_id="s1"), _hit(source_id="s3", rank=1)]
    score, missing = compute_recall_at_k(hits, ["s1", "s2"], k=5)
    assert score == 0.5
    assert "s2" in missing


def test_recall_at_k_empty_expected() -> None:
    hits = [_hit(source_id="s1")]
    score, missing = compute_recall_at_k(hits, [], k=5)
    assert score is None
    assert missing == []


def test_recall_at_k_respects_k() -> None:
    """Only top-K positions are considered, not rank field."""

    hits = [
        _hit(source_id=f"s{i}", rank=i) for i in range(6)
    ]  # 6 hits, s5 is at position 5
    score, _ = compute_recall_at_k(hits, ["s5"], k=3)
    assert score == 0.0  # s5 is at position 5, outside top-3


def test_source_pool_and_distinct_source_metrics() -> None:
    hits = [
        _hit(source_id="s1", chunk_id="c1", rank=0),
        _hit(source_id="s1", chunk_id="c2", rank=1),
        _hit(source_id="s2", chunk_id="c3", rank=2),
    ]

    pool_recall, missing = compute_source_pool_recall(hits, ["s1", "s2", "s3"])
    distinct = distinct_source_hits_at_k(hits, 5)

    assert pool_recall == 2 / 3
    assert missing == ["s3"]
    assert [hit.source_id for hit in distinct] == ["s1", "s2"]
    assert count_duplicate_sources_at_k(hits, 3) == 1


def test_ordered_unique_sources_preserves_result_order() -> None:
    hits = [
        _hit(source_id="s2", chunk_id="c1", rank=0),
        _hit(source_id="s1", chunk_id="c2", rank=1),
        _hit(source_id="s2", chunk_id="c3", rank=2),
        _hit(source_id="s3", chunk_id="c4", rank=3),
    ]

    assert ordered_unique_sources(hits) == ["s2", "s1", "s3"]


# ---------------------------------------------------------------------------
# MRR@K tests
# ---------------------------------------------------------------------------

def test_mrr_at_k_first_hit() -> None:
    hits = [_hit(source_id="s1", rank=0)]
    assert compute_mrr_at_k(hits, ["s1"], k=10) == 1.0


def test_mrr_at_k_second_hit() -> None:
    hits = [_hit(source_id="s3", rank=0), _hit(source_id="s1", rank=1)]
    assert compute_mrr_at_k(hits, ["s1"], k=10) == 0.5


def test_mrr_at_k_not_found() -> None:
    hits = [_hit(source_id="s3", rank=0)]
    assert compute_mrr_at_k(hits, ["s1"], k=10) == 0.0


def test_mrr_at_k_empty_expected() -> None:
    hits = [_hit(source_id="s1")]
    assert compute_mrr_at_k(hits, [], k=10) is None


# ---------------------------------------------------------------------------
# evaluate_case tests
# ---------------------------------------------------------------------------

def test_evaluate_case_good_retrieval() -> None:
    scenario = EvalScenario(
        case_id="test_001",
        question="问题",
        material_text="材料",
        expected_sources=["s1", "s2"],
        must_have_sources=["s1", "s2"],
        should_abstain=False,
    )
    hits = [_hit(source_id="s1", rank=0), _hit(source_id="s2", rank=1)]

    result = evaluate_case(scenario, hits, risk_level="medium")

    assert result.recall_at_5 == 1.0
    assert result.mrr_at_10 == 1.0
    assert result.abstention_correct is True
    assert result.is_bad_case is False


def test_evaluate_case_low_recall_is_bad() -> None:
    scenario = EvalScenario(
        case_id="test_002",
        question="问题",
        material_text="材料",
        expected_sources=["s1", "s2"],
        must_have_sources=["s1", "s2"],
        should_abstain=False,
    )
    hits = [_hit(source_id="s3", rank=0)]  # no expected sources

    result = evaluate_case(scenario, hits, risk_level="medium")

    assert result.recall_at_5 == 0.0
    assert result.is_bad_case is True
    assert "zero_recall_at_5" in result.bad_reasons
    assert "retrieval_zero_recall" in result.bad_case_categories


def test_evaluate_case_low_nonzero_recall_has_taxonomy() -> None:
    scenario = EvalScenario(
        case_id="test_low_recall",
        question="问题",
        material_text="材料",
        expected_sources=["s1", "s2", "s3"],
        must_have_sources=["s1", "s2", "s3"],
        min_recall_at_5=0.8,
    )
    hits = [_hit(source_id="s1", rank=0)]

    result = evaluate_case(scenario, hits)

    assert result.recall_at_5 == 0.3333
    assert "retrieval_low_recall" in result.bad_case_categories


def test_evaluate_case_records_candidate_and_source_diversity_metrics() -> None:
    scenario = EvalScenario(
        case_id="test_diagnostics",
        question="问题",
        material_text="材料",
        expected_sources=["s1", "s2"],
        must_have_sources=["s1", "s2"],
    )
    hits = [
        _hit(source_id="s1", chunk_id="c1", rank=0),
        _hit(source_id="s1", chunk_id="c2", rank=1),
        _hit(source_id="s3", chunk_id="c3", rank=2),
    ]
    candidate_hits = hits + [_hit(source_id="s2", chunk_id="c4", rank=50)]

    result = evaluate_case(scenario, hits, candidate_hits=candidate_hits)

    assert result.candidate_recall_at_50 == 1.0
    assert result.distinct_source_recall_at_5 == 0.5
    assert result.duplicate_source_count_at_10 == 1


def test_evaluate_case_candidate_missing_has_taxonomy() -> None:
    scenario = EvalScenario(
        case_id="test_candidate_missing",
        question="问题",
        material_text="材料",
        expected_sources=["s1", "s2"],
        must_have_sources=["s1", "s2"],
        min_recall_at_5=0.75,
    )
    hits = [_hit(source_id="s1", rank=0)]
    candidate_hits = [_hit(source_id="s1", rank=0)]

    result = evaluate_case(scenario, hits, candidate_hits=candidate_hits)

    assert "candidate_missing" in result.bad_case_categories


def test_evaluate_case_abstention_correct() -> None:
    scenario = EvalScenario(
        case_id="test_003",
        question="问题",
        material_text="材料",
        expected_sources=[],
        should_abstain=True,
    )
    hits: list[RetrievalHit] = []

    result = evaluate_case(scenario, hits, risk_level="insufficient_evidence")

    assert result.abstention_correct is True
    assert result.is_bad_case is False


def test_evaluate_case_abstention_incorrect() -> None:
    scenario = EvalScenario(
        case_id="test_004",
        question="问题",
        material_text="材料",
        expected_sources=["s1"],
        must_have_sources=["s1"],
        should_abstain=False,
    )
    hits: list[RetrievalHit] = []

    result = evaluate_case(scenario, hits, risk_level="insufficient_evidence")

    assert result.abstention_correct is False
    assert result.is_bad_case is True
    assert "abstention_incorrect" in result.bad_reasons
    assert "abstention_error" in result.bad_case_categories


def test_evaluate_case_second_retrieval_recorded_as_fact() -> None:
    """Second retrieval is recorded as a fact, never drives bad-case."""
    scenario = EvalScenario(
        case_id="test_005",
        question="问题",
        material_text="材料",
        expected_sources=["s1"],
        must_have_sources=["s1"],
    )
    hits = [_hit(source_id="s1")]

    triggered = evaluate_case(scenario, hits, second_retrieval_triggered=True)
    not_triggered = evaluate_case(scenario, hits, second_retrieval_triggered=False)

    assert triggered.second_retrieval_triggered is True
    assert not_triggered.second_retrieval_triggered is False
    # Neither should be a bad case purely due to second retrieval behavior.
    assert not triggered.is_bad_case
    assert not not_triggered.is_bad_case
    assert "second_retrieval_incorrect" not in triggered.bad_reasons
    assert "second_retrieval_incorrect" not in not_triggered.bad_reasons
    assert "second_retrieval_error" not in triggered.bad_case_categories
    assert "second_retrieval_error" not in not_triggered.bad_case_categories


# ---------------------------------------------------------------------------
# Aggregate metrics tests
# ---------------------------------------------------------------------------

def test_aggregate_metrics_calculates_means() -> None:
    results = [
        CaseMetricResult(
            case_id="c1", recall_at_3=1.0, recall_at_5=1.0, mrr_at_10=1.0,
            candidate_recall_at_50=1.0, distinct_source_recall_at_5=1.0,
            duplicate_source_count_at_10=0,
            abstention_correct=True,
            second_retrieval_triggered=True,
            total_latency_ms=100,
            retrieval_latency_ms=40,
            llm_call_count=2,
            retry_count=0,
        ),
        CaseMetricResult(
            case_id="c2", recall_at_3=0.5, recall_at_5=0.5, mrr_at_10=0.5,
            candidate_recall_at_50=0.75, distinct_source_recall_at_5=0.25,
            duplicate_source_count_at_10=2,
            abstention_correct=False,
            second_retrieval_triggered=False, is_bad_case=True,
            total_latency_ms=300,
            retrieval_latency_ms=80,
            llm_call_count=4,
            retry_count=1,
            bad_reasons=["test"], bad_case_categories=["abstention_error"],
        ),
    ]

    metrics = aggregate_metrics(results, "keyword")

    assert metrics.mode == "keyword"
    assert metrics.mean_recall_at_3 == 0.75
    assert metrics.mean_recall_at_5 == 0.75
    assert metrics.mean_mrr_at_10 == 0.75
    assert metrics.mean_candidate_recall_at_50 == 0.875
    assert metrics.mean_distinct_source_recall_at_5 == 0.625
    assert metrics.mean_duplicate_source_count_at_10 == 1.0
    assert metrics.abstention_accuracy == 0.5
    assert metrics.second_retrieval_trigger_rate == 0.5
    assert metrics.mean_total_latency_ms == 200.0
    assert metrics.mean_retrieval_latency_ms == 60.0
    assert metrics.total_llm_calls == 6
    assert metrics.total_retries == 1
    assert metrics.bad_case_count == 1
    assert metrics.bad_case_taxonomy == {"abstention_error": 1}
    assert metrics.total_cases == 2


def test_aggregate_metrics_empty() -> None:
    metrics = aggregate_metrics([], "keyword")

    assert metrics.total_cases == 0
    assert metrics.mean_recall_at_3 == 0.0


# ---------------------------------------------------------------------------
# Runner integration test (fixture corpus)
# ---------------------------------------------------------------------------

def test_format_summary_text_contains_key_metrics() -> None:
    from law_agent.review.evalset.runner import format_summary_text
    from law_agent.review.evalset.schemas import EvalSummary, ModeMetrics

    summary = EvalSummary(
        generated_at="2026-07-06T00:00:00+00:00",
        chunks_path="test.jsonl",
        cases_path="default",
        mode_metrics={
            "retrieval=service,review=llm": ModeMetrics(
                mode="retrieval=service,review=llm",
                mean_recall_at_3=0.7500,
                mean_recall_at_5=0.8000,
                mean_mrr_at_10=0.9000,
                abstention_accuracy=1.0,
                second_retrieval_trigger_rate=0.5,
                mean_total_latency_ms=123.4,
                mean_retrieval_latency_ms=45.6,
                total_llm_calls=8,
                total_retries=1,
                bad_case_count=1,
                total_cases=10,
            ),
        },
    )

    text = format_summary_text(summary)

    assert "RETRIEVAL=LOCAL,REVIEW=LOCAL mode" in text
    assert "Recall@3" in text
    assert "MRR@10" in text
    assert "Candidate Recall@50" in text
    assert "Mean total latency" in text
    assert "LLM calls / retries" in text
    assert "Bad cases" in text
