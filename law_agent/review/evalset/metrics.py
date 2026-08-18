"""Evaluation metrics for review retrieval (Issue 9).

Metrics:
- Recall@K: fraction of expected sources found in top-K results
- MRR@K: mean reciprocal rank of first expected source
- Abstention accuracy: did the system correctly abstain when it should?
- Second retrieval accuracy: did second retrieval trigger when expected?
"""

from __future__ import annotations

from collections import Counter

from law_agent.review.evalset.schemas import (
    BadCaseCategory,
    CaseMetricResult,
    EvalScenario,
)
from law_agent.review.schemas import RetrievalHit


def compute_recall_at_k(
    hits: list[RetrievalHit],
    expected_sources: list[str],
    k: int,
) -> tuple[float | None, list[str]]:
    """Compute Recall@K. Returns (score, missing_sources)."""

    if not expected_sources:
        return None, []

    top_k_sources = {h.source_id for h in hits[:k]}
    found = top_k_sources & set(expected_sources)
    missing = [s for s in expected_sources if s not in top_k_sources]
    return len(found) / len(expected_sources), missing


def compute_mrr_at_k(
    hits: list[RetrievalHit],
    expected_sources: list[str],
    k: int,
) -> float | None:
    """Compute MRR@K: reciprocal rank of first expected source in top-K."""

    if not expected_sources:
        return None

    expected_set = set(expected_sources)
    for i, hit in enumerate(hits[:k]):
        if hit.source_id in expected_set:
            return 1.0 / (i + 1)
    return 0.0


def compute_source_pool_recall(
    hits: list[RetrievalHit],
    expected_sources: list[str],
) -> tuple[float | None, list[str]]:
    """Compute source recall over an unordered candidate pool."""

    if not expected_sources:
        return None, []

    pool_sources = {h.source_id for h in hits}
    found = pool_sources & set(expected_sources)
    missing = [s for s in expected_sources if s not in pool_sources]
    return len(found) / len(expected_sources), missing


def distinct_source_hits_at_k(
    hits: list[RetrievalHit],
    k: int,
) -> list[RetrievalHit]:
    """Return the first K hits after collapsing duplicate source IDs."""

    seen: set[str] = set()
    distinct: list[RetrievalHit] = []
    for hit in hits:
        if hit.source_id in seen:
            continue
        seen.add(hit.source_id)
        distinct.append(hit)
        if len(distinct) >= k:
            break
    return distinct


def count_duplicate_sources_at_k(hits: list[RetrievalHit], k: int) -> int:
    """Count how many top-K positions are occupied by duplicate sources."""

    top = hits[:k]
    return len(top) - len({hit.source_id for hit in top})


def ordered_unique_sources(hits: list[RetrievalHit]) -> list[str]:
    """Return source IDs in result order, collapsing duplicates."""

    seen: set[str] = set()
    sources: list[str] = []
    for hit in hits:
        if hit.source_id in seen:
            continue
        seen.add(hit.source_id)
        sources.append(hit.source_id)
    return sources


