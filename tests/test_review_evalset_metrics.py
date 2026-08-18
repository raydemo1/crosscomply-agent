"""Evaluation-contract regression tests."""

from law_agent.review.evalset.metrics import aggregate_metrics, evaluate_case
from law_agent.review.evalset.schemas import EvalScenario
from law_agent.review.schemas import RetrievalHit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hit(
    source_id: str = "s1",
    chunk_id: str = "c1",
    citation_role: str = "primary_legal_basis",
    can_cite: bool = True,
    rank: int = 0,
) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id,
        doc_id="d1",
        source_id=source_id,
        title="t",
        text="x",
        score=1.0,
        rank=rank,
        retriever="hybrid",
        citation_role=citation_role,
        can_cite_clause=can_cite,
        source_url="u",
    )


def _scenario(expected_sources: list[str]) -> EvalScenario:
    return EvalScenario(
        case_id="test_citation_manual",
        question="问题",
        material_text="材料",
        expected_sources=expected_sources,
        must_have_sources=list(expected_sources),
        optional_supporting_sources=[],
    )


# ---------------------------------------------------------------------------
def test_empty_expected_sources_are_not_retrieval_scores() -> None:
    result = evaluate_case(_scenario([]), [_hit()])
    assert result.recall_at_3 is None
    assert result.recall_at_5 is None
    assert result.mrr_at_10 is None
    assert result.candidate_recall_at_50 is None


def test_aggregate_retrieval_uses_only_source_bearing_cases() -> None:
    bearing = evaluate_case(_scenario(["s1"]), [_hit(source_id="s1")])
    abstain = evaluate_case(_scenario([]), [])
    metrics = aggregate_metrics([bearing, abstain], "service_multi_agent")
    assert metrics.source_bearing_case_count == 1
    assert metrics.mean_recall_at_5 == 1.0


def test_candidate_recall_uses_strict_unique_top_50_chunks() -> None:
    candidates = [
        _hit(source_id="noise", chunk_id=f"noise-{index}", rank=index) for index in range(50)
    ] + [_hit(source_id="s1", chunk_id="expected-too-late", rank=50)]
    result = evaluate_case(
        _scenario(["s1"]),
        [_hit(source_id="s1", chunk_id="final")],
        candidate_hits=candidates,
    )
    assert result.candidate_recall_at_50 == 0.0
    assert result.candidate_unique_source_count == 1


def test_must_have_recall_excludes_optional_supporting_sources() -> None:
    scenario = EvalScenario(
        case_id="requirements_manual",
        question="问题",
        material_text="材料",
        expected_sources=["law", "guide"],
        must_have_sources=["law"],
        optional_supporting_sources=["guide"],
    )
    result = evaluate_case(scenario, [_hit(source_id="law")])
    assert result.must_have_recall_at_5 == 1.0
    assert result.optional_coverage_at_5 == 0.0
