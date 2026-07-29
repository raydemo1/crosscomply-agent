"""Evaluation runner for review modes (Issue 9).

Runs golden-set scenarios through explicit review modes, computes metrics,
and produces a comparison summary.
"""

from __future__ import annotations

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from law_agent.config import RerankMode
from law_agent.review.evalset.cases import EvalSuite, get_scenarios
from law_agent.review.evalset.metrics import aggregate_metrics, evaluate_case
from law_agent.review.evalset.schemas import (
    CaseMetricResult,
    EvalScenario,
    EvalSummary,
)
from law_agent.review.ids import utc_now_iso
from law_agent.review.io import (
    read_review_results,
)
from law_agent.review.llm import ReviewWorkflowFailed
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH
from law_agent.review.schemas import (
    CaseAnalysis,
    IssuePlan,
    RetrievalQuery,
    ReviewFacts,
    ReviewIssue,
)
from law_agent.review.service import create_review_case, run_service_retrieval

ReviewEvalMode = Literal["llm", "multi_agent"]
DEFAULT_REVIEW_MODE: ReviewEvalMode = "llm"
DEFAULT_RERANK_MODE: RerankMode = "off"
DEFAULT_EVAL_SUITE: EvalSuite = "full"
DEFAULT_MAX_WORKERS = 4
DEFAULT_EVAL_INPUTS_DIR = Path("data/review_runs/eval_inputs")


@dataclass(frozen=True)
class EvalCaseInput:
    """Frozen research inputs used to make retrieval evaluation reproducible."""

    facts: ReviewFacts
    queries: list[RetrievalQuery]