def evaluate_case(
    scenario: EvalScenario,
    hits: list[RetrievalHit],
    *,
    candidate_hits: list[RetrievalHit] | None = None,
    risk_level: str = "",
    second_retrieval_triggered: bool = False,
) -> CaseMetricResult:
    """Evaluate a single scenario case against retrieval results.

    Args:
        scenario: The golden-set scenario.
        hits: Retrieved evidence hits (hybrid_results or keyword_results).
        risk_level: The review result risk level (for abstention check).
        second_retrieval_triggered: Whether second retrieval was triggered.
    """

    recall_3, _missing_3 = compute_recall_at_k(hits, scenario.expected_sources, 3)
    recall_5, missing_5 = compute_recall_at_k(hits, scenario.expected_sources, 5)
    mrr = compute_mrr_at_k(hits, scenario.expected_sources, 10)
    unique_candidates = []
    seen_chunks: set[str] = set()
    for hit in candidate_hits if candidate_hits is not None else hits:
        if hit.chunk_id in seen_chunks:
            continue
        seen_chunks.add(hit.chunk_id)
        unique_candidates.append(hit)
        if len(unique_candidates) == 50:
            break
    candidate_recall_50, candidate_missing = compute_source_pool_recall(
        unique_candidates,
        scenario.expected_sources,
    )
    distinct_recall_5, _distinct_missing = compute_recall_at_k(
        distinct_source_hits_at_k(hits, 5),
        scenario.expected_sources,
        5,
    )
    must_have_recall_5, _must_have_missing = compute_recall_at_k(
        hits, scenario.must_have_sources, 5
    )
    optional_coverage_5, _optional_missing = compute_recall_at_k(
        hits, scenario.optional_supporting_sources, 5
    )
    duplicate_sources_10 = count_duplicate_sources_at_k(hits, 10)

    # Abstention check
    abstention_correct = True
    if scenario.should_abstain:
        abstention_correct = risk_level == "insufficient_evidence"
    else:
        # Should NOT abstain — if risk_level is insufficient, that's wrong
        if risk_level == "insufficient_evidence":
            abstention_correct = False

    # Second retrieval is recorded as a FACT only (did it fire?), not
    # compared against any expectation and never counted as a bad case.
    # Whether second retrieval fires is a means, not an end: if the final
    # result is bad, `low_recall_at_5` / `zero_recall` already flag it
    # regardless of trigger behavior.

    # Determine bad case taxonomy.
    # Only outcome metrics (recall and abstention) can mark a
    # case as bad. Second retrieval is intentionally excluded.
    bad_reasons: list[str] = []
    bad_categories: list[BadCaseCategory] = []
    if scenario.expected_sources and recall_5 == 0.0 and not scenario.should_abstain:
        bad_reasons.append("zero_recall_at_5")
        bad_categories.append("retrieval_zero_recall")
    elif (
        scenario.expected_sources
        and recall_5 < scenario.min_recall_at_5
        and not scenario.should_abstain
    ):
        bad_reasons.append(f"low_recall_at_5={recall_5:.4f}<min={scenario.min_recall_at_5:.4f}")
        bad_categories.append("retrieval_low_recall")
    if (
        scenario.expected_sources
        and candidate_recall_50 < scenario.min_recall_at_5
        and not scenario.should_abstain
    ):
        bad_reasons.append(f"candidate_missing={candidate_missing}")
        bad_categories.append("candidate_missing")
    if not abstention_correct:
        bad_reasons.append("abstention_incorrect")
        bad_categories.append("abstention_error")
    return CaseMetricResult(
        case_id=scenario.case_id,
        recall_at_3=round(recall_3, 4) if recall_3 is not None else None,
        recall_at_5=round(recall_5, 4) if recall_5 is not None else None,
        mrr_at_10=round(mrr, 4) if mrr is not None else None,
        candidate_recall_at_50=(
            round(candidate_recall_50, 4) if candidate_recall_50 is not None else None
        ),
        candidate_unique_source_count=len({hit.source_id for hit in unique_candidates}),
        distinct_source_recall_at_5=(
            round(distinct_recall_5, 4) if distinct_recall_5 is not None else None
        ),
        must_have_recall_at_5=(
            round(must_have_recall_5, 4) if must_have_recall_5 is not None else None
        ),
        optional_coverage_at_5=(
            round(optional_coverage_5, 4) if optional_coverage_5 is not None else None
        ),
        duplicate_source_count_at_10=duplicate_sources_10,
        abstention_correct=abstention_correct,
        second_retrieval_triggered=second_retrieval_triggered,
        actual_sources=ordered_unique_sources(hits),
        missing_sources=missing_5,
        is_bad_case=len(bad_reasons) > 0,
        bad_reasons=bad_reasons,
        bad_case_categories=bad_categories,
        risk_level=risk_level,
    )


def aggregate_metrics(
    case_results: list[CaseMetricResult],
    mode: str,
) -> object:
    """Aggregate per-case metrics into mode-level summary."""

    from law_agent.review.evalset.schemas import ModeMetrics

    total = len(case_results)
    if total == 0:
        return ModeMetrics(
            mode=mode,
            mean_recall_at_3=0.0,
            mean_recall_at_5=0.0,
            mean_mrr_at_10=0.0,
            mean_candidate_recall_at_50=0.0,
            mean_distinct_source_recall_at_5=0.0,
            mean_duplicate_source_count_at_10=0.0,
            abstention_accuracy=0.0,
            second_retrieval_trigger_rate=0.0,
            mean_total_latency_ms=None,
            mean_retrieval_latency_ms=None,
            total_llm_calls=0,
            total_retries=0,
            workflow_success_rate=0.0,
            bad_case_count=0,
            bad_case_taxonomy={},
            total_cases=0,
        )

    source_bearing = [c for c in case_results if c.recall_at_5 is not None]
    source_total = len(source_bearing)
    retrieval_denominator = source_total or 1
    mean_recall_3 = sum(c.recall_at_3 or 0.0 for c in source_bearing) / retrieval_denominator
    mean_recall_5 = sum(c.recall_at_5 or 0.0 for c in source_bearing) / retrieval_denominator
    mean_mrr = sum(c.mrr_at_10 or 0.0 for c in source_bearing) / retrieval_denominator
    mean_candidate_recall_50 = (
        sum(c.candidate_recall_at_50 or 0.0 for c in source_bearing) / retrieval_denominator
    )
    mean_distinct_recall_5 = (
        sum(c.distinct_source_recall_at_5 or 0.0 for c in source_bearing) / retrieval_denominator
    )
    must_have_cases = [c for c in case_results if c.must_have_recall_at_5 is not None]
    optional_cases = [c for c in case_results if c.optional_coverage_at_5 is not None]
    mean_must_have_recall_5 = sum(c.must_have_recall_at_5 or 0.0 for c in must_have_cases) / (
        len(must_have_cases) or 1
    )
    mean_optional_coverage_5 = sum(c.optional_coverage_at_5 or 0.0 for c in optional_cases) / (
        len(optional_cases) or 1
    )
    mean_duplicate_sources_10 = sum(c.duplicate_source_count_at_10 for c in case_results) / total

    abstention_correct = sum(1 for c in case_results if c.abstention_correct)
    second_retrieval_fired = sum(1 for c in case_results if c.second_retrieval_triggered)
    total_latencies = [c.total_latency_ms for c in case_results if c.total_latency_ms is not None]
    retrieval_latencies = [
        c.retrieval_latency_ms for c in case_results if c.retrieval_latency_ms is not None
    ]
    total_llm_calls = sum(c.llm_call_count for c in case_results)
    total_retries = sum(c.retry_count for c in case_results)
    workflow_successes = sum(1 for c in case_results if not c.workflow_failed)
    clean_successes = sum(1 for c in case_results if c.workflow_outcome == "clean_success")
    degraded_successes = sum(1 for c in case_results if c.workflow_outcome == "degraded_success")
    hard_failures = sum(1 for c in case_results if c.workflow_outcome == "hard_failure")
    critic_triggers = sum(1 for c in case_results if c.critic_triggered)
    critic_revisions = sum(1 for c in case_results if c.critic_revised)
    targeted_retrievals = sum(1 for c in case_results if c.targeted_retrieval_triggered)

    bad_count = sum(1 for c in case_results if c.is_bad_case)
    taxonomy = Counter(category for case in case_results for category in case.bad_case_categories)

    return ModeMetrics(
        mode=mode,
        mean_recall_at_3=round(mean_recall_3, 4),
        mean_recall_at_5=round(mean_recall_5, 4),
        mean_mrr_at_10=round(mean_mrr, 4),
        mean_candidate_recall_at_50=round(mean_candidate_recall_50, 4),
        mean_distinct_source_recall_at_5=round(mean_distinct_recall_5, 4),
        mean_must_have_recall_at_5=round(mean_must_have_recall_5, 4),
        mean_optional_coverage_at_5=round(mean_optional_coverage_5, 4),
        must_have_case_count=len(must_have_cases),
        optional_supporting_case_count=len(optional_cases),
        mean_duplicate_source_count_at_10=round(mean_duplicate_sources_10, 4),
        abstention_accuracy=round(abstention_correct / total, 4),
        second_retrieval_trigger_rate=round(second_retrieval_fired / total, 4),
        mean_total_latency_ms=(
            round(sum(total_latencies) / len(total_latencies), 2) if total_latencies else None
        ),
        mean_retrieval_latency_ms=(
            round(sum(retrieval_latencies) / len(retrieval_latencies), 2)
            if retrieval_latencies
            else None
        ),
        total_llm_calls=total_llm_calls,
        total_retries=total_retries,
        workflow_success_rate=round(workflow_successes / total, 4),
        clean_success_rate=round(clean_successes / total, 4),
        degraded_success_rate=round(degraded_successes / total, 4),
        hard_failure_rate=round(hard_failures / total, 4),
        source_bearing_case_count=source_total,
        critic_trigger_rate=round(critic_triggers / total, 4),
        critic_revision_rate=round(critic_revisions / total, 4),
        targeted_retrieval_trigger_rate=round(targeted_retrievals / total, 4),
        bad_case_count=bad_count,
        bad_case_taxonomy=dict(sorted(taxonomy.items())),
        total_cases=total,
    )