def run_evaluation(
    *,
    chunks_path: Path | str = DEFAULT_CHUNKS_PATH,
    scenarios: list[EvalScenario] | None = None,
    suite: EvalSuite = DEFAULT_EVAL_SUITE,
    top_k: int = 10,
    review_mode: ReviewEvalMode = DEFAULT_REVIEW_MODE,
    rerank_mode: RerankMode = DEFAULT_RERANK_MODE,
    max_workers: int = DEFAULT_MAX_WORKERS,
    eval_inputs_path: Path | str | None = None,
) -> EvalSummary:
    """Run full evaluation across selected modes.

    Returns an ``EvalSummary`` with per-mode aggregated metrics and bad cases.
    """

    cases_label = suite if scenarios is None else "custom"
    if scenarios is None:
        scenarios = get_scenarios(suite)

    generated_at = utc_now_iso()

    eval_key = _eval_key(review_mode, rerank_mode)
    max_workers = max(1, max_workers)
    eval_inputs: dict[str, EvalCaseInput] = {}
    if review_mode in ("llm", "multi_agent"):
        resolved_inputs_path = _default_eval_inputs_path(
            suite=suite,
            cases_label=cases_label,
            eval_inputs_path=eval_inputs_path,
        )
        if resolved_inputs_path is not None:
            eval_inputs = _load_or_build_eval_inputs(
                resolved_inputs_path,
                scenarios,
                max_workers=max_workers,
            )

    # Serial service retrieval can reuse adapters. Parallel service retrieval
    # must not share the pgvector Postgres connection across threads, so each
    # case builds its own adapters from the same config.
    from law_agent.config import require_service_config
    from law_agent.review.retrieval.service_backends import build_service_adapters

    service_adapters = None
    service_config = require_service_config()
    if max_workers == 1:
        service_adapters = build_service_adapters(service_config)

    try:
        results_by_mode: dict[str, list[CaseMetricResult]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            mode_results = list(
                executor.map(
                    lambda scenario: _run_single_case_safely(
                        scenario,
                        chunks_path,
                        review_mode=review_mode,
                        rerank_mode=rerank_mode,
                        top_k=top_k,
                        service_adapters=service_adapters,
                        service_config=service_config,
                        eval_input=eval_inputs.get(scenario.case_id),
                    ),
                    scenarios,
                )
            )
        results_by_mode[eval_key] = mode_results
    finally:
        if service_adapters is not None:
            service_adapters.close()

    mode_metrics = {
        mode: aggregate_metrics(results, mode)
        for mode, results in results_by_mode.items()
    }

    all_results = [
        result
        for results in results_by_mode.values()
        for result in results
    ]
    all_bad = [r for r in all_results if r.is_bad_case]

    return EvalSummary(
        generated_at=generated_at,
        chunks_path=str(chunks_path),
        cases_path=cases_label,
        mode_metrics=mode_metrics,
        bad_cases=all_bad,
        all_case_results=results_by_mode,
    )


def _default_eval_inputs_path(
    *,
    suite: EvalSuite,
    cases_label: str,
    eval_inputs_path: Path | str | None,
) -> Path | None:
    if eval_inputs_path is not None:
        return Path(eval_inputs_path)
    if cases_label == "custom":
        return None
    return DEFAULT_EVAL_INPUTS_DIR / f"{suite}_llm_inputs.jsonl"


def _load_or_build_eval_inputs(
    path: Path,
    scenarios: list[EvalScenario],
    *,
    max_workers: int,
) -> dict[str, EvalCaseInput]:
    loaded = _read_eval_inputs(path) if path.exists() else {}
    missing = [scenario for scenario in scenarios if scenario.case_id not in loaded]
    if missing:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            built = list(executor.map(_build_eval_input, missing))
        loaded.update(
            (scenario.case_id, eval_input)
            for scenario, eval_input in zip(missing, built, strict=True)
        )
        _write_eval_inputs(path, scenarios, loaded)
    return loaded


def _write_eval_inputs(
    path: Path,
    scenarios: list[EvalScenario],
    inputs: dict[str, EvalCaseInput],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for scenario in scenarios:
        eval_input = inputs[scenario.case_id]
        lines.append(
            json.dumps(
                {
                    "case_id": scenario.case_id,
                    "facts": eval_input.facts.model_dump(),
                    "queries": [query.model_dump() for query in eval_input.queries],
                },
                ensure_ascii=False,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_eval_inputs(path: Path) -> dict[str, EvalCaseInput]:
    inputs: dict[str, EvalCaseInput] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        inputs[payload["case_id"]] = EvalCaseInput(
            facts=ReviewFacts.model_validate(payload["facts"], strict=True),
            queries=[
                RetrievalQuery.model_validate(query, strict=True)
                for query in payload["queries"]
            ],
        )
    return inputs


def _build_eval_input(scenario: EvalScenario) -> EvalCaseInput:
    with tempfile.TemporaryDirectory() as tmpdir:
        response = create_review_case(
            question=scenario.question,
            material_text=scenario.material_text,
            output_dir=Path(tmpdir),
            now=lambda: "2026-07-06T00:00:00+00:00",
            id_factory=lambda prefix: f"{prefix}_eval",
            review_mode="llm",
        )
    return EvalCaseInput(
        facts=response.review_case.review_facts,
        queries=response.trace.queries,
    )


def _frozen_case_analyst(input: EvalCaseInput):
    """Adapt frozen eval input to the runtime Case Analyst interface."""

    query_types = list(dict.fromkeys(query.query_type for query in input.queries))
    issue_plan = IssuePlan(
        issues=[
            ReviewIssue(
                issue_id="issue_1",
                question="Frozen evaluation research plan",
                query_ids=[query.query_id for query in input.queries],
                query_types=query_types,
                required_evidence_roles=["primary_legal_basis"],
                priority="high",
            )
        ]
    )

    def run(*, question: str, material_text: str, trace_id: str) -> CaseAnalysis:
        return CaseAnalysis(
            facts=input.facts,
            issue_plan=issue_plan,
            queries=input.queries,
        )

    return run


def _run_single_case(
    scenario: EvalScenario,
    chunks_path: Path | str,
    *,
    review_mode: ReviewEvalMode,
    top_k: int,
    rerank_mode: RerankMode = DEFAULT_RERANK_MODE,
    service_adapters: object | None = None,
    service_config: object | None = None,
    eval_input: EvalCaseInput | None = None,
) -> CaseMetricResult:
    """Run a single scenario in one mode and return metrics."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        facts_extractor = None
        query_planner = None
        case_analyst = None
        if eval_input is not None and review_mode == "llm":
            facts_extractor = lambda _material, _question=None: eval_input.facts
            query_planner = lambda _question, _facts, _material=None: eval_input.queries
        elif eval_input is not None:
            case_analyst = _frozen_case_analyst(eval_input)

        create_review_case(
            question=scenario.question,
            material_text=scenario.material_text,
            output_dir=tmp_path,
            now=lambda: "2026-07-06T00:00:00+00:00",
            id_factory=lambda prefix: f"{prefix}_eval",
            review_mode=review_mode,
            facts_extractor=facts_extractor,
            query_planner=query_planner,
        )

        case_id = "review_eval"

        trace = run_service_retrieval(
            case_id=case_id,
            chunks_path=chunks_path,
            output_dir=tmp_path,
            top_k=top_k,
            review_mode=review_mode,
            rerank_mode=rerank_mode,
            config=service_config,
            adapters=service_adapters,
            case_analyst=case_analyst,
        )
        hits = trace.final_evidence or trace.hybrid_results
        second_retrieval_triggered = trace.evidence_self_check.second_retrieval_triggered

        # Get risk level and final citations from result.
        results_path = tmp_path / "review_results.jsonl"
        risk_level = ""
        if results_path.exists():
            results = read_review_results(results_path)
            if results:
                result = results[0]
                risk_level = result.risk_level

        case_metrics = evaluate_case(
            scenario,
            hits,
            candidate_hits=trace.candidate_results,
            risk_level=risk_level,
            second_retrieval_triggered=second_retrieval_triggered,
        )
        workflow_outcome = (
            "degraded_success"
            if any(step.status == "failed" for step in trace.agent_steps)
            else "clean_success"
        )
        return case_metrics.model_copy(
            update={
                "total_latency_ms": trace.total_latency_ms,
                "retrieval_latency_ms": trace.retrieval_latency_ms,
                "llm_call_count": trace.llm_call_count,
                "retry_count": trace.retry_count,
                "issue_count": len(trace.issue_plan.issues) if trace.issue_plan else 0,
                "critic_triggered": trace.critique_decision is not None,
                "critic_revised": (
                    trace.critique_decision is not None
                    and trace.critique_decision.decision
                    in {"research_required", "revision_required"}
                ),
                "targeted_retrieval_triggered": any(
                    step.agent_name == "evidence_researcher"
                    and step.decision is not None
                    and "critic-requested" in step.decision
                    for step in trace.agent_steps
                ),
                "critic_reason": (
                    trace.critique_decision.reason if trace.critique_decision else None
                ),
                "agent_steps": trace.agent_steps,
                "workflow_outcome": workflow_outcome,
            }
        )


def _run_single_case_safely(
    scenario: EvalScenario,
    chunks_path: Path | str,
    **kwargs,
) -> CaseMetricResult:
    """Convert an exhausted workflow node into one bad case, not a lost suite."""

    try:
        return _run_single_case(scenario, chunks_path, **kwargs)
    except ReviewWorkflowFailed as exc:
        return CaseMetricResult(
            case_id=scenario.case_id,
            recall_at_3=None if not scenario.expected_sources else 0.0,
            recall_at_5=None if not scenario.expected_sources else 0.0,
            mrr_at_10=None if not scenario.expected_sources else 0.0,
            candidate_recall_at_50=(
                None if not scenario.expected_sources else 0.0
            ),
            abstention_correct=False,
            is_bad_case=True,
            bad_reasons=[f"workflow_failed:{exc.failed_node}:{exc.reason}"],
            bad_case_categories=["workflow_error"],
            workflow_failed=True,
            workflow_outcome="hard_failure",
            failed_node=exc.failed_node,
            failure_reason=exc.reason,
        )


def _eval_key(
    review_mode: ReviewEvalMode,
    rerank_mode: RerankMode = DEFAULT_RERANK_MODE,
) -> str:
    base = f"retrieval=service,review={review_mode}"
    if rerank_mode == "off":
        return base
    return f"{base},rerank={rerank_mode}"


def format_summary_text(summary: EvalSummary) -> str:
    """Format evaluation summary as readable text for CLI output."""

    lines: list[str] = []
    lines.append("=" * 70)
    lines.append(f"Evaluation Summary (generated: {summary.generated_at})")
    lines.append(f"Corpus: {summary.chunks_path}")
    lines.append(f"Cases: {summary.cases_path}")
    lines.append("=" * 70)

    for mode_name in summary.mode_metrics:
        metrics = summary.mode_metrics.get(mode_name)
        if metrics is None:
            continue

        lines.append(f"\n--- {mode_name.upper()} mode ---")
        lines.append(f"  Total cases:        {metrics.total_cases}")
        lines.append(
            f"  Source-bearing cases: {metrics.source_bearing_case_count}"
        )
        lines.append(f"  Mean Recall@3:      {metrics.mean_recall_at_3:.4f}")
        lines.append(f"  Mean Recall@5:      {metrics.mean_recall_at_5:.4f}")
        lines.append(f"  Mean MRR@10:        {metrics.mean_mrr_at_10:.4f}")
        lines.append(f"  Candidate Recall@50: {metrics.mean_candidate_recall_at_50:.4f}")
        lines.append(f"  Distinct Recall@5:  {metrics.mean_distinct_source_recall_at_5:.4f}")
        lines.append(
            f"  Must-have Recall@5: {metrics.mean_must_have_recall_at_5:.4f} "
            f"({metrics.must_have_case_count} cases)"
        )
        lines.append(
            f"  Optional coverage@5: {metrics.mean_optional_coverage_at_5:.4f} "
            f"({metrics.optional_supporting_case_count} cases)"
        )
        lines.append(f"  Duplicate src@10:   {metrics.mean_duplicate_source_count_at_10:.4f}")
        lines.append(f"  Abstention accuracy: {metrics.abstention_accuracy:.4f}")
        lines.append(f"  Second retrieval trigger rate: {metrics.second_retrieval_trigger_rate:.4f}")
        if metrics.mean_total_latency_ms is not None:
            lines.append(f"  Mean total latency: {metrics.mean_total_latency_ms:.2f} ms")
        if metrics.mean_retrieval_latency_ms is not None:
            lines.append(
                f"  Mean retrieval latency: {metrics.mean_retrieval_latency_ms:.2f} ms"
            )
        lines.append(f"  LLM calls / retries: {metrics.total_llm_calls} / {metrics.total_retries}")
        lines.append(f"  Workflow success rate: {metrics.workflow_success_rate:.4f}")
        lines.append(f"  Clean success rate: {metrics.clean_success_rate:.4f}")
        lines.append(f"  Degraded success rate: {metrics.degraded_success_rate:.4f}")
        lines.append(f"  Hard failure rate: {metrics.hard_failure_rate:.4f}")
        if metrics.critic_trigger_rate:
            lines.append(f"  Critic trigger rate: {metrics.critic_trigger_rate:.4f}")
            lines.append(f"  Critic revision rate: {metrics.critic_revision_rate:.4f}")
            lines.append(
                "  Targeted retrieval rate: "
                f"{metrics.targeted_retrieval_trigger_rate:.4f}"
            )
        lines.append(f"  Bad cases:          {metrics.bad_case_count}")
        if metrics.bad_case_taxonomy:
            taxonomy = ", ".join(
                f"{category}={count}"
                for category, count in metrics.bad_case_taxonomy.items()
            )
            lines.append(f"  Bad taxonomy:       {taxonomy}")

    if summary.bad_cases:
        lines.append(f"\n--- Bad cases ({len(summary.bad_cases)}) ---")
        seen: set[str] = set()
        for case in summary.bad_cases:
            key = f"{case.case_id}"
            if key in seen:
                continue
            seen.add(key)
            categories = (
                f" categories={case.bad_case_categories}"
                if case.bad_case_categories
                else ""
            )
            lines.append(f"  {case.case_id}: {case.bad_reasons}{categories}")
            if case.missing_sources:
                lines.append(f"    missing: {case.missing_sources}")

    lines.append("\n" + "=" * 70)
    return "\n".join(lines)


def format_summary_markdown(
    summary: EvalSummary,
    *,
    run_config: dict[str, object] | None = None,
) -> str:
    """Format an evaluation result as a durable Markdown report."""

    suite_title = summary.cases_path.title()
    lines = [
        f"# LawAgent {suite_title} Evaluation Report",
        "",
        f"- Generated: `{summary.generated_at}`",
        f"- Corpus: `{summary.chunks_path}`",
        f"- Cases: `{summary.cases_path}`",
    ]
    if run_config:
        lines.extend(["", "## Run configuration", ""])
        for key, value in run_config.items():
            lines.append(f"- {key}: `{value}`")
    for mode_name, metrics in summary.mode_metrics.items():
        lines.extend(
            [
                "",
                f"## {mode_name}",
                "",
                "| Metric | Value |",
                "|---|---:|",
                f"| Total cases | {metrics.total_cases} |",
                f"| Source-bearing cases | {metrics.source_bearing_case_count} |",
                f"| Recall@3 | {metrics.mean_recall_at_3:.4f} |",
                f"| Recall@5 | {metrics.mean_recall_at_5:.4f} |",
                f"| MRR@10 | {metrics.mean_mrr_at_10:.4f} |",
                f"| Candidate Recall@50 | {metrics.mean_candidate_recall_at_50:.4f} |",
                f"| Must-have Recall@5 | {metrics.mean_must_have_recall_at_5:.4f} ({metrics.must_have_case_count} cases) |",
                f"| Optional coverage@5 | {metrics.mean_optional_coverage_at_5:.4f} ({metrics.optional_supporting_case_count} cases) |",
                f"| Abstention accuracy | {metrics.abstention_accuracy:.4f} |",
                f"| Second retrieval trigger rate | {metrics.second_retrieval_trigger_rate:.4f} |",
                f"| Bad cases | {metrics.bad_case_count} |",
                f"| Mean total latency (ms) | {_optional_number(metrics.mean_total_latency_ms)} |",
                f"| Mean retrieval latency (ms) | {_optional_number(metrics.mean_retrieval_latency_ms)} |",
                f"| Total LLM calls | {metrics.total_llm_calls} |",
                f"| Total retries | {metrics.total_retries} |",
                f"| Workflow success rate | {metrics.workflow_success_rate:.4f} |",
                f"| Clean success rate | {metrics.clean_success_rate:.4f} |",
                f"| Degraded success rate | {metrics.degraded_success_rate:.4f} |",
                f"| Hard failure rate | {metrics.hard_failure_rate:.4f} |",
                f"| Critic trigger rate | {metrics.critic_trigger_rate:.4f} |",
                f"| Critic revision rate | {metrics.critic_revision_rate:.4f} |",
                f"| Targeted retrieval trigger rate | {metrics.targeted_retrieval_trigger_rate:.4f} |",
            ]
        )
        if metrics.bad_case_taxonomy:
            lines.extend(["", "### Bad-case taxonomy", ""])
            for category, count in metrics.bad_case_taxonomy.items():
                lines.append(f"- `{category}`: {count}")

    lines.extend(["", "## Bad cases", ""])
    if not summary.bad_cases:
        lines.append("No bad cases.")
    else:
        lines.extend(["| Case | Categories | Reasons | Missing sources |", "|---|---|---|---|"])
        for case in summary.bad_cases:
            lines.append(
                "| "
                + " | ".join(
                    [
                        case.case_id,
                        ", ".join(case.bad_case_categories) or "-",
                        "<br>".join(case.bad_reasons) or "-",
                        "<br>".join(case.missing_sources) or "-",
                    ]
                )
                + " |"
            )
    lines.append("")
    return "\n".join(lines)


def _optional_number(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "-"
